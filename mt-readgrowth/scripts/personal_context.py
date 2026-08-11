#!/usr/bin/env python3
"""Assemble read-only, auditable personal context for reading tasks."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from belief_store import event_states as belief_event_states
from profile_store import (
    SECRET_PATTERNS,
    UNCONFIGURED_MEMORY_MODE,
    ProfileStoreFailure,
    event_states as profile_event_states,
    load_jsonl as load_profile_events,
    validate_memory_settings,
)


SCHEMA_VERSION = 1
ROUTES = {"discussion", "read-for-me", "recommendation"}
PROFILE_EVENT_PATH = Path("knowledge/user-profile/profile-events.jsonl")
PROFILE_SETTINGS_PATH = Path("knowledge/user-profile/settings.json")
BELIEF_EVENT_PATH = Path("knowledge/belief-events.jsonl")
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
ACTIVE_PROFILE_STATUSES = {"confirmed", "revised"}
DEFAULT_BELIEF_STATUSES = {"confirmed", "revised"}
EXPLICIT_BELIEF_STATUSES = {"candidate", "provisional"}
TERMINAL_BELIEF_STATUSES = {"rejected", "retired"}
SENSITIVE_CONTEXT = re.compile(
    r"(?i)(api[ _-]?key|access[ _-]?token|authorization|credential|secret|password|密钥|密码|令牌)"
)
PROCESS_CONTEXT = re.compile(
    r"(?i)(验收|体验可用|节点\s*\d|跳过.{0,6}讨论|流程.{0,8}通过|skill|同步.{0,8}成功|导入.{0,8}成功)"
)


class PersonalContextFailure(ValueError):
    """A controlled read-only context validation failure."""


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


def _parse_timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise PersonalContextFailure(f"{field} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise PersonalContextFailure(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _contains_secret(value: str) -> bool:
    return any(pattern.search(value) for pattern in SECRET_PATTERNS)


def _workspace_root(workspace: Path) -> Path:
    root = workspace.resolve()
    if not root.is_dir():
        raise PersonalContextFailure("reading workspace does not exist")
    return root


def _regular_directory(path: Path, label: str) -> None:
    if not path.exists():
        return
    if not path.is_dir() or path.is_symlink() or path.resolve() != path:
        raise PersonalContextFailure(f"{label} must be a regular workspace directory, not a link")


def _regular_file(path: Path, parent: Path, label: str) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or not path.is_file() or path.resolve().parent != parent.resolve():
        raise PersonalContextFailure(f"{label} must be a regular workspace file, not a link")


def _snapshot(path: Path, workspace: Path, *, loaded: bool = True) -> dict[str, Any]:
    relative = path.relative_to(workspace).as_posix()
    if not loaded:
        return {
            "path": relative,
            "loaded": False,
            "exists": None,
            "sha256": None,
            "reason": "memory-mode-off",
        }
    return {
        "path": relative,
        "loaded": True,
        "exists": path.is_file(),
        "sha256": file_sha256(path),
    }


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PersonalContextFailure(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise PersonalContextFailure(f"{label} must be a JSON object")
    return value


def _memory_settings(workspace: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    profile_dir = workspace / "knowledge" / "user-profile"
    settings_path = workspace / PROFILE_SETTINGS_PATH
    _regular_directory(profile_dir, "knowledge/user-profile")
    _regular_file(settings_path, profile_dir, "settings.json")
    snapshot = _snapshot(settings_path, workspace)
    if not settings_path.exists():
        return {
            "schema_version": SCHEMA_VERSION,
            "configured": False,
            "memory_mode": UNCONFIGURED_MEMORY_MODE,
            "decision_basis": "default-when-unconfigured",
        }, snapshot
    value = _load_json_object(settings_path, "settings.json")
    try:
        settings = validate_memory_settings(value)
    except ProfileStoreFailure as exc:
        raise PersonalContextFailure("settings.json has an invalid schema") from exc
    return settings, snapshot


def memory_settings(workspace: Path) -> dict[str, Any]:
    """Resolve the effective memory mode without loading profile or belief state."""
    workspace = _workspace_root(workspace)
    settings, snapshot = _memory_settings(workspace)
    return {**settings, "source_snapshot": snapshot}


def _profile_source(state: dict[str, Any], route: str) -> dict[str, Any]:
    profile_item_id = str(state["profile_item_id"])
    return {
        "source_id": f"profile:{profile_item_id}",
        "source_type": "user-profile",
        "status": state["status"],
        "category": state["category"],
        "content": state["content"],
        "content_sha256": state["content_sha256"],
        "evidence_type": state["evidence_type"],
        "usage_scope": route,
        "usage_scopes": list(state["usage_scopes"]),
        "sensitivity": state["sensitivity"],
        "expires_at": state["expires_at"],
        "source": {
            "path": PROFILE_EVENT_PATH.as_posix(),
            "event_id": state["current_event_id"],
            "profile_item_id": profile_item_id,
            "original_source": state["source"],
        },
    }


def _profile_source_manifest(workspace: Path, states: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    manifest: dict[str, str] = {}
    for state in states.values():
        source = state.get("source")
        if not isinstance(source, dict):
            raise PersonalContextFailure("profile source is invalid")
        relative = Path(str(source.get("path") or ""))
        if relative.is_absolute() or ".." in relative.parts:
            raise PersonalContextFailure("profile source path escapes the reading workspace")
        transcript = workspace / relative
        if transcript.is_symlink() or not transcript.is_file():
            raise PersonalContextFailure("profile source transcript is missing or linked")
        try:
            transcript.resolve().relative_to((workspace / "sessions").resolve())
        except ValueError as exc:
            raise PersonalContextFailure("profile source transcript escapes workspace/sessions") from exc
        source_event_id = str(source.get("event_id") or "")
        found = False
        for event in _load_jsonl(transcript, relative.as_posix()):
            if event.get("event_id") == source_event_id:
                found = event.get("role") == "user" and event.get("event_type") == "message"
                break
        if not found:
            raise PersonalContextFailure("profile source event must exist and be a user message")
        manifest[relative.as_posix()] = file_sha256(transcript)
    return [{"path": path, "sha256": manifest[path]} for path in sorted(manifest)]


def _load_profile_sources(
    workspace: Path,
    route: str,
    now: datetime,
    memory_mode: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    events_path = workspace / PROFILE_EVENT_PATH
    if memory_mode == "off":
        return [], [], _snapshot(events_path, workspace, loaded=False)
    profile_dir = events_path.parent
    _regular_directory(profile_dir, "knowledge/user-profile")
    _regular_file(events_path, profile_dir, "profile-events.jsonl")
    try:
        states = profile_event_states(load_profile_events(events_path))
    except (OSError, ProfileStoreFailure) as exc:
        raise PersonalContextFailure("profile event stream failed integrity validation") from exc
    source_manifest = _profile_source_manifest(workspace, states)
    available: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for profile_item_id in sorted(states):
        state = states[profile_item_id]
        source = _profile_source(state, route)
        if state["status"] not in ACTIVE_PROFILE_STATUSES:
            excluded.append({**source, "selected_by_default": False, "requires_explicit_selection": False, "exclusion_basis": "profile-status-not-active"})
            continue
        if route not in state["usage_scopes"]:
            excluded.append({**source, "selected_by_default": False, "requires_explicit_selection": False, "exclusion_basis": "profile-scope-mismatch"})
            continue
        expires_at = state.get("expires_at")
        if expires_at and _parse_timestamp(str(expires_at), "profile expires_at") <= now:
            excluded.append({**source, "selected_by_default": False, "requires_explicit_selection": False, "exclusion_basis": "profile-expired"})
            continue
        if state["sensitivity"] == "sensitive":
            available.append({
                **source,
                "selected_by_default": False,
                "requires_explicit_selection": True,
                "selection_basis": "sensitive-profile-explicit-selection-required",
            })
            continue
        available.append({
            **source,
            "selected_by_default": True,
            "requires_explicit_selection": False,
            "selection_basis": "confirmed-profile-scope-match",
        })
    snapshot = _snapshot(events_path, workspace)
    snapshot["source_files"] = source_manifest
    return available, excluded, snapshot


def _load_jsonl(path: Path, label: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    values: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise PersonalContextFailure(f"{label} could not be read") from exc
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PersonalContextFailure(f"{label} line {number} is invalid JSON") from exc
        if not isinstance(value, dict):
            raise PersonalContextFailure(f"{label} line {number} is not an object")
        values.append(value)
    return values


def belief_states(workspace: Path) -> list[dict[str, Any]]:
    """Rebuild current belief states without writing or materializing views."""
    workspace = _workspace_root(workspace)
    path = workspace / BELIEF_EVENT_PATH
    _regular_directory(path.parent, "knowledge")
    _regular_file(path, path.parent, "belief-events.jsonl")
    events = _load_jsonl(path, "belief-events.jsonl")
    event_ids = [str(event.get("event_id") or "") for event in events if event.get("event_id")]
    if len(event_ids) != len(set(event_ids)):
        raise PersonalContextFailure("belief event IDs must be unique")
    try:
        states = belief_event_states(events, strict=False)
    except (KeyError, TypeError, ValueError) as exc:
        raise PersonalContextFailure("belief event stream failed state reconstruction") from exc
    values = [
        {
            "candidate_id": state["candidate_id"],
            "belief_id": state.get("belief_id"),
            "status": state["status"],
            "content": state.get("content"),
            "source": state.get("source"),
            "event_id": state.get("current_event_id"),
            "user_statement": state.get("last_user_statement"),
        }
        for state in states.values()
    ]
    return sorted(values, key=lambda item: str(item.get("belief_id") or item["candidate_id"]))


def _belief_sources(workspace: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    path = workspace / BELIEF_EVENT_PATH
    _regular_directory(path.parent, "knowledge")
    _regular_file(path, path.parent, "belief-events.jsonl")
    available: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for state in belief_states(workspace):
        status = str(state.get("status") or "candidate")
        content = str(state.get("content") or "").strip()
        source_id = f"belief:{state.get('belief_id') or state['candidate_id']}"
        base = {
            "source_id": source_id,
            "source_type": "candidate-belief" if status == "candidate" else "durable-belief",
            "status": status,
            "content": content,
            "source": {
                "path": BELIEF_EVENT_PATH.as_posix(),
                "event_id": state.get("event_id"),
                "candidate_id": state.get("candidate_id"),
                "belief_id": state.get("belief_id"),
            },
        }
        if not content or SENSITIVE_CONTEXT.search(content):
            excluded.append({
                "source_id": source_id,
                "source_type": base["source_type"],
                "status": status,
                "source": base["source"],
                "selected_by_default": False,
                "requires_explicit_selection": False,
                "exclusion_basis": "belief-content-not-safe",
            })
        elif status in DEFAULT_BELIEF_STATUSES:
            available.append({
                **base,
                "selected_by_default": True,
                "requires_explicit_selection": False,
                "selection_basis": "confirmed-belief-status",
            })
        elif status in EXPLICIT_BELIEF_STATUSES:
            available.append({
                **base,
                "selected_by_default": False,
                "requires_explicit_selection": True,
                "selection_basis": "belief-explicit-selection-required",
            })
        else:
            excluded.append({
                **base,
                "selected_by_default": False,
                "requires_explicit_selection": False,
                "exclusion_basis": "belief-status-not-usable",
            })
    return available, excluded, _snapshot(path, workspace)


def historical_session_sources(workspace: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return safe historical user statements and an exact read-only source snapshot."""
    workspace = _workspace_root(workspace)
    sessions_root = workspace / "sessions"
    _regular_directory(sessions_root, "sessions")
    values: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    if sessions_root.exists():
        for transcript in sorted(sessions_root.glob("*/*/transcript.jsonl")):
            if transcript.is_symlink() or not transcript.is_file():
                raise PersonalContextFailure("historical transcript must be a regular workspace file")
            try:
                transcript.resolve().relative_to(sessions_root.resolve())
            except ValueError as exc:
                raise PersonalContextFailure("historical transcript escapes workspace/sessions") from exc
            relative = transcript.relative_to(workspace).as_posix()
            sources.append({"path": relative, "sha256": file_sha256(transcript)})
            for event in _load_jsonl(transcript, relative):
                if event.get("role") != "user" or event.get("event_type") != "message":
                    continue
                payload = event.get("payload")
                if not isinstance(payload, dict):
                    continue
                for index, line in enumerate(re.split(r"[\r\n]+", str(payload.get("content") or "")), start=1):
                    content = line.strip()
                    if not content or SENSITIVE_CONTEXT.search(content) or PROCESS_CONTEXT.search(content):
                        continue
                    source_event_id = str(event.get("event_id") or sha256_text(canonical_json(event)))
                    values.append({
                        "source_id": f"session:{source_event_id}:{index}",
                        "source_type": "historical-session-user-statement",
                        "status": "historical",
                        "content": content,
                        "source": {
                            "path": relative,
                            "event_id": event.get("event_id"),
                            "at": event.get("at"),
                            "line": index,
                        },
                        "selected_by_default": False,
                        "requires_explicit_selection": True,
                        "selection_basis": "historical-session-explicit-selection-required",
                    })
    manifest_sha256 = sha256_text(canonical_json(sources))
    return values, {
        "loaded": True,
        "sources": sources,
        "sha256": manifest_sha256,
    }


def _current_sources(values: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, dict):
            raise PersonalContextFailure("current context source must be an object")
        source = json.loads(canonical_json(raw))
        source_id = str(source.get("source_id") or "")
        content = str(source.get("content") or "").strip()
        if not source_id or source_id in seen:
            raise PersonalContextFailure("current context source IDs must be non-empty and unique")
        if not content:
            raise PersonalContextFailure("current context source content cannot be empty")
        if SENSITIVE_CONTEXT.search(content):
            raise PersonalContextFailure("current context source appears to contain credentials or secret-bearing text")
        seen.add(source_id)
        result.append(source)
    return result


def assemble_personal_context(
    workspace: Path,
    *,
    route: str,
    current_sources: Iterable[dict[str, Any]] = (),
    include_history: bool = True,
    now: str | None = None,
) -> dict[str, Any]:
    """Build an auditable source packet without creating, replacing, or appending files."""
    if route not in ROUTES:
        raise PersonalContextFailure("route must be discussion, read-for-me, or recommendation")
    workspace = _workspace_root(workspace)
    assembled_at = _parse_timestamp(now, "now") if now else datetime.now(timezone.utc)
    settings, settings_snapshot = _memory_settings(workspace)
    profiles, profile_excluded, profile_snapshot = _load_profile_sources(
        workspace,
        route,
        assembled_at,
        str(settings["memory_mode"]),
    )
    beliefs, belief_excluded, belief_snapshot = _belief_sources(workspace)
    if include_history:
        history, history_snapshot = historical_session_sources(workspace)
    else:
        history, history_snapshot = [], {"loaded": False, "sources": [], "sha256": None}
    available = _current_sources(current_sources) + profiles + beliefs + history
    by_id: dict[str, dict[str, Any]] = {}
    for source in available:
        source_id = str(source["source_id"])
        if source_id in by_id:
            raise PersonalContextFailure(f"duplicate personal context source ID: {source_id}")
        by_id[source_id] = source
    available = [by_id[source_id] for source_id in sorted(by_id)]
    excluded = sorted(profile_excluded + belief_excluded, key=lambda item: str(item["source_id"]))
    source_snapshots = {
        "profile_settings": settings_snapshot,
        "profile_events": profile_snapshot,
        "belief_events": belief_snapshot,
        "historical_sessions": history_snapshot,
    }
    semantic = {
        "route": route,
        "memory_mode": settings["memory_mode"],
        "memory_mode_configured": settings["configured"],
        "source_snapshots": source_snapshots,
        "available_sources": available,
        "excluded_sources": excluded,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "route": route,
        "assembled_at": assembled_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "memory_mode": settings["memory_mode"],
        "memory_mode_configured": settings["configured"],
        "source_snapshots": source_snapshots,
        "available_sources": available,
        "excluded_sources": excluded,
        "source_bundle_sha256": sha256_text(canonical_json(semantic)),
    }


def select_personal_context(
    packet: dict[str, Any],
    *,
    confirm_defaults: bool,
    include_source_ids: Iterable[str] = (),
    exclude_source_ids: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Apply task-level selection without reimplementing profile or belief status rules."""
    if not isinstance(packet, dict) or packet.get("route") not in ROUTES:
        raise PersonalContextFailure("personal context packet is invalid")
    available = packet.get("available_sources")
    if not isinstance(available, list):
        raise PersonalContextFailure("personal context packet has invalid available_sources")
    by_id: dict[str, dict[str, Any]] = {}
    for source in available:
        if not isinstance(source, dict):
            raise PersonalContextFailure("personal context source must be an object")
        source_id = str(source.get("source_id") or "")
        if not source_id or source_id in by_id:
            raise PersonalContextFailure("personal context source IDs must be non-empty and unique")
        by_id[source_id] = source

    include = {str(value).strip() for value in include_source_ids if str(value).strip()}
    exclude = {str(value).strip() for value in exclude_source_ids if str(value).strip()}
    if include & exclude:
        raise PersonalContextFailure("a personal context source cannot be both included and excluded")
    unknown = sorted((include | exclude) - set(by_id))
    if unknown:
        raise PersonalContextFailure("unknown personal context source ID: " + unknown[0])

    selected: list[dict[str, Any]] = []
    for source in available:
        source_id = str(source["source_id"])
        if source_id in exclude:
            continue
        if source_id in include:
            selected.append({**source, "selection_basis": "explicit-user-context-confirmation"})
        elif confirm_defaults and source.get("selected_by_default") is True:
            selected.append({
                **source,
                "selection_basis": str(source.get("selection_basis") or "confirmed-context-default"),
            })
    return selected


def source_snapshots_current(workspace: Path, snapshots: dict[str, Any]) -> bool:
    """Check persistent profile/belief snapshots before preparing a frozen task."""
    workspace = _workspace_root(workspace)
    if not isinstance(snapshots, dict):
        return False
    expected_paths = {
        "profile_settings": PROFILE_SETTINGS_PATH.as_posix(),
        "belief_events": BELIEF_EVENT_PATH.as_posix(),
    }
    for key in ("profile_settings", "belief_events"):
        snapshot = snapshots.get(key)
        if not isinstance(snapshot, dict) or snapshot.get("loaded") is not True:
            return False
        if snapshot.get("path") != expected_paths[key]:
            return False
        relative = Path(str(snapshot.get("path") or ""))
        if relative.is_absolute() or ".." in relative.parts:
            return False
        path = workspace / relative
        if path.is_symlink() or (path.exists() and not path.is_file()):
            return False
        if bool(snapshot.get("exists")) != path.is_file() or snapshot.get("sha256") != file_sha256(path):
            return False
    profile_snapshot = snapshots.get("profile_events")
    if not isinstance(profile_snapshot, dict):
        return False
    if profile_snapshot.get("path") != PROFILE_EVENT_PATH.as_posix():
        return False
    if profile_snapshot.get("loaded") is True:
        relative = Path(str(profile_snapshot.get("path") or ""))
        if relative.is_absolute() or ".." in relative.parts:
            return False
        path = workspace / relative
        if path.is_symlink() or (path.exists() and not path.is_file()):
            return False
        if bool(profile_snapshot.get("exists")) != path.is_file() or profile_snapshot.get("sha256") != file_sha256(path):
            return False
        for source in profile_snapshot.get("source_files") or []:
            if not isinstance(source, dict):
                return False
            source_relative = Path(str(source.get("path") or ""))
            if source_relative.is_absolute() or ".." in source_relative.parts:
                return False
            source_path = workspace / source_relative
            if source_path.is_symlink() or not source_path.is_file() or source.get("sha256") != file_sha256(source_path):
                return False
    elif profile_snapshot.get("loaded") is not False:
        return False
    return True
