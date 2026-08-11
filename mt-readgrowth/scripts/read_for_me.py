#!/usr/bin/env python3
"""Create auditable V2 node-1 read-for-me tasks and deterministic reports."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

sys.dont_write_bytecode = True

from personal_context import (
    PersonalContextFailure,
    assemble_personal_context,
    belief_states,
    historical_session_sources,
    source_snapshots_current,
)


SCHEMA_VERSION = 1
FLOW_VERSION = "0.1.1"
TASK_ROOT = Path("knowledge/read-for-me")
VALID_JOB_STATES = {"needs-brief", "context-review", "evidence-ready", "report-ready", "completed"}
BOOK_EVIDENCE_CLASSES = {"作者明确表达", "作者立场推断"}
PERSONAL_EVIDENCE_CLASS = "AI延伸应用"
FORBIDDEN_COVERAGE_CLAIMS = (
    "逐字读完",
    "逐章读完",
    "完整覆盖所有内容",
    "全部章节覆盖",
    "全书100%",
    "全书 100%",
)


def normalize_scope_limits(values: Iterable[Any]) -> list[str]:
    """Keep inherited evidence limits while removing forbidden completion claims."""
    replacements = {
        "逐字读完": "未逐字覆盖",
        "逐章读完": "未获得章节级完整覆盖",
        "完整覆盖所有内容": "仅覆盖已准备的相关证据",
        "全部章节覆盖": "仅覆盖已准备的相关证据",
        "全书100%": "不代表全书覆盖",
        "全书 100%": "不代表全书覆盖",
    }
    normalized: list[str] = []
    for value in values:
        text = str(value)
        for forbidden, replacement in replacements.items():
            text = text.replace(forbidden, replacement)
        if text not in normalized:
            normalized.append(text)
    return normalized
SENSITIVE_CONTEXT = re.compile(
    r"(?i)(api[ _-]?key|access[ _-]?token|authorization|credential|secret|password|密钥|密码|令牌)"
)
PROCESS_CONTEXT = re.compile(
    r"(?i)(验收|体验可用|节点\s*\d|跳过.{0,6}讨论|流程.{0,8}通过|skill|同步.{0,8}成功|导入.{0,8}成功)"
)


class ReadForMeFailure(RuntimeError):
    """A safe validation or lifecycle failure."""


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)


def atomic_json(path: Path, value: Any) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ReadForMeFailure(f"Missing required JSON file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ReadForMeFailure(f"Invalid JSON file: {path}") from exc
    if not isinstance(value, dict):
        raise ReadForMeFailure(f"Expected a JSON object: {path}")
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    values: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ReadForMeFailure(f"Invalid JSONL at {path}:{number}") from exc
        if not isinstance(value, dict):
            raise ReadForMeFailure(f"Expected a JSON object at {path}:{number}")
        values.append(value)
    return values


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(canonical_json(value) + "\n")


def relative_path(path: Path, workspace: Path) -> str:
    try:
        return path.resolve().relative_to(workspace.resolve()).as_posix()
    except ValueError as exc:
        raise ReadForMeFailure(f"Source path is outside the reading workspace: {path}") from exc


def yaml_scalar(content: str, key: str, indent: int = 0) -> str | None:
    match = re.search(rf"(?m)^{' ' * indent}{re.escape(key)}:\s*(.+?)\s*$", content)
    if not match or match.group(1) == "null":
        return None
    raw = match.group(1)
    try:
        return str(json.loads(raw))
    except json.JSONDecodeError:
        return raw.strip("\"'")


def yaml_list(content: str, key: str) -> list[str]:
    lines = content.splitlines()
    start = next((index for index, line in enumerate(lines) if re.fullmatch(rf"{re.escape(key)}:\s*", line)), None)
    if start is None:
        return []
    values: list[str] = []
    for line in lines[start + 1 :]:
        match = re.fullmatch(r"  -\s*(.+?)\s*", line)
        if not match:
            break
        raw = match.group(1)
        try:
            values.append(str(json.loads(raw)))
        except json.JSONDecodeError:
            values.append(raw.strip("\"'"))
    return values


def book_metadata(book_dir: Path) -> dict[str, Any]:
    path = book_dir / "book.yaml"
    if not path.is_file():
        raise ReadForMeFailure("book.yaml is missing")
    content = path.read_text(encoding="utf-8")
    return {
        "stable_book_id": yaml_scalar(content, "stable_book_id") or "",
        "title": yaml_scalar(content, "title") or book_dir.name,
        "authors": yaml_list(content, "authors"),
        "text_status": yaml_scalar(content, "status"),
        "quality_warning": yaml_scalar(content, "quality_warning"),
    }


def ensure_workspace(workspace: Path) -> Path:
    workspace = workspace.resolve()
    if not workspace.is_dir():
        raise ReadForMeFailure("reading workspace does not exist")
    return workspace


def ensure_book(workspace: Path, book_dir: Path) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    workspace = ensure_workspace(workspace)
    book_dir = book_dir.resolve()
    if book_dir.parent != (workspace / "books").resolve():
        raise ReadForMeFailure("book-dir must be directly under workspace/books")
    metadata = book_metadata(book_dir)
    stable_id = metadata["stable_book_id"]
    if not re.fullmatch(r"[0-9a-f]{64}", stable_id):
        raise ReadForMeFailure("book.yaml has an invalid stable_book_id")
    state = load_json(book_dir / "derived" / "analysis-state.json")
    if state.get("stable_book_id") != stable_id:
        raise ReadForMeFailure("analysis-state.json belongs to a different book")
    return book_dir, metadata, state


def task_dir(workspace: Path, report_id: str) -> Path:
    if not re.fullmatch(r"read-for-me-[0-9a-f]{16}", report_id):
        raise ReadForMeFailure("invalid report_id")
    return ensure_workspace(workspace) / TASK_ROOT / report_id


def brief_hash(brief: dict[str, Any]) -> str:
    return sha256_text(
        canonical_json(
            {key: value for key, value in brief.items() if key not in {"brief_sha256", "recorded_at"}}
        )
    )


def object_hash(value: dict[str, Any], field: str) -> str:
    return sha256_text(canonical_json({key: item for key, item in value.items() if key != field}))


def context_hash(context: dict[str, Any]) -> str:
    semantic = json.loads(canonical_json(context))
    semantic.pop("context_sha256", None)
    semantic.pop("reviewed_at", None)
    for field in ("available_sources", "selected_sources"):
        for item in semantic.get(field) or []:
            if isinstance(item, dict) and item.get("source_type") == "current-report-correction":
                source = item.get("source")
                if isinstance(source, dict):
                    source.pop("recorded_at", None)
    return sha256_text(canonical_json(semantic))


def event_id(event: str, report_id: str, fingerprint: str) -> str:
    return "read-for-me-event-" + sha256_text(f"{event}\0{report_id}\0{fingerprint}")


def append_event(directory: Path, event: str, fingerprint: str, payload: dict[str, Any], at: str) -> bool:
    path = directory / "events.jsonl"
    identifier = event_id(event, directory.name, fingerprint)
    if identifier in {str(item.get("event_id")) for item in load_jsonl(path)}:
        return False
    append_jsonl(
        path,
        {
            "schema_version": SCHEMA_VERSION,
            "event_id": identifier,
            "event": event,
            "at": at,
            "report_id": directory.name,
            "payload": payload,
        },
    )
    return True


def save_job(directory: Path, job: dict[str, Any], *, status: str | None = None, now: str | None = None) -> None:
    if status is not None:
        if status not in VALID_JOB_STATES:
            raise ReadForMeFailure(f"invalid job status: {status}")
        job["status"] = status
    job["updated_at"] = now or utc_now()
    atomic_json(directory / "job.json", job)


def next_action(job: dict[str, Any]) -> str:
    status = job.get("status")
    if status == "needs-brief":
        return "provide why-now and question, then start the corrected task"
    if job.get("book_gate", {}).get("analysis_status") != "discussion-ready":
        return "reuse derive_book.py prepare-overview, apply-overview, and audit until discussion-ready"
    if status == "context-review":
        return "review context candidates and run context --confirm"
    if status == "evidence-ready":
        return "generate a result JSON from report-input.json, then run apply"
    if status == "report-ready":
        return "run audit"
    if status == "completed":
        return "read report.md or continue with an on-demand chapter discussion"
    return "inspect the task"


def start_task(
    workspace: Path,
    book_dir: Path,
    *,
    why_now: str = "",
    question: str = "",
    desired_outcome: str = "",
    now: str | None = None,
) -> dict[str, Any]:
    workspace = ensure_workspace(workspace)
    book_dir, metadata, state = ensure_book(workspace, book_dir)
    recorded_at = now or utc_now()
    base = {
        "schema_version": SCHEMA_VERSION,
        "stable_book_id": metadata["stable_book_id"],
        "why_now": why_now.strip(),
        "question": question.strip(),
        "desired_outcome": desired_outcome.strip(),
    }
    report_id = "read-for-me-" + sha256_text(canonical_json(base))[:16]
    directory = task_dir(workspace, report_id)
    missing = [key for key in ("why_now", "question") if not base[key]]
    brief = {
        **base,
        "report_id": report_id,
        "recorded_at": recorded_at,
        "missing_fields": missing,
        "declared_gaps": (["desired_outcome"] if not base["desired_outcome"] else []),
    }
    brief["brief_sha256"] = brief_hash(brief)
    brief_path = directory / "reading-brief.json"
    if brief_path.exists():
        current = load_json(brief_path)
        if current.get("brief_sha256") != brief["brief_sha256"]:
            raise ReadForMeFailure("deterministic report_id already has a different reading brief")
        job = load_json(directory / "job.json")
        return {
            "result": "unchanged",
            "report_id": report_id,
            "status": job.get("status"),
            "missing_fields": current.get("missing_fields") or [],
            "next_action": next_action(job),
        }
    directory.mkdir(parents=True, exist_ok=True)
    atomic_json(brief_path, brief)
    status = "needs-brief" if missing else "context-review"
    job = {
        "schema_version": SCHEMA_VERSION,
        "flow_version": FLOW_VERSION,
        "report_id": report_id,
        "stable_book_id": metadata["stable_book_id"],
        "book_dir": relative_path(book_dir, workspace),
        "book_title": metadata["title"],
        "status": status,
        "created_at": recorded_at,
        "updated_at": recorded_at,
        "report_version": 0,
        "last_error": None,
        "book_gate": {
            "text_status": metadata["text_status"],
            "analysis_status": state.get("status"),
        },
    }
    atomic_json(directory / "job.json", job)
    append_event(
        directory,
        "read-for-me-started",
        brief["brief_sha256"],
        {"brief_sha256": brief["brief_sha256"], "status": status},
        recorded_at,
    )
    return {
        "result": "created",
        "report_id": report_id,
        "status": status,
        "missing_fields": missing,
        "declared_gaps": brief["declared_gaps"],
        "next_action": next_action(job),
    }


def safe_session_lines(workspace: Path) -> list[dict[str, Any]]:
    """Compatibility wrapper around the shared read-only history loader."""
    try:
        return historical_session_sources(workspace)[0]
    except PersonalContextFailure as exc:
        raise ReadForMeFailure(str(exc)) from exc


def context_source_packet(
    workspace: Path,
    brief: dict[str, Any],
    *,
    now: str | None = None,
) -> dict[str, Any]:
    current_sources: list[dict[str, Any]] = []
    fields = (
        ("why-now", "why_now", "reading-brief-reason"),
        ("question", "question", "reading-brief-question"),
        ("desired-outcome", "desired_outcome", "reading-brief-outcome"),
    )
    for suffix, key, source_type in fields:
        if brief.get(key):
            current_sources.append(
                {
                    "source_id": f"brief:{suffix}",
                    "source_type": source_type,
                    "status": "current-user-statement",
                    "content": brief[key],
                    "source": {
                        "path": relative_path(task_dir(workspace, brief["report_id"]) / "reading-brief.json", workspace),
                        "field": key,
                    },
                    "selected_by_default": True,
                    "requires_explicit_selection": False,
                }
            )
    try:
        return assemble_personal_context(
            workspace,
            route="read-for-me",
            current_sources=current_sources,
            now=now,
        )
    except PersonalContextFailure as exc:
        raise ReadForMeFailure(str(exc)) from exc


def context_candidates(workspace: Path, brief: dict[str, Any]) -> list[dict[str, Any]]:
    """Compatibility view of the shared context packet."""
    return context_source_packet(workspace, brief)["available_sources"]


def review_context(
    workspace: Path,
    report_id: str,
    *,
    include: Iterable[str] = (),
    exclude: Iterable[str] = (),
    corrections: Iterable[str] = (),
    confirm: bool = False,
    now: str | None = None,
) -> dict[str, Any]:
    workspace = ensure_workspace(workspace)
    directory = task_dir(workspace, report_id)
    job = load_json(directory / "job.json")
    brief = load_json(directory / "reading-brief.json")
    if brief.get("missing_fields"):
        raise ReadForMeFailure("reading brief is incomplete; create a corrected task first")
    source_packet = context_source_packet(workspace, brief, now=now)
    available = source_packet["available_sources"]
    existing_context_path = directory / "context.json"
    existing_context: dict[str, Any] | None = None
    if existing_context_path.exists():
        existing_context = load_json(existing_context_path)
        known_ids = {str(item.get("source_id")) for item in available}
        for item in existing_context.get("available_sources") or []:
            if (
                isinstance(item, dict)
                and item.get("source_type") == "current-report-correction"
                and str(item.get("source_id")) not in known_ids
            ):
                available.append(item)
                known_ids.add(str(item.get("source_id")))
    by_id = {str(item["source_id"]): item for item in available}
    include_ids, exclude_ids = set(include), set(exclude)
    if existing_context is not None:
        include_ids.update(
            str(item.get("source_id"))
            for item in existing_context.get("selected_sources") or []
            if isinstance(item, dict) and item.get("selection_basis") == "explicit-user-context-confirmation"
        )
    unknown = sorted((include_ids | exclude_ids) - set(by_id))
    if unknown:
        raise ReadForMeFailure("unknown context source IDs: " + ", ".join(unknown))
    correction_items: list[dict[str, Any]] = []
    for raw in corrections:
        content = raw.strip()
        if not content:
            continue
        if SENSITIVE_CONTEXT.search(content):
            raise ReadForMeFailure("context correction appears to contain credentials or secret-bearing text")
        source_id = "correction:" + sha256_text(content)[:16]
        correction_items.append(
            {
                "source_id": source_id,
                "source_type": "current-report-correction",
                "status": "current-user-statement",
                "content": content,
                "source": {"event": "context-correction", "recorded_at": now or utc_now()},
                "selected_by_default": True,
                "requires_explicit_selection": False,
            }
        )
        include_ids.add(source_id)
        by_id[source_id] = correction_items[-1]
    available.extend(correction_items)
    selected: list[dict[str, Any]] = []
    for candidate in available:
        source_id = str(candidate["source_id"])
        is_brief = str(candidate["source_type"]).startswith("reading-brief-")
        enabled = is_brief
        selection_basis = "current-reading-brief" if is_brief else None
        if confirm and candidate.get("selected_by_default") and source_id not in exclude_ids:
            enabled = True
            selection_basis = str(candidate.get("selection_basis") or "confirmed-context-default")
        if confirm and candidate.get("source_type") == "current-report-correction" and source_id not in exclude_ids:
            enabled, selection_basis = True, "explicit-user-context-confirmation"
        if confirm and source_id in include_ids and source_id not in exclude_ids:
            enabled, selection_basis = True, "explicit-user-context-confirmation"
        if source_id in exclude_ids:
            enabled, selection_basis = False, "explicit-user-context-exclusion"
        if enabled:
            selected.append({**candidate, "selection_basis": selection_basis})
    selected_ids = {str(item["source_id"]) for item in selected}
    excluded_sources = list(source_packet["excluded_sources"])
    for candidate in available:
        source_id = str(candidate["source_id"])
        if source_id in selected_ids:
            continue
        if source_id in exclude_ids:
            exclusion_basis = "explicit-user-context-exclusion"
        elif candidate.get("requires_explicit_selection"):
            exclusion_basis = "explicit-selection-required"
        else:
            exclusion_basis = "context-default-not-confirmed"
        excluded_sources.append({**candidate, "exclusion_basis": exclusion_basis})
    context = {
        "schema_version": SCHEMA_VERSION,
        "report_id": report_id,
        "stable_book_id": brief["stable_book_id"],
        "review_status": "confirmed" if confirm else "pending",
        "reviewed_at": now or utc_now(),
        "memory_mode": source_packet["memory_mode"],
        "memory_mode_configured": source_packet["memory_mode_configured"],
        "source_snapshots": source_packet["source_snapshots"],
        "source_bundle_sha256": source_packet["source_bundle_sha256"],
        "available_sources": available,
        "selected_sources": selected,
        "excluded_sources": excluded_sources,
        "unknowns": brief.get("declared_gaps") or [],
        "selection_rules": {
            "brief_enabled": True,
            "confirmed_revised_default_after_confirmation": True,
            "candidate_provisional_session_default": False,
        },
    }
    context["context_sha256"] = context_hash(context)
    atomic_json(directory / "context.json", context)
    event_name = "read-for-me-context-confirmed" if confirm else "read-for-me-context-previewed"
    append_event(
        directory,
        event_name,
        context["context_sha256"],
        {
            "context_sha256": context["context_sha256"],
            "selected_source_ids": [item["source_id"] for item in selected],
            "included_source_ids": sorted(include_ids),
            "excluded_source_ids": sorted(exclude_ids),
            "corrections": [
                {"source_id": item["source_id"], "content": item["content"]}
                for item in correction_items
            ],
        },
        now or utc_now(),
    )
    save_job(directory, job, status="context-review", now=now)
    return {
        "result": "confirmed" if confirm else "preview",
        "report_id": report_id,
        "status": job["status"],
        "available_sources": available,
        "selected_sources": selected,
        "excluded_sources": excluded_sources,
        "unknowns": context["unknowns"],
        "next_action": "run prepare" if confirm else "remove, correct, or include sources, then run context --confirm",
    }


def all_paragraphs(book_dir: Path) -> dict[str, dict[str, Any]]:
    values: dict[str, dict[str, Any]] = {}
    for record in load_jsonl(book_dir / "parsed" / "paragraphs.jsonl"):
        locator = str(record.get("locator") or "")
        if not locator or locator in values:
            raise ReadForMeFailure("paragraph locators are missing or duplicated")
        text = str(record.get("text") or "")
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != record.get("text_sha256"):
            raise ReadForMeFailure(f"display paragraph hash mismatch: {locator}")
        values[locator] = record
    return values


def citations_in(value: Any) -> set[str]:
    citations: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"citations", "book_citations"} and isinstance(item, list):
                citations.update(str(locator) for locator in item)
            else:
                citations.update(citations_in(item))
    elif isinstance(value, list):
        for item in value:
            citations.update(citations_in(item))
    return citations


def source_quality_summary(book_dir: Path, metadata: dict[str, Any]) -> tuple[dict[str, Any], Path | None]:
    path = book_dir / "derived" / "source-quality.json"
    if not path.exists():
        return {
            "quality_warning": metadata.get("quality_warning"),
            "registered_rules": [],
            "affected_count": 0,
            "skipped_evidence": [],
        }, None
    registry = load_json(path)
    rules: list[str] = []
    skipped: set[str] = set()
    affected = 0
    for key in ("rules", "registered_rules"):
        if isinstance(registry.get(key), list):
            for item in registry[key]:
                if isinstance(item, dict) and item.get("rule_id"):
                    rules.append(str(item["rule_id"]))
    if registry.get("rule_id"):
        rules.append(str(registry["rule_id"]))
    for key in ("entries", "affected", "paragraphs"):
        if isinstance(registry.get(key), list):
            affected = max(affected, len(registry[key]))
            for item in registry[key]:
                if isinstance(item, dict) and (
                    item.get("status") == "skipped"
                    or item.get("clean_status") == "skipped"
                    or item.get("usable") is False
                ):
                    if item.get("locator"):
                        skipped.add(str(item["locator"]))
    if isinstance(registry.get("skipped_evidence"), list):
        skipped.update(str(item) for item in registry["skipped_evidence"])
    return {
        "quality_warning": metadata.get("quality_warning"),
        "registered_rules": sorted(set(rules)),
        "affected_count": affected,
        "skipped_evidence": sorted(skipped),
    }, path


def manifest_for(paths: Iterable[Path], workspace: Path) -> list[dict[str, str]]:
    unique = sorted({path.resolve() for path in paths if path.is_file()}, key=lambda item: str(item).casefold())
    return [{"path": relative_path(path, workspace), "sha256": file_sha256(path)} for path in unique]


def prepare_report(
    workspace: Path,
    report_id: str,
    *,
    chapter_ids: Iterable[str] = (),
    now: str | None = None,
) -> dict[str, Any]:
    workspace = ensure_workspace(workspace)
    directory = task_dir(workspace, report_id)
    job = load_json(directory / "job.json")
    brief = load_json(directory / "reading-brief.json")
    context = load_json(directory / "context.json")
    if context.get("review_status") != "confirmed":
        raise ReadForMeFailure("personal context has not been confirmed")
    if context.get("context_sha256") != context_hash(context):
        raise ReadForMeFailure("personal context hash failed validation; review context again")
    try:
        snapshots_current = source_snapshots_current(workspace, context.get("source_snapshots"))
    except PersonalContextFailure as exc:
        raise ReadForMeFailure(str(exc)) from exc
    if not snapshots_current:
        raise ReadForMeFailure("personal context sources changed; review and confirm context again before prepare")
    book_dir, metadata, state = ensure_book(workspace, workspace / str(job["book_dir"]))
    if state.get("status") != "discussion-ready":
        job["book_gate"] = {"text_status": metadata["text_status"], "analysis_status": state.get("status")}
        save_job(directory, job, status="context-review", now=now)
        return {
            "result": "needs-evidence",
            "reason": "book-not-discussion-ready",
            "next_action": "run the existing overview derivation and audit workflow",
        }
    overview_path = book_dir / "derived" / "overview-result.json"
    overview = load_json(overview_path)
    if overview.get("stable_book_id") != metadata["stable_book_id"]:
        raise ReadForMeFailure("overview result belongs to a different book")
    toc_path = book_dir / "parsed" / "toc.json"
    toc = load_json(toc_path)
    valid_chapters = {str(item.get("chapter_id")) for item in toc.get("chapters") or [] if isinstance(item, dict)}
    requested = sorted(set(chapter_ids))
    invalid = sorted(set(requested) - valid_chapters)
    if invalid:
        raise ReadForMeFailure("unknown requested chapters: " + ", ".join(invalid))
    chapter_results: list[dict[str, Any]] = []
    chapter_paths: list[Path] = []
    missing: list[str] = []
    for chapter_id in requested:
        path = book_dir / "derived" / "chapter-results" / f"{chapter_id}.json"
        if not path.exists():
            legacy = book_dir / "derived" / f"chapter-result-{chapter_id}.json"
            path = legacy if legacy.exists() else path
        if not path.exists():
            missing.append(chapter_id)
            continue
        result = load_json(path)
        if result.get("stable_book_id") != metadata["stable_book_id"] or result.get("chapter_id") != chapter_id:
            raise ReadForMeFailure(f"chapter result identity mismatch: {chapter_id}")
        chapter_results.append(result)
        chapter_paths.append(path)
    if missing:
        return {
            "result": "needs-evidence",
            "reason": "requested-chapter-not-derived",
            "missing_chapters": missing,
            "requested_chapters": requested,
            "unrelated_chapters_scheduled": [],
            "next_actions": [
                f"derive_book.py prepare-chapter --book-dir <book-dir> --chapter-id {chapter_id}; review, apply, and audit"
                for chapter_id in missing
            ],
        }
    paragraphs = all_paragraphs(book_dir)
    locators = citations_in(overview)
    for result in chapter_results:
        locators.update(citations_in(result))
    unresolved = sorted(locator for locator in locators if locator not in paragraphs)
    if unresolved:
        raise ReadForMeFailure("unresolved evidence locators: " + ", ".join(unresolved[:5]))
    evidence_records = [
        {
            "locator": locator,
            "chapter_id": paragraphs[locator].get("chapter_id"),
            "chapter_title": paragraphs[locator].get("chapter_title"),
            "text": paragraphs[locator].get("text"),
            "text_sha256": paragraphs[locator].get("text_sha256"),
        }
        for locator in sorted(locators)
    ]
    quality, quality_path = source_quality_summary(book_dir, metadata)
    source_paths = [
        book_dir / "book.yaml",
        book_dir / "derived" / "analysis-state.json",
        overview_path,
        book_dir / "derived" / "book-map.md",
        book_dir / "derived" / "author-lens.md",
        toc_path,
        book_dir / "parsed" / "paragraphs.jsonl",
        directory / "reading-brief.json",
        directory / "context.json",
        *chapter_paths,
    ]
    if quality_path:
        source_paths.append(quality_path)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "report_id": report_id,
        "stable_book_id": metadata["stable_book_id"],
        "sources": manifest_for(source_paths, workspace),
    }
    atomic_json(directory / "source-manifest.json", manifest)
    manifest_sha = file_sha256(directory / "source-manifest.json")
    scope_limits = normalize_scope_limits(overview.get("scope_limits") or [])
    scope_limits.append("概览用于全书导航，不证明每章均已逐章处理。")
    evidence_gaps = []
    if not brief.get("desired_outcome"):
        evidence_gaps.append("用户未指定希望从报告获得的具体结果。")
    if not requested:
        evidence_gaps.append("本次未指定需要补充的章节结果，仅使用已审计概览。")
    report_input = {
        "schema_version": SCHEMA_VERSION,
        "flow_version": FLOW_VERSION,
        "report_id": report_id,
        "stable_book_id": metadata["stable_book_id"],
        "book": {"title": metadata["title"], "authors": metadata["authors"]},
        "reading_brief": brief,
        "reading_brief_sha256": brief["brief_sha256"],
        "context_sha256": context["context_sha256"],
        "context_sources": context["selected_sources"],
        "book_overview": overview,
        "book_route": overview.get("sections") or [],
        "author_lenses": overview.get("lenses") or [],
        "chapter_results": chapter_results,
        "evidence_records": evidence_records,
        "evidence_scope": {
            "overview_used": True,
            "requested_chapters": requested,
            "applied_chapters_used": [str(item.get("chapter_id")) for item in chapter_results],
            "chapter_coverage": (state.get("chapters") or {}).get("coverage"),
            "chapter_total": (state.get("chapters") or {}).get("total"),
            "quality": quality,
        },
        "scope_limits": scope_limits,
        "evidence_gaps": evidence_gaps,
        "source_manifest": manifest["sources"],
        "source_manifest_sha256": manifest_sha,
    }
    report_input["input_sha256"] = object_hash(report_input, "input_sha256")
    atomic_json(directory / "report-input.json", report_input)
    append_event(
        directory,
        "read-for-me-evidence-prepared",
        report_input["input_sha256"],
        {
            "input_sha256": report_input["input_sha256"],
            "requested_chapters": requested,
            "evidence_locator_count": len(evidence_records),
        },
        now or utc_now(),
    )
    job["book_gate"] = {"text_status": metadata["text_status"], "analysis_status": state.get("status")}
    job["last_error"] = None
    save_job(directory, job, status="evidence-ready", now=now)
    return {
        "result": "prepared",
        "report_id": report_id,
        "status": job["status"],
        "input_sha256": report_input["input_sha256"],
        "evidence_locator_count": len(evidence_records),
        "chapters_used": report_input["evidence_scope"]["applied_chapters_used"],
        "chapter_coverage": report_input["evidence_scope"]["chapter_coverage"],
        "next_action": next_action(job),
    }


def require_list(value: dict[str, Any], field: str, *, nonempty: bool = False) -> list[Any]:
    item = value.get(field)
    if not isinstance(item, list) or (nonempty and not item):
        qualifier = "non-empty " if nonempty else ""
        raise ReadForMeFailure(f"result field {field} must be a {qualifier}list")
    return item


def validate_locator_list(value: Any, allowed: set[str], field: str, *, nonempty: bool = True) -> list[str]:
    if not isinstance(value, list) or (nonempty and not value):
        qualifier = "non-empty " if nonempty else ""
        raise ReadForMeFailure(f"{field} must be a {qualifier}list")
    locators = [str(item) for item in value]
    unknown = sorted(set(locators) - allowed)
    if unknown:
        raise ReadForMeFailure(f"{field} contains locators outside the prepared packet: {', '.join(unknown[:5])}")
    return locators


def strings_in(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings_in(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings_in(item)


def validate_result(result: dict[str, Any], report_input: dict[str, Any]) -> None:
    if result.get("schema_version") != SCHEMA_VERSION:
        raise ReadForMeFailure("result schema_version must be 1")
    for field in ("report_id", "stable_book_id"):
        if result.get(field) != report_input.get(field):
            raise ReadForMeFailure(f"result {field} does not match report input")
    if result.get("input_sha256") != report_input.get("input_sha256"):
        raise ReadForMeFailure("result input_sha256 is stale or missing")
    if result.get("reading_brief_sha256") != report_input.get("reading_brief_sha256"):
        raise ReadForMeFailure("result reading_brief_sha256 is stale or missing")
    if result.get("source_manifest_sha256") != report_input.get("source_manifest_sha256"):
        raise ReadForMeFailure("result source_manifest_sha256 is stale or missing")
    if not isinstance(result.get("generator"), dict) or not result["generator"].get("name"):
        raise ReadForMeFailure("result generator identity is required")
    if not isinstance(result.get("book_overview"), dict):
        raise ReadForMeFailure("result book_overview must be an object")
    for field in ("theme", "core_question", "core_answer", "scope"):
        if not result["book_overview"].get(field):
            raise ReadForMeFailure(f"book_overview.{field} is required")
    allowed_locators = {str(item.get("locator")) for item in report_input.get("evidence_records") or []}
    input_context = {
        str(item.get("source_id")): str(item.get("status"))
        for item in report_input.get("context_sources") or []
    }
    allowed_context = set(input_context)
    result_context = require_list(result, "context_sources", nonempty=True)
    result_context_ids: set[str] = set()
    for item in result_context:
        source_id = str(item.get("source_id")) if isinstance(item, dict) else ""
        if (
            not isinstance(item, dict)
            or source_id not in allowed_context
            or str(item.get("evidence_status")) != input_context[source_id]
        ):
            raise ReadForMeFailure("each result context source must be selected and retain evidence_status")
        result_context_ids.add(source_id)
    if result_context_ids != allowed_context:
        raise ReadForMeFailure("result context_sources must match the approved context snapshot")
    for field in ("book_route", "core_ideas"):
        items = require_list(result, field, nonempty=True)
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict) or item.get("evidence_class") not in BOOK_EVIDENCE_CLASSES:
                raise ReadForMeFailure(f"{field}[{index}] has an invalid evidence_class")
            validate_locator_list(item.get("citations"), allowed_locators, f"{field}[{index}].citations")
            if field == "core_ideas" and (not item.get("statement") or not item.get("support")):
                raise ReadForMeFailure(f"core_ideas[{index}] requires statement and support")
    personalized = require_list(result, "personalized_answers", nonempty=True)
    for index, item in enumerate(personalized, start=1):
        if not isinstance(item, dict) or item.get("evidence_class") != PERSONAL_EVIDENCE_CLASS:
            raise ReadForMeFailure(f"personalized_answers[{index}] must use AI延伸应用")
        validate_locator_list(item.get("book_citations"), allowed_locators, f"personalized_answers[{index}].book_citations")
        source_ids = item.get("user_context_source_ids")
        if not isinstance(source_ids, list) or not source_ids:
            raise ReadForMeFailure(f"personalized_answers[{index}] needs user context sources")
        unknown = sorted({str(source_id) for source_id in source_ids} - allowed_context)
        if unknown:
            raise ReadForMeFailure(f"personalized_answers[{index}] uses unapproved context: {', '.join(unknown)}")
        if not item.get("mapping") or not item.get("answer"):
            raise ReadForMeFailure(f"personalized_answers[{index}] requires answer and mapping")
    for field in (
        "agreements_and_challenges",
        "actions",
        "additional_relevant_questions",
        "scope_limits",
        "evidence_gaps",
    ):
        require_list(result, field)
    if not isinstance(result.get("reading_path"), dict):
        raise ReadForMeFailure("result reading_path must be an object")
    if not isinstance(result.get("evidence_scope"), dict):
        raise ReadForMeFailure("result evidence_scope must be an object")
    expected_scope = report_input.get("evidence_scope") or {}
    result_scope = result["evidence_scope"]
    scope_pairs = (
        ("applied_chapters_used", expected_scope.get("applied_chapters_used") or []),
        ("chapter_coverage", expected_scope.get("chapter_coverage")),
        ("registered_cleaning_rules", (expected_scope.get("quality") or {}).get("registered_rules") or []),
        ("skipped_evidence", (expected_scope.get("quality") or {}).get("skipped_evidence") or []),
    )
    for field, expected in scope_pairs:
        if result_scope.get(field) != expected:
            raise ReadForMeFailure(f"evidence_scope.{field} does not match the prepared evidence")
    if result_scope.get("overview_used") is not True:
        raise ReadForMeFailure("evidence_scope.overview_used must disclose the overview")
    if not set(report_input.get("scope_limits") or []).issubset(set(result.get("scope_limits") or [])):
        raise ReadForMeFailure("result dropped prepared scope limits")
    if not set(report_input.get("evidence_gaps") or []).issubset(set(result.get("evidence_gaps") or [])):
        raise ReadForMeFailure("result dropped prepared evidence gaps")
    for text in strings_in(result):
        for forbidden in FORBIDDEN_COVERAGE_CLAIMS:
            if forbidden in text:
                raise ReadForMeFailure(f"result contains forbidden coverage claim: {forbidden}")


def list_text(values: Any, *, empty: str = "- 无。") -> str:
    if not isinstance(values, list) or not values:
        return empty
    lines: list[str] = []
    for item in values:
        if isinstance(item, dict):
            text = item.get("text") or item.get("statement") or item.get("title") or item.get("action") or canonical_json(item)
        else:
            text = str(item)
        lines.append(f"- {text}")
    return "\n".join(lines)


def finish_sentence(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text if text[-1] in "。！？；：" else text + "。"


def compact_text(value: Any, limit: int = 56) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip("，。；：") + "…"


def evidence_record_map(report_input: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("locator")): item
        for item in report_input.get("evidence_records") or []
        if isinstance(item, dict) and item.get("locator")
    }


def human_locator(locator: Any, report_input: dict[str, Any]) -> str:
    raw = str(locator or "")
    record = evidence_record_map(report_input).get(raw) or {}
    chapter_title = str(record.get("chapter_title") or record.get("chapter_id") or "相关章节").strip()
    match = re.search(r":p-(\d+)$", raw)
    paragraph = f"第{int(match.group(1))}段" if match else "相关段落"
    return f"《{chapter_title}》{paragraph}"


def evidence_details(citations: Any, report_input: dict[str, Any]) -> str:
    if not isinstance(citations, list) or not citations:
        return ""
    labels = list(dict.fromkeys(human_locator(locator, report_input) for locator in citations))
    body = "\n".join(f"- {label}" for label in labels)
    return f"<details><summary>查看书中依据</summary>\n\n{body}\n\n</details>"


def context_label(source_id: Any, report_input: dict[str, Any]) -> str:
    raw = str(source_id or "")
    known = {
        "brief:why-now": "本次阅读原因（用户本次明确提供）",
        "brief:question": "本次核心问题（用户本次明确提供）",
        "brief:desired-outcome": "本次阅读目标（用户本次明确提供）",
    }
    if raw in known:
        return known[raw]
    sources = {
        str(item.get("source_id")): item
        for item in report_input.get("context_sources") or []
        if isinstance(item, dict)
    }
    item = sources.get(raw) or {}
    source_type = str(item.get("source_type") or "")
    status = str(item.get("status") or "")
    type_labels = {
        "durable-belief": "长期认知",
        "candidate-belief": "候选认知",
        "historical-session-user-statement": "历史会话中的用户原话",
    }
    status_labels = {
        "confirmed": "已确认",
        "revised": "已修订确认",
        "provisional": "暂定",
        "candidate": "候选",
        "historical": "历史记录",
        "current-user-statement": "用户本次明确提供",
    }
    label = type_labels.get(source_type, "本次获准使用的个人信息")
    return f"{label}（{status_labels.get(status, '已获本次授权')}）"


def chapter_label(chapter_id: Any, report_input: dict[str, Any]) -> str:
    raw = str(chapter_id or "")
    for item in report_input.get("evidence_records") or []:
        if isinstance(item, dict) and str(item.get("chapter_id")) == raw:
            return f"《{str(item.get('chapter_title') or raw).strip()}》"
    match = re.fullmatch(r"ch-(\d+)", raw)
    return f"第{int(match.group(1))}个目录项" if match else "相关章节"


def humanize_scope_text(value: Any, report_input: dict[str, Any]) -> str:
    text = str(value or "").replace("spine", "电子书目录")
    return re.sub(r"ch-\d+", lambda match: chapter_label(match.group(0), report_input), text)


def mermaid_label(value: Any, limit: int = 74) -> str:
    return compact_text(value, limit=limit).replace('"', "'").replace("[", "（").replace("]", "）")


def render_route_mindmap(result: dict[str, Any], report_input: dict[str, Any]) -> str:
    overview = result["book_overview"]
    lines = [
        "```mermaid",
        "flowchart LR",
        f"    ROOT[\"{mermaid_label(report_input['book']['title'], 40)}<br/>核心：{mermaid_label(overview['core_question'], 54)}\"]",
    ]
    for index, item in enumerate(result["book_route"], start=1):
        node = f"R{index}"
        lines.append(
            f"    {node}[\"{mermaid_label(item.get('title') or '思想路线', 34)}"
            f"<br/>{mermaid_label(item.get('statement') or item.get('relationship'), 64)}\"]"
        )
        lines.append(f"    ROOT --> {node}")
    lines.append("```")
    return "\n".join(lines)


def render_result(result: dict[str, Any], report_input: dict[str, Any]) -> str:
    overview = result["book_overview"]
    route_titles = [str(item.get("title") or "思想路线") for item in result["book_route"]]
    route_lines = [
        f"- **{item.get('title', '思想路线')}**：{finish_sentence(item.get('statement') or item.get('relationship'))}"
        for item in result["book_route"]
    ]
    idea_lines = []
    for index, item in enumerate(result["core_ideas"], start=1):
        conditions = "、".join(str(value) for value in item.get("conditions") or []) or "未单独列出。"
        counterexamples = "、".join(str(value) for value in item.get("counterexamples") or []) or "未单独列出。"
        idea_lines.append(
            f"### {index}. {item.get('title', '核心观点')}\n\n"
            f"> 证据性质：{item['evidence_class']}\n\n"
            f"**先说人话：** {finish_sentence(item['statement'])}\n\n"
            f"**为什么这样说：** {finish_sentence(item['support'])}\n\n"
            f"**什么时候成立：** {finish_sentence(conditions)}\n\n"
            f"**不要误解成：** {finish_sentence(counterexamples)}\n\n"
            + evidence_details(item["citations"], report_input)
        )
    answer_lines = []
    for item in result["personalized_answers"]:
        contexts = "、".join(context_label(source_id, report_input) for source_id in item["user_context_source_ids"])
        answer_lines.append(
            f"### {item.get('question') or report_input['reading_brief']['question']}\n\n"
            f"**给你的回答（{item['evidence_class']}）：** {finish_sentence(item['answer'])}\n\n"
            f"**为什么这样回答：** {finish_sentence(item['mapping'])}\n\n"
            f"**本次使用的你的信息：** {contexts}\n\n"
            + evidence_details(item["book_citations"], report_input)
        )
    action_lines = []
    for index, item in enumerate(result["actions"], start=1):
        if isinstance(item, dict):
            action_lines.append(
                f"### {index}. {item.get('action', '行动')}\n\n"
                f"- 为什么：{finish_sentence(item.get('rationale', ''))}\n"
                f"- 怎么检验：{finish_sentence(item.get('verification', '未指定'))}\n"
                f"- 什么时候调整：{finish_sentence(item.get('adjust_when', '未指定'))}"
            )
        else:
            action_lines.append(f"{index}. {item}")
    reading_path = result["reading_path"]
    path_lines = []
    for label, key in (("精读", "deep_read"), ("浏览", "browse"), ("继续讨论", "discuss")):
        values = reading_path.get(key) or []
        value_lines = "\n".join(f"  - {value}" for value in values) if values else "  - 暂无。"
        path_lines.append(f"- **{label}**\n{value_lines}")
    scope = result["evidence_scope"]
    context_lines = [f"- {context_label(item['source_id'], report_input)}" for item in result["context_sources"]]
    used_chapters = [chapter_label(chapter_id, report_input) for chapter_id in scope.get("applied_chapters_used") or []]
    chapter_total = int((report_input.get("evidence_scope") or {}).get("chapter_total") or 0)
    chapter_count = len(scope.get("applied_chapters_used") or [])
    coverage = float(scope.get("chapter_coverage", report_input["evidence_scope"].get("chapter_coverage")) or 0)
    if chapter_total:
        report_coverage = chapter_count / chapter_total
        coverage_text = f"{chapter_count}/{chapter_total}（约 {report_coverage * 100:.1f}%）"
        current_chapter_count = min(chapter_total, max(0, round(coverage * chapter_total)))
        current_coverage_text = f"{current_chapter_count}/{chapter_total}（约 {coverage * 100:.1f}%）"
    else:
        coverage_text = f"{chapter_count} 个按需章节"
        current_coverage_text = f"约 {coverage * 100:.1f}%"
    route_summary = "\n".join(f"- {title}" for title in route_titles)
    route_mindmap = render_route_mindmap(result, report_input)
    skipped_evidence = scope.get("skipped_evidence") or []
    skipped_evidence_text = (
        "无" if not skipped_evidence else f"{len(skipped_evidence)} 条，详情保存在机器审计文件"
    )
    return (
        f"# 替我读：{report_input['book']['title']}\n\n"
        "> 本报告复用已审计全书概览和按需章节证据；"
        "不代表按字逐句或逐章处理完整本书。\n\n"
        "## 1. 你为什么需要这本书\n\n"
        f"- 为什么现在读：{report_input['reading_brief']['why_now']}\n"
        f"- 当前问题：{report_input['reading_brief']['question']}\n"
        f"- 希望获得：{report_input['reading_brief']['desired_outcome'] or '用户尚未明确。'}\n\n"
        "## 2. 一分钟认识这本书\n\n"
        f"**这本书在讲什么：** {finish_sentence(overview['theme'])}\n\n"
        f"**作者真正想回答的问题：** {finish_sentence(overview['core_question'])}\n\n"
        f"**全书怎么展开：**\n\n{route_summary}\n\n"
        f"**最后给出的答案：** {finish_sentence(overview['core_answer'])}\n\n"
        f"**阅读时必须保留的警惕：** {finish_sentence(overview['scope'])}\n\n"
        "## 3. 全书内容与思想路线\n\n"
        + route_mindmap
        + "\n\n### 读图说明\n\n"
        + "\n".join(route_lines)
        + "\n\n"
        "## 4. 必须理解的核心观点\n\n" + "\n\n".join(idea_lines) + "\n\n"
        "## 5. 这本书怎样回答你的问题\n\n" + "\n\n".join(answer_lines) + "\n\n"
        "## 6. 你应该接受、保留还是质疑什么\n\n"
        + list_text(result["agreements_and_challenges"])
        + "\n\n"
        "## 7. 怎样把它变成行动\n\n"
        + ("\n\n".join(action_lines) if action_lines else "- 暂无。")
        + "\n\n"
        "## 8. 如果以后想读原书\n\n"
        + "\n".join(path_lines)
        + "\n\n"
        "### 这本书可能还与你有关的问题\n\n"
        + list_text(result["additional_relevant_questions"])
        + "\n\n"
        "## 9. 证据与边界\n\n"
        "- 使用了经过审计的全书概览。\n"
        f"- 重点处理章节：{'、'.join(used_chapters) if used_chapters else '没有额外处理章节'}。\n"
        f"- 本报告章节使用范围：{coverage_text}；只补充了与你的问题直接相关的部分。\n"
        f"- 书籍当前思想层覆盖：{current_coverage_text}；可能包含报告生成后的按需章节讨论。\n"
        f"- 登记清洁：{'无' if not scope.get('registered_cleaning_rules') else '已按登记规则处理，详情保存在机器审计文件'}。\n"
        f"- 跳过证据：{skipped_evidence_text}。\n"
        "- 原始定位符、哈希和机器上下文标识保存在机器审计文件，不在面向用户的正文中展示。\n"
        "- 本次获准使用的个人上下文：\n"
        + "\n".join(context_lines)
        + "\n- 范围限制：\n"
        + list_text([humanize_scope_text(value, report_input) for value in result["scope_limits"]])
        + "\n- 证据缺口：\n"
        + list_text(result["evidence_gaps"])
        + "\n"
    )


def record_failure(directory: Path, job: dict[str, Any], safe_message: str, now: str | None = None) -> None:
    timestamp = now or utc_now()
    fingerprint = sha256_text(safe_message)
    append_event(
        directory,
        "read-for-me-generation-failed",
        fingerprint,
        {"code": "invalid-result", "safe_message": safe_message},
        timestamp,
    )
    job["last_error"] = {"code": "invalid-result", "at": timestamp, "safe_message": safe_message}
    if job.get("status") not in {"completed", "report-ready"}:
        job["status"] = "evidence-ready"
    save_job(directory, job, now=timestamp)


def validate_report_input_fresh(directory: Path, workspace: Path, report_input: dict[str, Any]) -> None:
    if report_input.get("input_sha256") != object_hash(report_input, "input_sha256"):
        raise ReadForMeFailure("report input hash is stale")
    brief = load_json(directory / "reading-brief.json")
    if brief.get("brief_sha256") != brief_hash(brief) or brief.get("brief_sha256") != report_input.get("reading_brief_sha256"):
        raise ReadForMeFailure("reading brief changed after report preparation")
    context = load_json(directory / "context.json")
    if context.get("context_sha256") != context_hash(context) or context.get("context_sha256") != report_input.get("context_sha256"):
        raise ReadForMeFailure("personal context changed after report preparation")
    manifest_path = directory / "source-manifest.json"
    manifest = load_json(manifest_path)
    if file_sha256(manifest_path) != report_input.get("source_manifest_sha256"):
        raise ReadForMeFailure("source manifest changed after report preparation")
    for item in manifest.get("sources") or []:
        source = workspace / str(item.get("path") or "")
        if not source.is_file() or file_sha256(source) != item.get("sha256"):
            raise ReadForMeFailure(f"prepared source changed: {item.get('path')}")


def apply_result(
    workspace: Path,
    report_id: str,
    result_path: Path,
    *,
    now: str | None = None,
) -> dict[str, Any]:
    workspace = ensure_workspace(workspace)
    directory = task_dir(workspace, report_id)
    job = load_json(directory / "job.json")
    report_input = load_json(directory / "report-input.json")
    try:
        validate_report_input_fresh(directory, workspace, report_input)
        result = load_json(result_path.resolve())
        validate_result(result, report_input)
        rendered = render_result(result, report_input)
    except ReadForMeFailure as exc:
        record_failure(directory, job, str(exc), now=now)
        raise
    target_result = directory / "result.json"
    target_report = directory / "report.md"
    result_content = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if (
        target_result.exists()
        and target_result.read_text(encoding="utf-8") == result_content
        and target_report.exists()
        and target_report.read_text(encoding="utf-8") == rendered
    ):
        return {
            "result": "unchanged",
            "report_id": report_id,
            "status": job.get("status"),
            "next_action": next_action(job),
        }
    if target_result.exists() and target_report.exists():
        previous = int(job.get("report_version") or 1)
        version_dir = directory / "versions" / f"v{previous:03d}"
        if not version_dir.exists():
            version_dir.mkdir(parents=True)
            atomic_write(version_dir / "result.json", target_result.read_text(encoding="utf-8"))
            atomic_write(version_dir / "report.md", target_report.read_text(encoding="utf-8"))
    atomic_write(target_result, result_content)
    atomic_write(target_report, rendered)
    job["report_version"] = int(job.get("report_version") or 0) + 1
    job["last_error"] = None
    save_job(directory, job, status="report-ready", now=now)
    append_event(
        directory,
        "read-for-me-report-applied",
        file_sha256(target_result),
        {
            "result_sha256": file_sha256(target_result),
            "report_sha256": file_sha256(target_report),
            "version": job["report_version"],
        },
        now or utc_now(),
    )
    return {
        "result": "applied",
        "report_id": report_id,
        "status": job["status"],
        "report_version": job["report_version"],
        "result_path": relative_path(target_result, workspace),
        "report_path": relative_path(target_report, workspace),
        "next_action": next_action(job),
    }


def record_feedback(
    workspace: Path,
    report_id: str,
    feedback: str,
    *,
    now: str | None = None,
) -> dict[str, Any]:
    directory = task_dir(workspace, report_id)
    job = load_json(directory / "job.json")
    content = feedback.strip()
    if not content:
        raise ReadForMeFailure("feedback cannot be empty")
    appended = append_event(
        directory,
        "read-for-me-feedback-recorded",
        sha256_text(content),
        {"content": content, "belief_promotion": False},
        now or utc_now(),
    )
    return {
        "result": "recorded" if appended else "unchanged",
        "report_id": report_id,
        "status": job.get("status"),
        "belief_promotion": False,
        "next_action": "continue discussion or explicitly use weekly_review.py for any later belief decision",
    }


def audit_task(
    workspace: Path,
    report_id: str,
    *,
    complete: bool = True,
    now: str | None = None,
) -> dict[str, Any]:
    workspace = ensure_workspace(workspace)
    directory = task_dir(workspace, report_id)
    checks: dict[str, bool] = {}
    failures: list[str] = []

    def check(name: str, value: bool) -> None:
        checks[name] = bool(value)
        if not value:
            failures.append(name)

    try:
        events = load_jsonl(directory / "events.jsonl")
        ids = [str(item.get("event_id")) for item in events]
        check("event_ids_unique", len(ids) == len(set(ids)) and all(ids))
        job = load_json(directory / "job.json")
        brief = load_json(directory / "reading-brief.json")
        context = load_json(directory / "context.json")
        report_input = load_json(directory / "report-input.json")
        manifest = load_json(directory / "source-manifest.json")
        identities = {
            job.get("report_id"),
            brief.get("report_id"),
            context.get("report_id"),
            report_input.get("report_id"),
        }
        stable_ids = {
            job.get("stable_book_id"),
            brief.get("stable_book_id"),
            context.get("stable_book_id"),
            report_input.get("stable_book_id"),
        }
        check("identity", identities == {report_id} and len(stable_ids) == 1 and None not in stable_ids)
        check(
            "brief_hash",
            brief.get("brief_sha256") == brief_hash(brief) == report_input.get("reading_brief_sha256"),
        )
        check(
            "context_hash",
            context.get("context_sha256") == context_hash(context) == report_input.get("context_sha256"),
        )
        check("input_hash", report_input.get("input_sha256") == object_hash(report_input, "input_sha256"))
        manifest_ok = (
            manifest.get("report_id") == report_id
            and file_sha256(directory / "source-manifest.json") == report_input.get("source_manifest_sha256")
        )
        for item in manifest.get("sources") or []:
            source = workspace / str(item.get("path") or "")
            manifest_ok = manifest_ok and source.is_file() and file_sha256(source) == item.get("sha256")
        check("source_manifest", manifest_ok)
        selected = {str(item.get("source_id")): item for item in context.get("selected_sources") or []}
        boundary_ok = context.get("review_status") == "confirmed"
        for item in selected.values():
            if item.get("requires_explicit_selection"):
                boundary_ok = boundary_ok and item.get("selection_basis") == "explicit-user-context-confirmation"
        check("context_boundary", boundary_ok)
        result = load_json(directory / "result.json")
        validate_result(result, report_input)
        check("result_schema", True)
        rendered = render_result(result, report_input)
        check(
            "report_render",
            (directory / "report.md").is_file()
            and (directory / "report.md").read_text(encoding="utf-8") == rendered,
        )
        check("coverage_disclosure", not any(term in rendered for term in FORBIDDEN_COVERAGE_CLAIMS))
    except (ReadForMeFailure, OSError, KeyError, TypeError, ValueError) as exc:
        check("audit_exception", False)
        safe_error = str(exc)
    else:
        safe_error = None
    passed = not failures
    if passed and complete:
        job = load_json(directory / "job.json")
        save_job(directory, job, status="completed", now=now)
        append_event(
            directory,
            "read-for-me-audit-passed",
            file_sha256(directory / "result.json"),
            {"checks": sorted(checks)},
            now or utc_now(),
        )
    return {
        "result": "passed" if passed else "failed",
        "report_id": report_id,
        "checks": checks,
        "failed_checks": failures,
        "safe_error": safe_error,
        "status": (
            load_json(directory / "job.json").get("status")
            if (directory / "job.json").exists()
            else None
        ),
    }


def status_packet(workspace: Path, report_id: str) -> dict[str, Any]:
    directory = task_dir(workspace, report_id)
    job = load_json(directory / "job.json")
    packet = {
        "result": "status",
        "report_id": report_id,
        "status": job.get("status"),
        "book_gate": job.get("book_gate"),
        "last_error": job.get("last_error"),
        "next_action": next_action(job),
    }
    for name in ("reading-brief.json", "context.json", "report-input.json", "result.json", "report.md"):
        packet[name] = (directory / name).exists()
    return packet


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    start = subparsers.add_parser("start")
    start.add_argument("--workspace", type=Path, required=True)
    start.add_argument("--book-dir", type=Path, required=True)
    start.add_argument("--why-now", default="")
    start.add_argument("--question", default="")
    start.add_argument("--desired-outcome", default="")
    start.add_argument("--at", help=argparse.SUPPRESS)
    context = subparsers.add_parser("context")
    context.add_argument("--workspace", type=Path, required=True)
    context.add_argument("--report-id", required=True)
    context.add_argument("--include", action="append", default=[])
    context.add_argument("--exclude", action="append", default=[])
    context.add_argument("--correction", action="append", default=[])
    context.add_argument("--confirm", action="store_true")
    context.add_argument("--at", help=argparse.SUPPRESS)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--workspace", type=Path, required=True)
    prepare.add_argument("--report-id", required=True)
    prepare.add_argument("--chapter", action="append", default=[])
    prepare.add_argument("--at", help=argparse.SUPPRESS)
    status = subparsers.add_parser("status")
    status.add_argument("--workspace", type=Path, required=True)
    status.add_argument("--report-id", required=True)
    apply = subparsers.add_parser("apply")
    apply.add_argument("--workspace", type=Path, required=True)
    apply.add_argument("--report-id", required=True)
    apply.add_argument("--result", type=Path, required=True)
    apply.add_argument("--at", help=argparse.SUPPRESS)
    audit = subparsers.add_parser("audit")
    audit.add_argument("--workspace", type=Path, required=True)
    audit.add_argument("--report-id", required=True)
    audit.add_argument("--no-complete", action="store_true", help=argparse.SUPPRESS)
    audit.add_argument("--at", help=argparse.SUPPRESS)
    feedback = subparsers.add_parser("feedback")
    feedback.add_argument("--workspace", type=Path, required=True)
    feedback.add_argument("--report-id", required=True)
    feedback.add_argument("--feedback", required=True)
    feedback.add_argument("--at", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    configure_console()
    args = parse_args(argv or sys.argv[1:])
    try:
        if args.command == "start":
            result = start_task(
                args.workspace,
                args.book_dir,
                why_now=args.why_now,
                question=args.question,
                desired_outcome=args.desired_outcome,
                now=args.at,
            )
        elif args.command == "context":
            result = review_context(
                args.workspace,
                args.report_id,
                include=args.include,
                exclude=args.exclude,
                corrections=args.correction,
                confirm=args.confirm,
                now=args.at,
            )
        elif args.command == "prepare":
            result = prepare_report(args.workspace, args.report_id, chapter_ids=args.chapter, now=args.at)
        elif args.command == "status":
            result = status_packet(args.workspace, args.report_id)
        elif args.command == "apply":
            result = apply_result(args.workspace, args.report_id, args.result, now=args.at)
        elif args.command == "audit":
            result = audit_task(args.workspace, args.report_id, complete=not args.no_complete, now=args.at)
        else:
            result = record_feedback(args.workspace, args.report_id, args.feedback, now=args.at)
    except ReadForMeFailure as exc:
        print(json.dumps({"result": "error", "safe_message": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("result") != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
