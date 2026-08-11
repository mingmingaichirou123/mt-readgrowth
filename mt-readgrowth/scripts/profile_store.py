#!/usr/bin/env python3
"""Manage the append-only user-profile lifecycle and rebuildable current views."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

from record_session import safe_session_file, validate_session


SCHEMA_VERSION = 1
PROFILE_DIR = Path("knowledge/user-profile")
SETTINGS_FILE = "settings.json"
EVENTS_FILE = "profile-events.jsonl"
PROFILE_JSON_FILE = "profile.json"
PROFILE_MARKDOWN_FILE = "profile.md"

MEMORY_MODES = {"proactive-review", "explicit-only", "off"}
INITIALIZATION_MEMORY_MODE = "proactive-review"
UNCONFIGURED_MEMORY_MODE = "explicit-only"
INITIALIZATION_DECISION_BASIS = "workspace-initialization-default"
EXPLICIT_DECISION_BASIS = "explicit-user-decision"
CATEGORIES = {
    "background",
    "occupation",
    "goals",
    "interests",
    "reading_preferences",
    "communication_preferences",
    "constraints",
    "traits",
}
SCOPE_ORDER = ("discussion", "read-for-me", "recommendation")
USAGE_SCOPES = set(SCOPE_ORDER)
SENSITIVITIES = {"normal", "sensitive"}
ACTIVE_STATUSES = {"confirmed", "revised"}
TERMINAL_STATUSES = {"rejected", "retired"}

SECRET_PATTERNS = (
    re.compile(r"(?i)\bauthorization\s*:\s*\S+"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(
        r"(?i)\b(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|client[_ -]?secret|"
        r"password|passwd|cookie|private[_ -]?key|weread_api_key)\b\s*[:=]\s*\S+"
    ),
    re.compile(r"(?i)-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
)


class ProfileStoreFailure(ValueError):
    """A controlled profile-store validation failure."""


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    if not path.exists():
        return digest.hexdigest()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def format_timestamp(value: str | None) -> str:
    if value is None:
        parsed = datetime.now(timezone.utc)
    else:
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except (AttributeError, ValueError) as exc:
            raise ProfileStoreFailure("timestamp must be RFC3339") from exc
        if parsed.tzinfo is None:
            raise ProfileStoreFailure("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def initialization_memory_settings(at: str | None = None) -> dict[str, Any]:
    """Build the persisted, non-user-authored memory default for a new workspace."""
    return {
        "schema_version": SCHEMA_VERSION,
        "memory_mode": INITIALIZATION_MEMORY_MODE,
        "configured_at": format_timestamp(at),
        "decision_basis": INITIALIZATION_DECISION_BASIS,
    }


def _contains_secret(value: str) -> bool:
    return any(pattern.search(value) for pattern in SECRET_PATTERNS)


def _safe_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProfileStoreFailure(f"{field} is required")
    cleaned = value.strip()
    if _contains_secret(cleaned):
        raise ProfileStoreFailure("profile data contains prohibited secret-bearing material")
    return cleaned


def _profile_directory(workspace: Path, *, create: bool = True) -> tuple[Path, Path]:
    workspace_root = workspace.resolve()
    if not workspace_root.is_dir() or not (workspace_root / "workspace.yaml").is_file():
        raise ProfileStoreFailure("workspace is not initialized")
    knowledge_dir = workspace_root / "knowledge"
    profile_dir = workspace_root / PROFILE_DIR
    if create:
        knowledge_dir.mkdir(exist_ok=True)
        profile_dir.mkdir(exist_ok=True)
    for directory, label in ((knowledge_dir, "knowledge"), (profile_dir, "knowledge/user-profile")):
        if not directory.is_dir() or directory.resolve() != directory:
            raise ProfileStoreFailure(f"{label} must be a regular workspace directory, not a link")
    return workspace_root, profile_dir


def _safe_profile_file(profile_dir: Path, name: str) -> Path:
    path = profile_dir / name
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file() or path.resolve().parent != profile_dir:
            raise ProfileStoreFailure(f"{name} must be a regular file inside knowledge/user-profile")
    return path


def atomic_write(path: Path, content: str) -> None:
    profile_dir = path.parent
    _safe_profile_file(profile_dir, path.name)
    temporary = _safe_profile_file(profile_dir, path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)


def atomic_json(path: Path, value: Any) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    values: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ProfileStoreFailure(f"profile-events.jsonl line {line_number} is invalid JSON") from exc
        if not isinstance(value, dict):
            raise ProfileStoreFailure(f"profile-events.jsonl line {line_number} is not an object")
        values.append(value)
    return values


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    _safe_profile_file(path.parent, path.name)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(canonical_json(value) + "\n")


def _normalize_scopes(values: list[str]) -> list[str]:
    if not values or any(value not in USAGE_SCOPES for value in values):
        raise ProfileStoreFailure("usage_scopes must contain only discussion, read-for-me, or recommendation")
    return [scope for scope in SCOPE_ORDER if scope in set(values)]


def _normalize_expiry(value: str | None) -> str | None:
    return format_timestamp(value) if value else None


def _normalize_source(source: dict[str, Any]) -> dict[str, str]:
    if not isinstance(source, dict):
        raise ProfileStoreFailure("source must be an object")
    source_type = source.get("type")
    session_id = source.get("session_id")
    path = source.get("path")
    event_id = source.get("event_id")
    if source_type != "session-user-message":
        raise ProfileStoreFailure("profile source must be a session user message")
    for value, field in ((session_id, "source.session_id"), (path, "source.path"), (event_id, "source.event_id")):
        if not isinstance(value, str) or not value.strip():
            raise ProfileStoreFailure(f"{field} is required")
        if _contains_secret(value):
            raise ProfileStoreFailure("profile data contains prohibited secret-bearing material")
    normalized_path = Path(str(path).replace("\\", "/"))
    if normalized_path.is_absolute() or ".." in normalized_path.parts:
        raise ProfileStoreFailure("source.path must be a workspace-relative transcript path")
    parts = normalized_path.parts
    if len(parts) != 4 or parts[0] != "sessions" or not re.fullmatch(r"\d{4}", parts[1]) or parts[2] != session_id or parts[3] != "transcript.jsonl":
        raise ProfileStoreFailure("source.path must match sessions/<year>/<session-id>/transcript.jsonl")
    return {
        "type": "session-user-message",
        "session_id": str(session_id),
        "path": normalized_path.as_posix(),
        "event_id": str(event_id),
    }


def _validate_source_event(workspace: Path, source: dict[str, str]) -> None:
    session_dir = workspace / Path(source["path"]).parent
    try:
        resolved_session, _session_yaml, _content = validate_session(workspace, session_dir)
        transcript = safe_session_file(resolved_session, "transcript.jsonl", required=True)
    except ValueError as exc:
        raise ProfileStoreFailure("profile source session is invalid") from exc
    found = False
    try:
        lines = transcript.read_text(encoding="utf-8").splitlines()
        for line in lines:
            if not line.strip():
                continue
            event = json.loads(line)
            if isinstance(event, dict) and event.get("event_id") == source["event_id"]:
                found = event.get("role") == "user" and event.get("event_type") == "message"
                break
    except (OSError, json.JSONDecodeError) as exc:
        raise ProfileStoreFailure("profile source transcript is invalid") from exc
    if not found:
        raise ProfileStoreFailure("profile source event must exist and be a role=user message")


def _candidate_identity(source: dict[str, str], category: str, content: str) -> str:
    return canonical_json({"source": source, "category": category, "content": content})


def _candidate_event(
    *,
    source: dict[str, str],
    category: str,
    content: str,
    usage_scopes: list[str],
    sensitivity: str,
    expires_at: str | None,
    decision_basis: str,
    at: str,
) -> dict[str, Any]:
    identity = _candidate_identity(source, category, content)
    profile_item_id = "profile-" + sha256_text(identity)
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": "profile-event-" + sha256_text("candidate\0" + identity),
        "event": "profile-candidate-recorded",
        "at": at,
        "profile_item_id": profile_item_id,
        "from_status": None,
        "to_status": "candidate",
        "category": category,
        "content": content,
        "content_sha256": sha256_text(content),
        "evidence_type": "explicit-user-statement",
        "usage_scopes": usage_scopes,
        "sensitivity": sensitivity,
        "expires_at": expires_at,
        "source": source,
        "decision_basis": decision_basis,
    }


def _validate_candidate_event(event: dict[str, Any]) -> None:
    if event.get("schema_version") != SCHEMA_VERSION or event.get("event") != "profile-candidate-recorded":
        raise ProfileStoreFailure("profile candidate event has an invalid schema")
    content = _safe_text(event.get("content"), "content")
    category = event.get("category")
    if category not in CATEGORIES:
        raise ProfileStoreFailure("profile category is invalid")
    scopes = _normalize_scopes(event.get("usage_scopes") if isinstance(event.get("usage_scopes"), list) else [])
    sensitivity = event.get("sensitivity")
    if sensitivity not in SENSITIVITIES:
        raise ProfileStoreFailure("profile sensitivity is invalid")
    expires_at = _normalize_expiry(event.get("expires_at"))
    source = _normalize_source(event.get("source"))
    expected = _candidate_event(
        source=source,
        category=str(category),
        content=content,
        usage_scopes=scopes,
        sensitivity=str(sensitivity),
        expires_at=expires_at,
        decision_basis=str(event.get("decision_basis")),
        at=format_timestamp(event.get("at")),
    )
    if event.get("decision_basis") not in {"session-summary-profile-candidate", "explicit-user-request"}:
        raise ProfileStoreFailure("profile candidate decision_basis is invalid")
    for key in (
        "event_id",
        "profile_item_id",
        "from_status",
        "to_status",
        "content_sha256",
        "evidence_type",
        "usage_scopes",
        "expires_at",
        "source",
    ):
        if event.get(key) != expected.get(key):
            raise ProfileStoreFailure("profile candidate event failed integrity validation")


def _public_state(state: dict[str, Any]) -> dict[str, Any]:
    return {
        key: state[key]
        for key in (
            "profile_item_id",
            "status",
            "category",
            "content",
            "content_sha256",
            "evidence_type",
            "usage_scopes",
            "sensitivity",
            "expires_at",
            "source",
            "current_event_id",
            "updated_at",
        )
    }


def event_states(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    states: dict[str, dict[str, Any]] = {}
    event_ids: set[str] = set()
    for event in events:
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or event_id in event_ids:
            raise ProfileStoreFailure("profile event IDs must be unique")
        event_ids.add(event_id)
        if event.get("event") == "profile-candidate-recorded":
            _validate_candidate_event(event)
            profile_item_id = str(event["profile_item_id"])
            if profile_item_id in states:
                raise ProfileStoreFailure("profile item has more than one candidate event")
            states[profile_item_id] = {
                "profile_item_id": profile_item_id,
                "status": "candidate",
                "category": event["category"],
                "content": event["content"],
                "content_sha256": event["content_sha256"],
                "evidence_type": event["evidence_type"],
                "usage_scopes": event["usage_scopes"],
                "sensitivity": event["sensitivity"],
                "expires_at": event["expires_at"],
                "source": event["source"],
                "current_event_id": event_id,
                "updated_at": event["at"],
                "events": [event],
            }
            continue
        if event.get("event") != "profile-user-decision" or event.get("schema_version") != SCHEMA_VERSION:
            raise ProfileStoreFailure("profile decision event has an invalid schema")
        profile_item_id = event.get("profile_item_id")
        if profile_item_id not in states:
            raise ProfileStoreFailure("profile decision has no candidate event")
        state = states[str(profile_item_id)]
        current = state["status"]
        if current in TERMINAL_STATUSES or event.get("from_status") != current:
            raise ProfileStoreFailure("profile decision has an invalid transition")
        decision = event.get("decision")
        allowed = {
            ("candidate", "confirm"): "confirmed",
            ("candidate", "revise"): "revised",
            ("candidate", "reject"): "rejected",
            ("confirmed", "revise"): "revised",
            ("confirmed", "retire"): "retired",
            ("revised", "revise"): "revised",
            ("revised", "retire"): "retired",
        }
        target = allowed.get((current, decision))
        content = _safe_text(event.get("content"), "content")
        statement = _safe_text(event.get("user_statement"), "user_statement")
        if target is None or event.get("to_status") != target:
            raise ProfileStoreFailure("profile decision has an invalid transition")
        if event.get("decision_basis") != "explicit-user-decision" or event.get("content_sha256") != sha256_text(content):
            raise ProfileStoreFailure("profile decision failed integrity validation")
        expected_identity = canonical_json({
            "profile_item_id": profile_item_id,
            "previous_event_id": state["current_event_id"],
            "decision": decision,
            "user_statement": statement,
            "content": content,
        })
        if event_id != "profile-event-" + sha256_text("decision\0" + expected_identity):
            raise ProfileStoreFailure("profile decision event ID failed integrity validation")
        format_timestamp(event.get("at"))
        state.update({
            "status": target,
            "content": content,
            "content_sha256": event["content_sha256"],
            "current_event_id": event_id,
            "updated_at": event["at"],
        })
        state["events"].append(event)
    return states


def _profile_json(states: dict[str, dict[str, Any]], stream_sha256: str) -> dict[str, Any]:
    active = [_public_state(state) for state in states.values() if state["status"] in ACTIVE_STATUSES]
    active.sort(key=lambda item: (item["category"], item["profile_item_id"]))
    return {
        "schema_version": SCHEMA_VERSION,
        "source": "knowledge/user-profile/profile-events.jsonl",
        "profile_events_sha256": stream_sha256,
        "items": active,
    }


def _profile_markdown(profile: dict[str, Any]) -> str:
    lines = [
        "# 用户资料",
        "",
        f"- 当前有效资料：{len(profile['items'])} 条",
        f"- 事件流 SHA-256：`{profile['profile_events_sha256']}`",
        "- 真源：`knowledge/user-profile/profile-events.jsonl`",
        "",
    ]
    if not profile["items"]:
        lines.extend(["当前没有已确认且有效的用户资料。", ""])
        return "\n".join(lines)
    for item in profile["items"]:
        lines.extend([
            f"## {item['category']} · {item['profile_item_id']}",
            "",
            f"- 状态：`{item['status']}`",
            f"- 适用范围：{', '.join(f'`{scope}`' for scope in item['usage_scopes'])}",
            f"- 敏感度：`{item['sensitivity']}`",
            f"- 到期时间：`{item['expires_at']}`" if item["expires_at"] else "- 到期时间：无",
            f"- 更新时间：`{item['updated_at']}`",
            f"- 来源：`{item['source']['path']}#{item['source']['event_id']}`",
            f"- 当前事件：`{item['current_event_id']}`",
            f"- 内容 SHA-256：`{item['content_sha256']}`",
            "",
            item["content"],
            "",
        ])
    return "\n".join(lines).rstrip() + "\n"


def get_memory_settings(workspace: Path) -> dict[str, Any]:
    _workspace, profile_dir = _profile_directory(workspace)
    path = _safe_profile_file(profile_dir, SETTINGS_FILE)
    if not path.exists():
        return {
            "schema_version": SCHEMA_VERSION,
            "configured": False,
            "memory_mode": UNCONFIGURED_MEMORY_MODE,
            "decision_basis": "default-when-unconfigured",
        }
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProfileStoreFailure("settings.json is invalid JSON") from exc
    if not isinstance(value, dict):
        raise ProfileStoreFailure("settings.json must be an object")
    return validate_memory_settings(value)


def validate_memory_settings(value: dict[str, Any]) -> dict[str, Any]:
    """Validate either the initialization default or an explicit user mode decision."""
    decision_basis = value.get("decision_basis")
    if (
        value.get("schema_version") != SCHEMA_VERSION
        or value.get("memory_mode") not in MEMORY_MODES
        or decision_basis not in {INITIALIZATION_DECISION_BASIS, EXPLICIT_DECISION_BASIS}
    ):
        raise ProfileStoreFailure("settings.json has an invalid schema")
    if decision_basis == INITIALIZATION_DECISION_BASIS:
        if value.get("memory_mode") != INITIALIZATION_MEMORY_MODE or "user_statement" in value:
            raise ProfileStoreFailure("initialization memory settings must use the proactive-review default")
    else:
        _safe_text(value.get("user_statement"), "user_statement")
    format_timestamp(value.get("configured_at"))
    return {**value, "configured": True}


def set_memory_mode(workspace: Path, memory_mode: str, user_statement: str, at: str | None = None) -> dict[str, Any]:
    if memory_mode not in MEMORY_MODES:
        raise ProfileStoreFailure("memory_mode must be proactive-review, explicit-only, or off")
    statement = _safe_text(user_statement, "user_statement")
    _workspace, profile_dir = _profile_directory(workspace)
    current = get_memory_settings(workspace)
    if (
        current["configured"]
        and current["memory_mode"] == memory_mode
        and current.get("decision_basis") == EXPLICIT_DECISION_BASIS
    ):
        return {"result": "unchanged", **current}
    value = {
        "schema_version": SCHEMA_VERSION,
        "memory_mode": memory_mode,
        "configured_at": format_timestamp(at),
        "decision_basis": EXPLICIT_DECISION_BASIS,
        "user_statement": statement,
    }
    atomic_json(_safe_profile_file(profile_dir, SETTINGS_FILE), value)
    return {"result": "recorded", "configured": True, **value}


def rebuild_views(workspace: Path) -> dict[str, Any]:
    _workspace, profile_dir = _profile_directory(workspace)
    events_path = _safe_profile_file(profile_dir, EVENTS_FILE)
    events = load_jsonl(events_path)
    states = event_states(events)
    profile = _profile_json(states, file_sha256(events_path))
    atomic_json(_safe_profile_file(profile_dir, PROFILE_JSON_FILE), profile)
    atomic_write(_safe_profile_file(profile_dir, PROFILE_MARKDOWN_FILE), _profile_markdown(profile))
    return {
        "result": "rebuilt",
        "events": len(events),
        "active_items": len(profile["items"]),
        "profile_json": str(profile_dir / PROFILE_JSON_FILE),
        "profile_markdown": str(profile_dir / PROFILE_MARKDOWN_FILE),
    }


def validate_profile_candidate(
    workspace: Path,
    *,
    category: str,
    content: str,
    usage_scopes: list[str],
    source: dict[str, Any],
    sensitivity: str = "normal",
    expires_at: str | None = None,
    at: str | None = None,
    decision_basis: str = "session-summary-profile-candidate",
) -> dict[str, Any]:
    """Validate and normalize one candidate without appending or rebuilding views."""
    if category not in CATEGORIES:
        raise ProfileStoreFailure("unsupported profile category")
    cleaned_content = _safe_text(content, "content")
    scopes = _normalize_scopes(usage_scopes)
    if sensitivity not in SENSITIVITIES:
        raise ProfileStoreFailure("sensitivity must be normal or sensitive")
    if decision_basis not in {"session-summary-profile-candidate", "explicit-user-request"}:
        raise ProfileStoreFailure("candidate decision_basis is invalid")
    normalized_source = _normalize_source(source)
    workspace_root, profile_dir = _profile_directory(workspace)
    for name in (
        EVENTS_FILE,
        PROFILE_JSON_FILE,
        PROFILE_MARKDOWN_FILE,
        PROFILE_JSON_FILE + ".tmp",
        PROFILE_MARKDOWN_FILE + ".tmp",
    ):
        _safe_profile_file(profile_dir, name)
    _validate_source_event(workspace_root, normalized_source)
    return _candidate_event(
        source=normalized_source,
        category=category,
        content=cleaned_content,
        usage_scopes=scopes,
        sensitivity=sensitivity,
        expires_at=_normalize_expiry(expires_at),
        decision_basis=decision_basis,
        at=format_timestamp(at),
    )


def record_profile_candidate(
    workspace: Path,
    *,
    category: str,
    content: str,
    usage_scopes: list[str],
    source: dict[str, Any],
    sensitivity: str = "normal",
    expires_at: str | None = None,
    at: str | None = None,
    decision_basis: str = "session-summary-profile-candidate",
) -> dict[str, Any]:
    event = validate_profile_candidate(
        workspace,
        category=category,
        content=content,
        usage_scopes=usage_scopes,
        source=source,
        sensitivity=sensitivity,
        expires_at=expires_at,
        at=at,
        decision_basis=decision_basis,
    )
    workspace_root, profile_dir = _profile_directory(workspace)
    events_path = _safe_profile_file(profile_dir, EVENTS_FILE)
    events = load_jsonl(events_path)
    event_states(events)
    if event["event_id"] in {existing.get("event_id") for existing in events}:
        rebuild_views(workspace_root)
        return {
            "result": "duplicate",
            "event_id": event["event_id"],
            "profile_item_id": event["profile_item_id"],
            "status": event_states(events)[event["profile_item_id"]]["status"],
        }
    append_jsonl(events_path, event)
    rebuild_views(workspace_root)
    return {
        "result": "recorded",
        "event_id": event["event_id"],
        "profile_item_id": event["profile_item_id"],
        "status": "candidate",
    }


def _decision_content(state: dict[str, Any], decision: str, revised_content: str | None) -> tuple[str, str]:
    current = state["status"]
    if current in TERMINAL_STATUSES:
        raise ProfileStoreFailure(f"profile item is terminal: {current}")
    transitions = {
        ("candidate", "confirm"): "confirmed",
        ("candidate", "revise"): "revised",
        ("candidate", "reject"): "rejected",
        ("confirmed", "revise"): "revised",
        ("confirmed", "retire"): "retired",
        ("revised", "revise"): "revised",
        ("revised", "retire"): "retired",
    }
    target = transitions.get((current, decision))
    if target is None:
        raise ProfileStoreFailure(f"{decision} is not allowed from {current}")
    if decision == "revise":
        content = _safe_text(revised_content, "revised_content")
    else:
        content = state["content"]
    return target, content


def decide_profile(
    workspace: Path,
    profile_item_id: str,
    decision: str,
    user_statement: str,
    revised_content: str | None = None,
    at: str | None = None,
) -> dict[str, Any]:
    statement = _safe_text(user_statement, "user_statement")
    if decision not in {"confirm", "revise", "reject", "retire"}:
        raise ProfileStoreFailure("unsupported profile decision")
    workspace_root, profile_dir = _profile_directory(workspace)
    events_path = _safe_profile_file(profile_dir, EVENTS_FILE)
    events = load_jsonl(events_path)
    states = event_states(events)
    if profile_item_id not in states:
        raise ProfileStoreFailure("unknown profile item")
    state = states[profile_item_id]
    desired_content = _safe_text(revised_content, "revised_content") if decision == "revise" else state["content"]
    current_event = state["events"][-1]
    if (
        current_event.get("event") == "profile-user-decision"
        and current_event.get("decision") == decision
        and current_event.get("user_statement") == statement
        and current_event.get("content") == desired_content
    ):
        rebuild_views(workspace_root)
        return {
            "result": "duplicate",
            "event_id": current_event["event_id"],
            "profile_item_id": profile_item_id,
            "status": state["status"],
        }
    target, content = _decision_content(state, decision, revised_content)
    identity = canonical_json({
        "profile_item_id": profile_item_id,
        "previous_event_id": state["current_event_id"],
        "decision": decision,
        "user_statement": statement,
        "content": content,
    })
    event = {
        "schema_version": SCHEMA_VERSION,
        "event_id": "profile-event-" + sha256_text("decision\0" + identity),
        "event": "profile-user-decision",
        "at": format_timestamp(at),
        "profile_item_id": profile_item_id,
        "from_status": state["status"],
        "to_status": target,
        "decision": decision,
        "content": content,
        "content_sha256": sha256_text(content),
        "user_statement": statement,
        "decision_basis": "explicit-user-decision",
        "source": state["source"],
    }
    append_jsonl(events_path, event)
    events.append(event)
    updated = event_states(events)[profile_item_id]
    rebuild_views(workspace_root)
    return {
        "result": "recorded",
        "event_id": event["event_id"],
        "profile_item_id": profile_item_id,
        "previous_status": state["status"],
        "status": updated["status"],
        "decision": decision,
    }


def remember_profile(
    workspace: Path,
    *,
    category: str,
    content: str,
    usage_scopes: list[str],
    source: dict[str, Any],
    user_statement: str,
    sensitivity: str = "normal",
    expires_at: str | None = None,
    at: str | None = None,
) -> dict[str, Any]:
    _safe_text(user_statement, "user_statement")
    candidate = record_profile_candidate(
        workspace,
        category=category,
        content=content,
        usage_scopes=usage_scopes,
        source=source,
        sensitivity=sensitivity,
        expires_at=expires_at,
        at=at,
        decision_basis="explicit-user-request",
    )
    decision = decide_profile(
        workspace,
        candidate["profile_item_id"],
        "confirm",
        user_statement,
        at=at,
    )
    return decision


def list_profiles(workspace: Path) -> dict[str, Any]:
    _workspace, profile_dir = _profile_directory(workspace)
    events = load_jsonl(_safe_profile_file(profile_dir, EVENTS_FILE))
    states = event_states(events)
    items = [_public_state(state) for state in states.values()]
    items.sort(key=lambda item: (item["category"], item["profile_item_id"]))
    settings = get_memory_settings(workspace)
    return {
        "result": "ok",
        "memory_mode": settings["memory_mode"],
        "memory_mode_configured": settings["configured"],
        "active_count": sum(item["status"] in ACTIVE_STATUSES for item in items),
        "pending_count": sum(item["status"] == "candidate" for item in items),
        "items": items,
    }


def _settings_valid(workspace: Path) -> bool:
    try:
        get_memory_settings(workspace)
        return True
    except (OSError, ProfileStoreFailure):
        return False


def _sources_valid(workspace: Path, events: list[dict[str, Any]]) -> bool:
    try:
        seen: set[str] = set()
        for event in events:
            if event.get("event") != "profile-candidate-recorded":
                continue
            key = canonical_json(event.get("source"))
            if key in seen:
                continue
            seen.add(key)
            _validate_source_event(workspace, _normalize_source(event.get("source")))
        return True
    except (OSError, ProfileStoreFailure):
        return False


def audit(workspace: Path) -> dict[str, Any]:
    workspace_root, profile_dir = _profile_directory(workspace)
    events_path = _safe_profile_file(profile_dir, EVENTS_FILE)
    json_path = _safe_profile_file(profile_dir, PROFILE_JSON_FILE)
    markdown_path = _safe_profile_file(profile_dir, PROFILE_MARKDOWN_FILE)
    checks: dict[str, bool] = {"settings_valid": _settings_valid(workspace_root)}
    events: list[dict[str, Any]] = []
    states: dict[str, dict[str, Any]] = {}
    try:
        events = load_jsonl(events_path)
        states = event_states(events)
        checks["event_ids_unique"] = len({event["event_id"] for event in events}) == len(events)
        checks["event_schema_and_transitions"] = True
        checks["content_hashes"] = all(event.get("content_sha256") == sha256_text(str(event.get("content", ""))) for event in events)
        checks["secrets_absent"] = all(
            not _contains_secret(str(event.get(field, "")))
            for event in events
            for field in ("content", "user_statement")
        )
    except (OSError, ProfileStoreFailure):
        checks["event_ids_unique"] = False
        checks["event_schema_and_transitions"] = False
        checks["content_hashes"] = False
        checks["secrets_absent"] = False
    checks["sources_valid"] = _sources_valid(workspace_root, events) if checks["event_schema_and_transitions"] else False
    expected_json = _profile_json(states, file_sha256(events_path))
    expected_markdown = _profile_markdown(expected_json)
    if not events and not json_path.exists() and not markdown_path.exists():
        checks["profile_json_current"] = True
        checks["profile_markdown_current"] = True
    else:
        try:
            actual_json = json.loads(json_path.read_text(encoding="utf-8"))
            checks["profile_json_current"] = actual_json == expected_json
        except (OSError, json.JSONDecodeError):
            checks["profile_json_current"] = False
        try:
            checks["profile_markdown_current"] = markdown_path.read_text(encoding="utf-8") == expected_markdown
        except OSError:
            checks["profile_markdown_current"] = False
    active_ids = {state["profile_item_id"] for state in states.values() if state["status"] in ACTIVE_STATUSES}
    expected_ids = {item["profile_item_id"] for item in expected_json["items"]}
    checks["active_view_membership"] = active_ids == expected_ids
    failed = [name for name, passed in checks.items() if not passed]
    return {
        "result": "passed" if not failed else "failed",
        "checks": checks,
        "failed_checks": failed,
        "counts": {
            "events": len(events),
            "profile_items": len(states),
            "active_items": len(active_ids),
            "candidates": sum(state["status"] == "candidate" for state in states.values()),
        },
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    mode = subparsers.add_parser("mode")
    mode.add_argument("--workspace", type=Path, required=True)
    mode.add_argument("--memory-mode", choices=sorted(MEMORY_MODES), required=True)
    mode.add_argument("--user-statement", required=True)
    mode.add_argument("--at", help=argparse.SUPPRESS)

    record = subparsers.add_parser("record")
    record.add_argument("--workspace", type=Path, required=True)
    record.add_argument("--category", choices=sorted(CATEGORIES), required=True)
    record.add_argument("--content", required=True)
    record.add_argument("--usage-scope", action="append", choices=list(SCOPE_ORDER), required=True)
    record.add_argument("--sensitivity", choices=sorted(SENSITIVITIES), default="normal")
    record.add_argument("--expires-at")
    record.add_argument("--source-session-id", required=True)
    record.add_argument("--source-path", required=True)
    record.add_argument("--source-event-id", required=True)
    record.add_argument("--confirm", action="store_true")
    record.add_argument("--user-statement")
    record.add_argument("--at", help=argparse.SUPPRESS)

    listing = subparsers.add_parser("list")
    listing.add_argument("--workspace", type=Path, required=True)

    decide = subparsers.add_parser("decide")
    decide.add_argument("--workspace", type=Path, required=True)
    decide.add_argument("--profile-item-id", required=True)
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
        if args.command == "mode":
            result = set_memory_mode(args.workspace, args.memory_mode, args.user_statement, args.at)
        elif args.command == "record":
            source = {
                "type": "session-user-message",
                "session_id": args.source_session_id,
                "path": args.source_path,
                "event_id": args.source_event_id,
            }
            if args.confirm:
                result = remember_profile(
                    args.workspace,
                    category=args.category,
                    content=args.content,
                    usage_scopes=args.usage_scope,
                    source=source,
                    user_statement=args.user_statement or "",
                    sensitivity=args.sensitivity,
                    expires_at=args.expires_at,
                    at=args.at,
                )
            else:
                if args.user_statement:
                    raise ProfileStoreFailure("--user-statement is only used with --confirm")
                result = record_profile_candidate(
                    args.workspace,
                    category=args.category,
                    content=args.content,
                    usage_scopes=args.usage_scope,
                    source=source,
                    sensitivity=args.sensitivity,
                    expires_at=args.expires_at,
                    at=args.at,
                )
        elif args.command == "list":
            result = list_profiles(args.workspace)
        elif args.command == "decide":
            result = decide_profile(
                args.workspace,
                args.profile_item_id,
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
        return 0 if result.get("result") != "failed" else 2
    except (OSError, ProfileStoreFailure) as exc:
        print(json.dumps({"result": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
