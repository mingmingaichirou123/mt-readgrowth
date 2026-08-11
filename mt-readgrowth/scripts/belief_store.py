#!/usr/bin/env python3
"""Manage the append-only long-term belief lifecycle and durable views."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True


SCHEMA_VERSION = 1
BELIEF_EVENT_PATH = Path("knowledge/belief-events.jsonl")
BELIEF_DIR = Path("knowledge/beliefs")
ACTIVE_BELIEF_STATUSES = {"provisional", "confirmed", "revised", "retired"}
TERMINAL_STATUSES = {"rejected", "retired"}


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def format_timestamp(value: str | None = None) -> str:
    if value is None:
        parsed = datetime.now(timezone.utc)
    else:
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except (AttributeError, ValueError) as exc:
            raise ValueError("belief timestamp must be RFC3339") from exc
        if parsed.tzinfo is None:
            raise ValueError("belief timestamp must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    values: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number} is not a JSON object")
        values.append(value)
    return values


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(canonical_json(value) + "\n")


def candidate_id(session_id: str, index: int, content: str) -> str:
    digest = sha256_text(f"{session_id}\0{index}\0{content}")
    return f"candidate-{digest[:16]}"


def belief_id_for(candidate: str) -> str:
    return "belief-" + candidate.removeprefix("candidate-")


def candidate_event(candidate: dict[str, Any], at: str) -> dict[str, Any]:
    identifier = str(candidate.get("candidate_id") or "")
    content = str(candidate.get("content") or "").strip()
    source = candidate.get("source")
    if not identifier or not content or not isinstance(source, dict):
        raise ValueError("belief candidate requires candidate_id, content, and source")
    belief_id = str(candidate.get("belief_id") or belief_id_for(identifier))
    event_id = "belief-event-" + sha256_text("candidate-recorded\0" + identifier)
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": event_id,
        "event": "belief-candidate-recorded",
        "at": format_timestamp(at),
        "candidate_id": identifier,
        "belief_id": belief_id,
        "from_status": None,
        "to_status": "candidate",
        "content": content,
        "content_sha256": sha256_text(content),
        "source": source,
        "decision_basis": "session-summary-candidate",
    }


def _transition(decision: str, current: str, revised_content: str | None, content: str) -> tuple[str, str]:
    if current in TERMINAL_STATUSES:
        raise ValueError(f"belief is terminal: {current}")
    if decision == "confirm":
        if current == "candidate":
            return "provisional", content
        if current == "provisional":
            return "confirmed", content
        raise ValueError(f"confirm is not allowed from {current}")
    if decision == "revise":
        if not revised_content or not revised_content.strip():
            raise ValueError("revise requires --revised-content")
        target = "provisional" if current == "candidate" else "revised"
        if current not in {"candidate", "provisional", "confirmed", "revised"}:
            raise ValueError(f"revise is not allowed from {current}")
        return target, revised_content.strip()
    if decision == "reject":
        if current != "candidate":
            raise ValueError("reject is allowed only from candidate")
        return "rejected", content
    if decision == "retire":
        if current not in {"provisional", "confirmed", "revised"}:
            raise ValueError(f"retire is not allowed from {current}")
        return "retired", content
    raise ValueError(f"unsupported decision: {decision}")


def decision_event(
    state: dict[str, Any],
    decision: str,
    user_statement: str,
    revised_content: str | None,
    at: str,
) -> dict[str, Any]:
    statement = user_statement.strip()
    if not statement:
        raise ValueError("--user-statement is required and must quote the user's explicit decision")
    current = str(state["status"])
    target, content = _transition(decision, current, revised_content, str(state["content"]))
    identity = canonical_json({
        "candidate_id": state["candidate_id"],
        "from_status": current,
        "decision": decision,
        "user_statement": statement,
        "content": content,
    })
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": "belief-event-" + sha256_text(identity),
        "event": "belief-user-decision",
        "at": format_timestamp(at),
        "candidate_id": state["candidate_id"],
        "belief_id": state["belief_id"],
        "from_status": current,
        "to_status": target,
        "decision": decision,
        "content": content,
        "content_sha256": sha256_text(content),
        "user_statement": statement,
        "decision_basis": "explicit-user-decision",
        "source": state["source"],
    }


def _validate_candidate_event(event: dict[str, Any]) -> None:
    candidate = str(event.get("candidate_id") or "")
    content = event.get("content")
    source = event.get("source")
    if (
        event.get("schema_version") != SCHEMA_VERSION
        or event.get("event") != "belief-candidate-recorded"
        or not str(event.get("event_id") or "")
        or not candidate
        or event.get("belief_id") != belief_id_for(candidate)
        or event.get("from_status") is not None
        or event.get("to_status") != "candidate"
        or not isinstance(content, str)
        or not content.strip()
        or event.get("content_sha256") != sha256_text(content)
        or not isinstance(source, dict)
        or not str(source.get("session_id") or "")
        or not str(source.get("summary_path") or "")
        or not isinstance(source.get("candidate_index"), int)
        or int(source["candidate_index"]) < 1
        or event.get("decision_basis") != "session-summary-candidate"
    ):
        raise ValueError("invalid belief candidate event")
    if event.get("event_id") != "belief-event-" + sha256_text("candidate-recorded\0" + candidate):
        raise ValueError("belief candidate event ID is not deterministic")
    format_timestamp(str(event.get("at") or ""))


def _validate_decision_event(event: dict[str, Any], state: dict[str, Any]) -> None:
    statement = event.get("user_statement")
    content = event.get("content")
    if (
        event.get("schema_version") != SCHEMA_VERSION
        or event.get("event") != "belief-user-decision"
        or event.get("candidate_id") != state["candidate_id"]
        or event.get("belief_id") != state["belief_id"]
        or event.get("from_status") != state["status"]
        or not isinstance(statement, str)
        or not statement.strip()
        or not isinstance(content, str)
        or not content.strip()
        or event.get("content_sha256") != sha256_text(content)
        or event.get("decision_basis") != "explicit-user-decision"
        or event.get("source") != state["source"]
    ):
        raise ValueError("invalid belief decision event")
    target, expected_content = _transition(
        str(event.get("decision") or ""),
        str(state["status"]),
        content if event.get("decision") == "revise" else None,
        str(state["content"]),
    )
    if event.get("to_status") != target or content != expected_content:
        raise ValueError("belief decision transition does not match the decision")
    identity = canonical_json({
        "candidate_id": state["candidate_id"],
        "from_status": state["status"],
        "decision": event["decision"],
        "user_statement": statement.strip(),
        "content": content,
    })
    if event.get("event_id") != "belief-event-" + sha256_text(identity):
        raise ValueError("belief decision event ID is not deterministic")
    format_timestamp(str(event.get("at") or ""))


def event_states(events: list[dict[str, Any]], *, strict: bool = True) -> dict[str, dict[str, Any]]:
    """Reduce the belief stream once; strict mode enforces the durable contract."""
    states: dict[str, dict[str, Any]] = {}
    event_ids: set[str] = set()
    for event in events:
        candidate = str(event.get("candidate_id") or "")
        event_id = str(event.get("event_id") or "")
        if strict:
            if not event_id or event_id in event_ids:
                raise ValueError("belief event IDs must be present and unique")
            event_ids.add(event_id)
        if not candidate:
            if strict:
                raise ValueError("belief event has no candidate_id")
            continue
        if event.get("event") == "belief-candidate-recorded":
            if strict:
                _validate_candidate_event(event)
                if candidate in states:
                    raise ValueError(f"duplicate candidate registration: {candidate}")
            states[candidate] = {
                "candidate_id": candidate,
                "belief_id": event.get("belief_id"),
                "status": "candidate",
                "content": event.get("content"),
                "source": event.get("source"),
                "events": [event],
                "current_event_id": event.get("event_id"),
                "last_user_statement": None,
            }
            continue
        if candidate not in states:
            if strict:
                raise ValueError(f"decision before candidate registration: {candidate}")
            continue
        current = states[candidate]
        if strict:
            _validate_decision_event(event, current)
        current["status"] = event.get("to_status") or current["status"]
        current["content"] = event.get("content")
        current["events"].append(event)
        current["current_event_id"] = event.get("event_id")
        current["last_user_statement"] = event.get("user_statement")
    return states


def register_candidates(
    workspace: Path,
    candidates: list[dict[str, Any]],
    at: str,
) -> tuple[int, list[dict[str, Any]]]:
    workspace = workspace.resolve()
    path = workspace / BELIEF_EVENT_PATH
    events = load_jsonl(path)
    event_states(events)
    known_ids = {str(event.get("event_id")) for event in events}
    appended = 0
    for candidate in candidates:
        event = candidate_event(candidate, at)
        if event["event_id"] in known_ids:
            continue
        append_jsonl(path, event)
        events.append(event)
        known_ids.add(event["event_id"])
        appended += 1
    event_states(events)
    return appended, events


def list_candidates(
    workspace: Path,
    candidates: list[dict[str, Any]],
    now: str | None = None,
) -> dict[str, Any]:
    at = format_timestamp(now)
    appended, events = register_candidates(workspace, candidates, at)
    states = event_states(events)
    return {
        "result": "ok",
        "candidate_events_appended": appended,
        "candidates": [states[candidate["candidate_id"]] for candidate in candidates],
    }


def belief_markdown(state: dict[str, Any]) -> str:
    lines = [
        f"# {state['belief_id']}\n",
        f"- 状态：`{state['status']}`",
        f"- 候选来源：`{state['source'].get('session_id')}`",
        f"- 候选记录：`{state['source'].get('summary_path')}`",
        f"- 当前内容 SHA-256：`{sha256_text(str(state['content']))}`",
        "",
        "## 当前内容",
        "",
        str(state["content"]),
        "",
        "## 决定历史",
        "",
    ]
    for event in state["events"][1:]:
        lines.append(
            f"- `{event['at']}` `{event.get('from_status')} -> {event.get('to_status')}` "
            f"决定：`{event.get('decision')}`；用户原话：{event.get('user_statement')}；事件：`{event['event_id']}`"
        )
    return "\n".join(lines).rstrip() + "\n"


def write_belief_view(workspace: Path, state: dict[str, Any]) -> Path | None:
    path = workspace.resolve() / BELIEF_DIR / f"{state['belief_id']}.md"
    if state["status"] in ACTIVE_BELIEF_STATUSES:
        atomic_write(path, belief_markdown(state))
        return path
    return None


def _duplicate_decision(state: dict[str, Any], decision: str, statement: str, revised_content: str | None) -> bool:
    revision = revised_content.strip() if revised_content and revised_content.strip() else None
    return any(
        event.get("event") == "belief-user-decision"
        and event.get("decision") == decision
        and str(event.get("user_statement") or "").strip() == statement
        and (decision != "revise" or event.get("content") == revision)
        for event in state["events"][1:]
    )


def decide_belief(
    workspace: Path,
    candidate: str,
    decision: str,
    user_statement: str,
    revised_content: str | None = None,
    at: str | None = None,
) -> dict[str, Any]:
    statement = user_statement.strip()
    if not statement:
        raise ValueError("--user-statement is required and must quote the user's explicit decision")
    workspace = workspace.resolve()
    path = workspace / BELIEF_EVENT_PATH
    events = load_jsonl(path)
    states = event_states(events)
    if candidate not in states:
        raise ValueError(f"unknown candidate_id: {candidate}")
    state = states[candidate]
    if _duplicate_decision(state, decision, statement, revised_content):
        belief_path = workspace / BELIEF_DIR / f"{state['belief_id']}.md"
        return {
            "result": "duplicate",
            "candidate_id": candidate,
            "status": state["status"],
            "belief_path": str(belief_path) if state["status"] in ACTIVE_BELIEF_STATUSES else None,
        }
    event = decision_event(state, decision, statement, revised_content, format_timestamp(at))
    if event["event_id"] in {item.get("event_id") for item in events}:
        return {
            "result": "duplicate",
            "candidate_id": candidate,
            "status": state["status"],
            "belief_path": str(workspace / BELIEF_DIR / f"{state['belief_id']}.md") if state["status"] in ACTIVE_BELIEF_STATUSES else None,
        }
    append_jsonl(path, event)
    events.append(event)
    updated = event_states(events)[candidate]
    belief_path = write_belief_view(workspace, updated)
    return {
        "result": "recorded",
        "candidate_id": candidate,
        "belief_id": updated["belief_id"],
        "previous_status": event["from_status"],
        "status": updated["status"],
        "decision": decision,
        "event_id": event["event_id"],
        "belief_path": str(belief_path) if belief_path else None,
        "next_action": "再次显式确认后进入 confirmed。" if updated["status"] == "provisional" else None,
    }


def rebuild_views(workspace: Path) -> dict[str, Any]:
    workspace = workspace.resolve()
    events = load_jsonl(workspace / BELIEF_EVENT_PATH)
    states = event_states(events)
    directory = workspace / BELIEF_DIR
    directory.mkdir(parents=True, exist_ok=True)
    expected = {
        str(state["belief_id"]): belief_markdown(state)
        for state in states.values()
        if state["status"] in ACTIVE_BELIEF_STATUSES
    }
    for belief_id, content in expected.items():
        atomic_write(directory / f"{belief_id}.md", content)
    return {"result": "rebuilt", "durable_beliefs": len(expected)}


def audit(workspace: Path) -> dict[str, Any]:
    workspace = workspace.resolve()
    events = load_jsonl(workspace / BELIEF_EVENT_PATH)
    checks: dict[str, bool] = {
        "event_ids_unique": len({event.get("event_id") for event in events}) == len(events)
        and all(event.get("event_id") for event in events),
        "event_schema": all(event.get("schema_version") == SCHEMA_VERSION for event in events),
        "content_hashes": all(
            event.get("content_sha256") == sha256_text(str(event.get("content", ""))) for event in events
        ),
    }
    states: dict[str, dict[str, Any]] = {}
    try:
        states = event_states(events)
        checks["transitions"] = True
    except (KeyError, TypeError, ValueError):
        checks["transitions"] = False
    directory = workspace / BELIEF_DIR
    belief_paths = {path.stem: path for path in directory.glob("*.md")} if directory.exists() else {}
    expected = {
        str(state["belief_id"]): state
        for state in states.values()
        if state["status"] in ACTIVE_BELIEF_STATUSES
    }
    checks["belief_views_exact"] = set(belief_paths) == set(expected)
    checks["unconfirmed_not_promoted"] = all(
        state["status"] not in {"candidate", "rejected"} or str(state["belief_id"]) not in belief_paths
        for state in states.values()
    )
    checks["belief_views_current"] = all(
        belief_paths[belief_id].read_text(encoding="utf-8") == belief_markdown(state)
        for belief_id, state in expected.items()
        if belief_id in belief_paths
    )
    failed = [name for name, passed in checks.items() if not passed]
    return {
        "result": "passed" if not failed else "failed",
        "checks": checks,
        "failed_checks": failed,
        "counts": {
            "events": len(events),
            "candidates": len(states),
            "durable_beliefs": len(expected),
        },
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    decide = subparsers.add_parser("decide")
    decide.add_argument("--workspace", type=Path, required=True)
    decide.add_argument("--candidate-id", required=True)
    decide.add_argument("--decision", choices=["confirm", "revise", "reject", "retire"], required=True)
    decide.add_argument("--user-statement", required=True)
    decide.add_argument("--revised-content")
    decide.add_argument("--at", help=argparse.SUPPRESS)
    rebuild = subparsers.add_parser("rebuild")
    rebuild.add_argument("--workspace", type=Path, required=True)
    audit_parser = subparsers.add_parser("audit")
    audit_parser.add_argument("--workspace", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    configure_console()
    args = parse_args(argv or sys.argv[1:])
    try:
        if args.command == "decide":
            result = decide_belief(
                args.workspace,
                args.candidate_id,
                args.decision,
                args.user_statement,
                args.revised_content,
                args.at,
            )
        elif args.command == "rebuild":
            result = rebuild_views(args.workspace)
        else:
            result = audit(args.workspace)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("result") not in {"error", "failed"} else 2
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"result": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
