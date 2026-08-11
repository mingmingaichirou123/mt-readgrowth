#!/usr/bin/env python3
"""Create append-only reading sessions and derived summaries."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


SESSION_STATUSES = {"pending", "generated", "confirmed", "failed"}
EVIDENCE_MODES = {"full", "restricted", "none"}
REQUIRED_EVIDENCE_CLASSES = ["作者明确表达", "作者立场推断", "AI延伸应用"]
SUMMARY_REQUIRED_FIELDS = [
    "question",
    "sources",
    "author_explicit",
    "author_inference",
    "ai_application",
    "user_confirmed",
    "disagreements",
    "candidate_insights",
    "unresolved",
    "actions",
]


def resolved_sessions_root(workspace: Path, *, create: bool = False) -> Path:
    workspace_root = workspace.resolve()
    sessions_root = workspace_root / "sessions"
    if create:
        sessions_root.mkdir(parents=True, exist_ok=True)
    if not sessions_root.is_dir():
        raise ValueError("workspace sessions directory is missing")
    resolved = sessions_root.resolve()
    if resolved != sessions_root:
        raise ValueError("workspace sessions directory must not be a symlink or Junction")
    return resolved


def safe_session_file(session_dir: Path, name: str, *, required: bool = False) -> Path:
    path = session_dir / name
    if required and not path.is_file():
        raise ValueError(f"{name} is missing")
    if path.exists() or path.is_symlink():
        if path.is_symlink() or path.resolve().parent != session_dir:
            raise ValueError(f"{name} must not escape the session directory")
        if not path.is_file():
            raise ValueError(f"{name} must be a regular file")
    return path


def yaml_field(content: str, key: str) -> str:
    matches = re.findall(rf"(?m)^{re.escape(key)}:\s*(.*?)\s*$", content)
    if len(matches) != 1:
        raise ValueError(f"session.yaml must contain exactly one {key}")
    return matches[0]


def yaml_json_string(content: str, key: str) -> str:
    raw = yaml_field(content, key)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"session.yaml has invalid {key}") from exc
    if not isinstance(value, str) or not value:
        raise ValueError(f"session.yaml has invalid {key}")
    return value


def validate_session(workspace: Path, session_dir: Path) -> tuple[Path, Path, str]:
    sessions_root = resolved_sessions_root(workspace)
    resolved_session = session_dir.resolve()
    try:
        relative = resolved_session.relative_to(sessions_root)
    except ValueError as exc:
        raise ValueError("session directory is outside workspace/sessions") from exc
    if len(relative.parts) != 2 or not re.fullmatch(r"\d{4}", relative.parts[0]):
        raise ValueError("session directory must match workspace/sessions/<year>/<session-id>")
    if not resolved_session.is_dir():
        raise ValueError("session directory is missing")

    session_yaml = safe_session_file(resolved_session, "session.yaml", required=True)
    content = session_yaml.read_text(encoding="utf-8")
    if yaml_field(content, "schema_version") != "1":
        raise ValueError("session.yaml has unsupported schema_version")
    session_id = yaml_json_string(content, "session_id")
    if session_id != relative.parts[1]:
        raise ValueError("session.yaml session_id does not match its directory")
    started_at = yaml_json_string(content, "started_at")
    if not started_at.startswith(relative.parts[0] + "-"):
        raise ValueError("session.yaml started_at does not match its year directory")
    yaml_field(content, "ended_at")
    yaml_field(content, "stable_book_ids")
    yaml_field(content, "focus_locators")
    yaml_field(content, "weread_highlight_ids")
    if yaml_json_string(content, "evidence_mode") not in EVIDENCE_MODES:
        raise ValueError("session.yaml has invalid evidence_mode")
    if yaml_json_string(content, "summary_status") not in SESSION_STATUSES:
        raise ValueError("session.yaml has invalid summary_status")
    return resolved_session, session_yaml, content


def start_session(workspace: Path, book_ids: list[str], locators: list[str], evidence_mode: str) -> dict[str, str]:
    if evidence_mode not in EVIDENCE_MODES:
        raise ValueError("invalid evidence_mode")
    now = datetime.now(timezone.utc)
    session_id = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    sessions_root = resolved_sessions_root(workspace, create=True)
    year_dir = sessions_root / now.strftime("%Y")
    year_dir.mkdir(exist_ok=True)
    if year_dir.resolve() != year_dir:
        raise ValueError("session year directory must not be a symlink or Junction")
    session_dir = year_dir / session_id
    session_dir.mkdir(parents=True, exist_ok=False)
    lines = [
        "schema_version: 1",
        f"session_id: {yaml_string(session_id)}",
        f"started_at: {yaml_string(now.isoformat(timespec='seconds').replace('+00:00', 'Z'))}",
        "ended_at: null",
        "stable_book_ids:",
    ]
    lines.extend(f"  - {yaml_string(value)}" for value in book_ids)
    if not book_ids:
        lines[-1] = "stable_book_ids: []"
    lines.append("focus_locators:")
    lines.extend(f"  - {yaml_string(value)}" for value in locators)
    if not locators:
        lines[-1] = "focus_locators: []"
    lines.extend([
        "weread_highlight_ids: []",
        f"evidence_mode: {yaml_string(evidence_mode)}",
        'summary_status: "pending"',
    ])
    (session_dir / "session.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    append_event(workspace, session_dir, "system", "session-start", {"evidence_mode": evidence_mode})
    return {"session_id": session_id, "session_dir": str(session_dir)}


def append_event(workspace: Path, session_dir: Path, role: str, event_type: str, payload: Any) -> dict[str, Any]:
    resolved_session, _session_yaml, _content = validate_session(workspace, session_dir)
    transcript = safe_session_file(resolved_session, "transcript.jsonl")
    at = utc_now()
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    event = {
        "schema_version": 1,
        "event_id": hashlib.sha256(f"{at}\0{role}\0{event_type}\0{canonical}\0{uuid.uuid4().hex}".encode("utf-8")).hexdigest(),
        "at": at,
        "role": role,
        "event_type": event_type,
        "payload": payload,
    }
    with transcript.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
    return event


def markdown_list(values: list[Any]) -> str:
    if not values:
        return "- 无\n"
    lines = []
    for value in values:
        if isinstance(value, dict):
            text = value.get("text") or value.get("claim") or value.get("content") or json.dumps(value, ensure_ascii=False)
            source = value.get("source") or value.get("locator")
            lines.append(f"- {text}" + (f"（来源：`{source}`）" if source else ""))
        else:
            lines.append(f"- {value}")
    return "\n".join(lines) + "\n"


def normalize_summary(summary: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(summary, dict):
        raise ValueError("summary JSON must be an object")
    missing = [key for key in SUMMARY_REQUIRED_FIELDS if key not in summary]
    if missing:
        raise ValueError("summary JSON is missing: " + ", ".join(missing))
    raw_candidates = summary.get("profile_candidates", [])
    if not isinstance(raw_candidates, list):
        raise ValueError("profile_candidates must be a list")
    if len(raw_candidates) > 3:
        raise ValueError("profile_candidates must contain at most three items")
    candidates: list[dict[str, Any]] = []
    for candidate in raw_candidates:
        if not isinstance(candidate, dict):
            raise ValueError("each profile candidate must be an object")
        source_event_id = candidate.get("source_event_id")
        if not isinstance(source_event_id, str) or not source_event_id.strip():
            raise ValueError("profile candidate source_event_id is required")
        candidates.append(
            {
                "category": candidate.get("category"),
                "content": candidate.get("content"),
                "usage_scopes": candidate.get("usage_scopes"),
                "sensitivity": candidate.get("sensitivity", "normal"),
                "expires_at": candidate.get("expires_at"),
                "source_event_id": source_event_id.strip(),
            }
        )
    return {**summary, "profile_candidates": candidates}


def _candidate_source(workspace: Path, session_dir: Path, event_id: str) -> dict[str, str]:
    relative = session_dir.relative_to(resolved_sessions_root(workspace))
    return {
        "type": "session-user-message",
        "session_id": session_dir.name,
        "path": (Path("sessions") / relative / "transcript.jsonl").as_posix(),
        "event_id": event_id,
    }


def _write_summary_document(workspace: Path, session_dir: Path, summary: dict[str, Any]) -> Path:
    resolved_session, session_yaml, content = validate_session(workspace, session_dir)
    path = safe_session_file(resolved_session, "summary.md")
    temporary = safe_session_file(resolved_session, "summary.md.tmp")
    temporary_yaml = safe_session_file(resolved_session, "session.yaml.tmp")
    safe_session_file(resolved_session, "transcript.jsonl")

    updated_content, count = re.subn(
        r"(?m)^summary_status:.*$", 'summary_status: "generated"', content, count=1
    )
    if count != 1:
        raise ValueError("session.yaml has no summary_status")
    updated_content, count = re.subn(
        r"(?m)^ended_at:.*$", f"ended_at: {yaml_string(utc_now())}", updated_content, count=1
    )
    if count != 1:
        raise ValueError("session.yaml has no ended_at")
    sections = [
        "# 阅读讨论总结\n",
        "## 用户原始问题\n\n" + str(summary["question"]) + "\n",
        "## 使用的书籍与原文位置\n\n" + markdown_list(summary["sources"]),
        "## 作者明确表达\n\n" + markdown_list(summary["author_explicit"]),
        "## 作者立场推断\n\n" + markdown_list(summary["author_inference"]),
        "## AI延伸应用\n\n" + markdown_list(summary["ai_application"]),
        "## 用户明确确认\n\n" + markdown_list(summary["user_confirmed"]),
        "## 分歧与适用条件\n\n" + markdown_list(summary["disagreements"]),
        "## 候选认知（未经用户确认）\n\n" + markdown_list(summary["candidate_insights"]),
        "## 候选用户资料（未经用户确认）\n\n" + markdown_list(summary["profile_candidates"]),
        "## 未解决问题\n\n" + markdown_list(summary["unresolved"]),
        "## 建议行动或后续阅读\n\n" + markdown_list(summary["actions"]),
    ]
    temporary.write_text("\n".join(sections), encoding="utf-8", newline="\n")
    temporary_yaml.write_text(updated_content, encoding="utf-8", newline="\n")
    temporary.replace(path)
    temporary_yaml.replace(session_yaml)
    append_event(
        workspace,
        resolved_session,
        "system",
        "summary-generated",
        {
            "path": "summary.md",
            "candidate_beliefs_promoted": False,
            "profile_candidates_present": len(summary["profile_candidates"]),
            "profile_candidates_confirmed": False,
        },
    )
    return path


def summarize_session(workspace: Path, session_dir: Path, summary: dict[str, Any]) -> dict[str, Any]:
    """Write a compatible summary and materialize review candidates only when configured."""
    normalized = normalize_summary(summary)
    resolved_session, _session_yaml, _content = validate_session(workspace, session_dir)
    safe_session_file(resolved_session, "summary.md")
    safe_session_file(resolved_session, "summary.md.tmp")
    safe_session_file(resolved_session, "session.yaml.tmp")
    safe_session_file(resolved_session, "transcript.jsonl")

    from personal_context import memory_settings

    settings = memory_settings(workspace)
    mode = str(settings["memory_mode"])
    candidate_at = utc_now()
    prepared: list[dict[str, Any]] = []
    if mode == "proactive-review":
        from profile_store import validate_profile_candidate

        for candidate in normalized["profile_candidates"]:
            source = _candidate_source(workspace, resolved_session, candidate["source_event_id"])
            validate_profile_candidate(
                workspace,
                category=candidate["category"],
                content=candidate["content"],
                usage_scopes=candidate["usage_scopes"],
                source=source,
                sensitivity=candidate["sensitivity"],
                expires_at=candidate["expires_at"],
                at=candidate_at,
            )
            prepared.append({**candidate, "source": source})

    summary_path = _write_summary_document(workspace, resolved_session, normalized)
    materialized: list[dict[str, Any]] = []
    if mode == "proactive-review":
        from profile_store import record_profile_candidate

        for index, candidate in enumerate(prepared, start=1):
            recorded = record_profile_candidate(
                workspace,
                category=candidate["category"],
                content=candidate["content"],
                usage_scopes=candidate["usage_scopes"],
                source=candidate["source"],
                sensitivity=candidate["sensitivity"],
                expires_at=candidate["expires_at"],
                at=candidate_at,
            )
            materialized.append(
                {
                    "index": index,
                    "profile_item_id": recorded["profile_item_id"],
                    "event_id": recorded["event_id"],
                    "status": recorded["status"],
                    "result": recorded["result"],
                    "category": candidate["category"],
                    "content": candidate["content"],
                    "usage_scopes": candidate["usage_scopes"],
                    "sensitivity": candidate["sensitivity"],
                    "expires_at": candidate["expires_at"],
                }
            )

    suppression_basis = None
    if mode == "explicit-only":
        suppression_basis = "explicit-only-no-auto-materialization"
    elif mode == "off":
        suppression_basis = "memory-mode-off"
    review = {
        "schema_version": 1,
        "memory_mode": mode,
        "memory_mode_configured": bool(settings["configured"]),
        "candidate_count": len(normalized["profile_candidates"]),
        "materialized_candidates": materialized,
        "review_required": mode == "proactive-review" and bool(materialized),
        "suppression_basis": suppression_basis,
        "authorization_granted": False,
        "decision_options": {
            "save": "confirm",
            "revise": "revise",
            "ignore": "reject",
        },
        "unmentioned_candidates": "remain-candidate",
    }
    return {"summary_path": str(summary_path), "profile_review": review}


def write_summary(workspace: Path, session_dir: Path, summary: dict[str, Any]) -> Path:
    """Compatibility wrapper returning the historical Path result."""
    return Path(summarize_session(workspace, session_dir, summary)["summary_path"])


def discussion_context(
    workspace: Path,
    session_dir: Path,
    *,
    confirm: bool,
    include_source_ids: list[str] | tuple[str, ...] = (),
    exclude_source_ids: list[str] | tuple[str, ...] = (),
    now: str | None = None,
) -> dict[str, Any]:
    """Preview or atomically freeze shared personal context inside one legal session."""
    resolved_session, _session_yaml, _content = validate_session(workspace, session_dir)
    context_path = safe_session_file(resolved_session, "context.json")
    temporary = safe_session_file(resolved_session, "context.json.tmp")
    safe_session_file(resolved_session, "transcript.jsonl")

    from personal_context import (
        assemble_personal_context,
        canonical_json,
        select_personal_context,
        sha256_text,
    )

    packet = assemble_personal_context(workspace, route="discussion", now=now)
    selected = select_personal_context(
        packet,
        confirm_defaults=confirm,
        include_source_ids=include_source_ids,
        exclude_source_ids=exclude_source_ids,
    )
    context = {
        "schema_version": 1,
        "route": "discussion",
        "session_id": resolved_session.name,
        "assembled_at": packet["assembled_at"],
        "context_review_confirmed": confirm,
        "memory_mode": packet["memory_mode"],
        "memory_mode_configured": packet["memory_mode_configured"],
        "source_snapshots": packet["source_snapshots"],
        "source_bundle_sha256": packet["source_bundle_sha256"],
        "available_sources": packet["available_sources"],
        "selected_sources": selected,
        "excluded_sources": packet["excluded_sources"],
        "selection": {
            "include_source_ids": sorted(set(include_source_ids)),
            "exclude_source_ids": sorted(set(exclude_source_ids)),
            "defaults_confirmed": confirm,
        },
        "evidence_policy": {
            "required_labels": REQUIRED_EVIDENCE_CLASSES,
            "profile_context_role": ["explanation-angle", "examples", "application-mapping"],
            "personal_context_cannot_change_evidence_classification": True,
        },
    }
    context["context_sha256"] = sha256_text(canonical_json(context))
    if not confirm:
        return {"result": "preview", "context_path": None, "context": context}

    serialized = json.dumps(context, ensure_ascii=False, indent=2) + "\n"
    try:
        temporary.write_text(serialized, encoding="utf-8", newline="\n")
        temporary.replace(context_path)
    finally:
        if temporary.exists() and temporary.is_file() and not temporary.is_symlink():
            temporary.unlink()
    return {"result": "frozen", "context_path": str(context_path), "context": context}


def load_discussion_context(workspace: Path, session_dir: Path) -> dict[str, Any]:
    """Load and validate a confirmed frozen discussion context without following links."""
    resolved_session, _session_yaml, _content = validate_session(workspace, session_dir)
    context_path = safe_session_file(resolved_session, "context.json", required=True)
    try:
        value = json.loads(context_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("context.json is invalid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("context.json must be an object")

    from personal_context import canonical_json, sha256_text

    stored_hash = value.get("context_sha256")
    unhashed = {key: item for key, item in value.items() if key != "context_sha256"}
    if not isinstance(stored_hash, str) or stored_hash != sha256_text(canonical_json(unhashed)):
        raise ValueError("context.json failed hash validation")
    if (
        value.get("schema_version") != 1
        or value.get("route") != "discussion"
        or value.get("session_id") != resolved_session.name
        or value.get("context_review_confirmed") is not True
    ):
        raise ValueError("context.json is not a confirmed context for this discussion session")
    evidence_policy = value.get("evidence_policy")
    if (
        not isinstance(evidence_policy, dict)
        or evidence_policy.get("required_labels") != REQUIRED_EVIDENCE_CLASSES
        or evidence_policy.get("personal_context_cannot_change_evidence_classification") is not True
    ):
        raise ValueError("context.json has an invalid evidence policy")
    available = value.get("available_sources")
    selected = value.get("selected_sources")
    excluded = value.get("excluded_sources")
    snapshots = value.get("source_snapshots")
    if not all(isinstance(item, list) for item in (available, selected, excluded)) or not isinstance(snapshots, dict):
        raise ValueError("context.json has an invalid source packet")
    by_id: dict[str, dict[str, Any]] = {}
    for source in available:
        if not isinstance(source, dict):
            raise ValueError("context.json has an invalid available source")
        source_id = str(source.get("source_id") or "")
        if not source_id or source_id in by_id:
            raise ValueError("context.json available source IDs must be non-empty and unique")
        if source.get("source_type") == "user-profile" and source.get("status") not in {"confirmed", "revised"}:
            raise ValueError("context.json must not make a profile candidate available")
        by_id[source_id] = source
    selected_ids: set[str] = set()
    for source in selected:
        if not isinstance(source, dict):
            raise ValueError("context.json has an invalid selected source")
        source_id = str(source.get("source_id") or "")
        if not source_id or source_id in selected_ids or source_id not in by_id:
            raise ValueError("context.json selected sources must be unique members of available_sources")
        available_source = by_id[source_id]
        for key in ("source_type", "status", "content", "source"):
            if source.get(key) != available_source.get(key):
                raise ValueError("context.json selected source differs from its available source")
        if (
            available_source.get("selected_by_default") is not True
            and source.get("selection_basis") != "explicit-user-context-confirmation"
        ):
            raise ValueError("context.json selected an opt-in source without explicit confirmation")
        selected_ids.add(source_id)
    semantic = {
        "route": "discussion",
        "memory_mode": value.get("memory_mode"),
        "memory_mode_configured": value.get("memory_mode_configured"),
        "source_snapshots": snapshots,
        "available_sources": available,
        "excluded_sources": excluded,
    }
    if value.get("source_bundle_sha256") != sha256_text(canonical_json(semantic)):
        raise ValueError("context.json source bundle failed validation")
    return value


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    start = subparsers.add_parser("start")
    start.add_argument("--workspace", type=Path, required=True)
    start.add_argument("--book-id", action="append", default=[])
    start.add_argument("--focus-locator", action="append", default=[])
    start.add_argument("--evidence-mode", choices=["full", "restricted", "none"], default="none")
    append = subparsers.add_parser("append")
    append.add_argument("--workspace", type=Path, required=True)
    append.add_argument("--session-dir", type=Path, required=True)
    append.add_argument("--role", choices=["user", "assistant", "system", "tool"], required=True)
    append.add_argument("--event-type", default="message")
    append.add_argument("--content", required=True)
    summarize = subparsers.add_parser("summarize")
    summarize.add_argument("--workspace", type=Path, required=True)
    summarize.add_argument("--session-dir", type=Path, required=True)
    summarize.add_argument("--summary-json", type=Path, required=True)
    context = subparsers.add_parser("context")
    context.add_argument("--workspace", type=Path, required=True)
    context.add_argument("--session-dir", type=Path, required=True)
    context.add_argument("--confirm", action="store_true")
    context.add_argument("--include-source-id", action="append", default=[])
    context.add_argument("--exclude-source-id", action="append", default=[])
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    configure_console()
    args = parse_args(argv or sys.argv[1:])
    try:
        if args.command == "start":
            result = start_session(args.workspace, args.book_id, args.focus_locator, args.evidence_mode)
        elif args.command == "append":
            result = append_event(
                args.workspace,
                args.session_dir,
                args.role,
                args.event_type,
                {"content": args.content},
            )
        elif args.command == "summarize":
            summary = json.loads(args.summary_json.read_text(encoding="utf-8"))
            result = summarize_session(args.workspace, args.session_dir, summary)
        else:
            result = discussion_context(
                args.workspace,
                args.session_dir,
                confirm=args.confirm,
                include_source_ids=args.include_source_id,
                exclude_source_ids=args.exclude_source_id,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"result": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
