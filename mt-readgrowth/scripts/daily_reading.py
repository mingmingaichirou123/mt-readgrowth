#!/usr/bin/env python3
"""Initialize a workspace and run the V1 manual reading flows."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

from import_epub import ImportFailure, import_epub
from import_txt import (
    DEFAULT_MAX_CHAPTER_CHARS,
    DEFAULT_MAX_CHAPTER_PARAGRAPHS,
    import_txt,
    inspect_txt,
)
from locate_weread_notes import locate_notes
from map_weread_book import map_book
from profile_store import (
    INITIALIZATION_MEMORY_MODE,
    ProfileStoreFailure,
    atomic_json as atomic_profile_json,
    get_memory_settings,
    initialization_memory_settings,
)
from sync_library import rebuild_overview
from weread_credentials import CredentialFailure, resolve_weread_api_key
import sync_weread


FLOW_VERSION = "0.5.2"

WORKSPACE_CONFIG = (
    "schema_version: 1\n"
    "workspace_id: personal-reading\n"
    "skill_name: mt-readgrowth\n"
    "catalog: catalog/books.jsonl\n"
    "books_dir: books\n"
    "sessions_dir: sessions\n"
    "weread_dir: integrations/weread\n"
)

WORKSPACE_DIRECTORIES = (
    "inbox",
    "integrations/weread",
    "integrations/weread/books",
    "integrations/weread/notes",
    "integrations/weread/matches",
    "integrations/weread/reports",
    "catalog",
    "books",
    "sessions",
    "knowledge/beliefs",
    "knowledge/weekly",
    "knowledge/read-for-me",
    "knowledge/recommendations",
    "knowledge/user-profile",
    "cache",
    "logs",
    "trash",
)


class DailyFlowFailure(RuntimeError):
    pass


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    temporary.replace(path)


def atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8", newline="\n")
    temporary.replace(path)


def initialize_workspace(workspace: Path) -> dict[str, Any]:
    """Create the empty workspace contract and its safe user-profile default."""
    workspace = workspace.resolve()
    if workspace.exists() and not workspace.is_dir():
        raise DailyFlowFailure(f"Workspace path is not a directory: {workspace}")
    workspace.mkdir(parents=True, exist_ok=True)

    config_path = workspace / "workspace.yaml"
    config_created = False
    if config_path.exists():
        current = config_path.read_text(encoding="utf-8").replace("\r\n", "\n")
        if current.rstrip("\n") + "\n" != WORKSPACE_CONFIG:
            raise DailyFlowFailure(
                "workspace.yaml conflicts with the MT-readgrowth workspace contract; refusing to overwrite"
            )
    for relative in WORKSPACE_DIRECTORIES:
        directory = workspace / relative
        if directory.exists() and not directory.is_dir():
            raise DailyFlowFailure(f"Workspace directory path is occupied by a file: {relative}")

    if not config_path.exists():
        atomic_text(config_path, WORKSPACE_CONFIG)
        config_created = True

    created_directories = []
    for relative in WORKSPACE_DIRECTORIES:
        directory = workspace / relative
        if not directory.exists():
            directory.mkdir(parents=True)
            created_directories.append(relative)

    profile_settings_path = workspace / "knowledge" / "user-profile" / "settings.json"
    profile_settings_created = False
    try:
        if not profile_settings_path.exists() and not profile_settings_path.is_symlink():
            atomic_profile_json(profile_settings_path, initialization_memory_settings())
            profile_settings_created = True
        settings = get_memory_settings(workspace)
    except (OSError, ProfileStoreFailure) as exc:
        raise DailyFlowFailure(f"user-profile settings conflict with the workspace contract: {exc}") from exc

    changed = config_created or bool(created_directories) or profile_settings_created
    return {
        "result": "initialized" if changed else "unchanged",
        "flow_version": FLOW_VERSION,
        "workspace": str(workspace),
        "workspace_config": str(config_path),
        "config_created": config_created,
        "created_directories": created_directories,
        "profile_settings": str(profile_settings_path),
        "profile_settings_created": profile_settings_created,
        "memory_mode": settings["memory_mode"],
        "default_memory_mode": INITIALIZATION_MEMORY_MODE,
        "reading_data_created": False,
        "credential_created": False,
        "next_action": "choose import, WeRead sync, or TXT inspection",
    }


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise DailyFlowFailure(f"Expected a JSON object: {path}")
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DailyFlowFailure(f"Invalid JSONL at {path}:{number}") from exc
        if isinstance(value, dict):
            records.append(value)
    return records


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(canonical_json(value) + "\n")


def ensure_workspace_book(workspace: Path, book_dir: Path) -> Path:
    workspace = workspace.resolve()
    book_dir = book_dir.resolve()
    books_root = (workspace / "books").resolve()
    if book_dir.parent != books_root or not (book_dir / "book.yaml").is_file():
        raise DailyFlowFailure("book-dir must be an imported book directly under workspace/books")
    return book_dir


def unified_import(
    source: Path,
    workspace: Path,
    *,
    title: str | None = None,
    authors: list[str] | None = None,
    language: str | None = None,
    encoding: str | None = None,
    confirm_no_headings: bool = False,
    max_chapter_chars: int = DEFAULT_MAX_CHAPTER_CHARS,
    max_chapter_paragraphs: int = DEFAULT_MAX_CHAPTER_PARAGRAPHS,
) -> dict[str, Any]:
    source = source.resolve()
    workspace = workspace.resolve()
    suffix = source.suffix.casefold()
    if suffix == ".epub":
        result = import_epub(source, workspace)
        source_format = "epub"
    elif suffix == ".txt":
        inspection = inspect_txt(
            source,
            encoding=encoding,
            max_chapter_chars=max_chapter_chars,
            max_chapter_paragraphs=max_chapter_paragraphs,
        )
        if inspection.get("result") == "needs-confirmation" and not (
            inspection.get("reason") == "no-chapter-headings" and confirm_no_headings
        ):
            return {
                "result": "needs-confirmation",
                "flow_version": FLOW_VERSION,
                "stage": "txt-inspection",
                "inspection": inspection,
            }
        result = import_txt(
            source,
            workspace,
            title=title,
            authors=authors,
            language=language,
            encoding=encoding,
            confirm_no_headings=confirm_no_headings,
            max_chapter_chars=max_chapter_chars,
            max_chapter_paragraphs=max_chapter_paragraphs,
        )
        source_format = "txt"
    else:
        raise DailyFlowFailure("Unified import accepts only .epub or .txt files")

    if (workspace / "integrations" / "weread" / "shelf.json").exists():
        rebuild_overview(workspace)
        queue = refresh_version_queue(workspace)
    else:
        queue = {"pending_count": 0, "reason": "no-shelf-snapshot"}
    return {
        **result,
        "flow_version": FLOW_VERSION,
        "source_format": source_format,
        "version_queue": queue,
        "next_action": (
            "run derive_book.py prepare-overview and review before apply-overview"
            if result.get("result") == "imported"
            else "reuse the existing imported book"
        ),
    }


def decision_state(workspace: Path) -> dict[str, dict[str, Any]]:
    path = workspace / "integrations" / "weread" / "version-decisions.jsonl"
    latest: dict[str, dict[str, Any]] = {}
    for event in load_jsonl(path):
        candidate_id = event.get("candidate_id")
        if candidate_id:
            latest[str(candidate_id)] = event
    return latest


def candidate_id(provider_identity: str, stable_book_id: str) -> str:
    digest = hashlib.sha256(f"{provider_identity}\n{stable_book_id}".encode("utf-8")).hexdigest()
    return f"version:{digest[:24]}"


def refresh_version_queue(workspace: Path, *, now: str | None = None) -> dict[str, Any]:
    workspace = workspace.resolve()
    overview_path = workspace / "catalog" / "overview.json"
    if not overview_path.exists():
        raise DailyFlowFailure("catalog/overview.json is missing; build the reading-system overview first")
    overview = load_json(overview_path)
    decisions = decision_state(workspace)
    pending = []
    resolved = []
    for entry in overview.get("entries") or []:
        if entry.get("provider_type") != "book" or entry.get("link_status") != "needs-confirmation":
            continue
        stable_id = entry.get("stable_book_id")
        provider_identity = entry.get("provider_identity")
        if not stable_id or not provider_identity:
            continue
        item = {
            "candidate_id": candidate_id(str(provider_identity), str(stable_id)),
            "provider_identity": provider_identity,
            "weread_book_id": entry.get("weread_book_id"),
            "weread_title": entry.get("title"),
            "weread_author": entry.get("author"),
            "stable_book_id": stable_id,
            "book_dir": entry.get("book_dir"),
            "link_basis": entry.get("link_basis"),
            "evidence": {
                "title_author_only": entry.get("link_basis") in {"exact-title-author", "base-title-author"},
                "source_hash_is_local_only": True,
                "edition_confirmed": False,
            },
            "required_action": "confirm or reject explicitly; do not synchronize notes while pending",
        }
        decision = decisions.get(item["candidate_id"])
        if decision:
            resolved.append({**item, "decision": decision.get("decision"), "decided_at": decision.get("at")})
        else:
            pending.append(item)
    queue = {
        "schema_version": 1,
        "flow_version": FLOW_VERSION,
        "generated_at": now or utc_now(),
        "source": overview.get("source"),
        "pending_count": len(pending),
        "pending": pending,
        "resolved_count": len(resolved),
        "resolved": resolved,
    }
    atomic_json(workspace / "integrations" / "weread" / "version-confirmations.json", queue)
    return queue


def decide_version(
    workspace: Path,
    candidate: str,
    decision: str,
    *,
    now: str | None = None,
) -> dict[str, Any]:
    workspace = workspace.resolve()
    queue_path = workspace / "integrations" / "weread" / "version-confirmations.json"
    queue = load_json(queue_path) if queue_path.exists() else refresh_version_queue(workspace)
    item = next((row for row in queue.get("pending") or [] if row.get("candidate_id") == candidate), None)
    if not item:
        raise DailyFlowFailure("candidate-id is not pending in the current version queue")
    decided_at = now or utc_now()
    mapping = None
    if decision == "confirm":
        relative = item.get("book_dir")
        if not relative:
            raise DailyFlowFailure("version candidate has no local book directory")
        book_dir = ensure_workspace_book(workspace, workspace / str(relative))
        mapping = map_book(
            book_dir,
            str(item["weread_book_id"]),
            str(item.get("weread_title") or ""),
            str(item.get("weread_author") or ""),
            True,
            "user-confirmed-version-queue",
        )
        if mapping.get("mapping_status") != "confirmed" or mapping.get("edition_status") != "confirmed":
            raise DailyFlowFailure("candidate metadata no longer supports confirmation")
    event = {
        "schema_version": 1,
        "event": "weread-version-decision",
        "event_id": f"decision:{candidate}:{decided_at}",
        "at": decided_at,
        "candidate_id": candidate,
        "decision": decision,
        "provider_identity": item.get("provider_identity"),
        "weread_book_id": item.get("weread_book_id"),
        "stable_book_id": item.get("stable_book_id"),
        "basis": "explicit-user-decision",
    }
    append_jsonl(workspace / "integrations" / "weread" / "version-decisions.jsonl", event)
    rebuild_overview(workspace)
    refreshed = refresh_version_queue(workspace, now=decided_at)
    return {"result": "recorded", "decision_event": event, "mapping": mapping, "queue": refreshed}


def progress_value(snapshot: dict[str, Any] | None) -> Any:
    if not snapshot:
        return None
    return ((snapshot.get("progress") or {}).get("book") or {}).get("progress")


def route_for_match(book_dir: Path, match: dict[str, Any]) -> dict[str, Any] | None:
    result = match.get("result") or {}
    if result.get("status") not in {"exact", "high-confidence"}:
        return None
    candidates = result.get("candidates") or []
    if len(candidates) != 1:
        return None
    candidate = candidates[0]
    chapter_id = candidate.get("chapter_id")
    state = load_json(book_dir / "derived" / "analysis-state.json")
    completed = set((state.get("chapters") or {}).get("completed") or [])
    prepare_required = chapter_id not in completed
    return {
        "source_event_id": match.get("source_event_id"),
        "match_status": result.get("status"),
        "locator": candidate.get("locator"),
        "chapter_id": chapter_id,
        "prepare_chapter_required": prepare_required,
        "next_steps": (
            ["derive_book.py prepare-chapter", "human review", "derive_book.py apply-chapter", "build_context.py"]
            if prepare_required
            else ["build_context.py"]
        ),
    }


def sync_current_book(
    workspace: Path,
    book_dir: Path,
    api_key: str,
    *,
    now: str | None = None,
) -> dict[str, Any]:
    workspace = workspace.resolve()
    book_dir = ensure_workspace_book(workspace, book_dir)
    local = sync_weread.book_fields(book_dir)
    stable_id = str(local.get("stable_book_id") or "")
    if not stable_id:
        raise DailyFlowFailure("book.yaml has no stable_book_id")
    snapshot_path = workspace / "integrations" / "weread" / "books" / f"{stable_id}.json"
    before_snapshot = load_json(snapshot_path) if snapshot_path.exists() else None
    note_path = workspace / "integrations" / "weread" / "notes" / f"{stable_id}.jsonl"
    before_ids = {str(row.get("event_id")) for row in load_jsonl(note_path)}

    sync_result = sync_weread.sync(book_dir, workspace, api_key)
    all_events = load_jsonl(note_path)
    new_events = [row for row in all_events if str(row.get("event_id")) not in before_ids]
    new_ids = {str(row.get("event_id")) for row in new_events}
    match_path = workspace / "integrations" / "weread" / "matches" / f"{stable_id}.jsonl"
    locate_notes(book_dir, note_path, match_path)
    matches = [row for row in load_jsonl(match_path) if str(row.get("source_event_id")) in new_ids]
    by_source = {str(row.get("source_event_id")): row for row in matches}
    routes = []
    unlocated = []
    non_locatable = []
    status_counts: dict[str, int] = {}
    for event in new_events:
        match = by_source.get(str(event.get("event_id")))
        status = ((match or {}).get("result") or {}).get("status", "not-found")
        status_counts[status] = status_counts.get(status, 0) + 1
        route = route_for_match(book_dir, match) if match else None
        if route:
            routes.append(route)
        elif event.get("mark_text") or event.get("abstract"):
            unlocated.append({
                "source_event_id": event.get("event_id"),
                "event": event.get("event"),
                "status": status,
                "reason": (((match or {}).get("result") or {}).get("reason") or "no match event"),
            })
        else:
            non_locatable.append({
                "source_event_id": event.get("event_id"),
                "event": event.get("event"),
                "reason": "provider event has no source text to locate",
            })

    after_snapshot = load_json(snapshot_path)
    previous_progress = progress_value(before_snapshot)
    current_progress = progress_value(after_snapshot)
    generated_at = now or utc_now()
    report = {
        "schema_version": 1,
        "flow_version": FLOW_VERSION,
        "result": "synced-current-book",
        "generated_at": generated_at,
        "scope": {
            "stable_book_id": stable_id,
            "weread_book_id": local.get("weread_book_id"),
            "api_calls": [
                "/book/info",
                "/book/chapterinfo",
                "/book/getprogress",
                "/book/bookmarklist",
                "/review/list/mine",
            ],
            "full_shelf_sync": False,
        },
        "progress": {
            "previous": previous_progress,
            "current": current_progress,
            "changed": previous_progress != current_progress,
        },
        "notes": {
            "fetched_events": sync_result.get("note_event_count", len(all_events)),
            "new": len(new_events),
            "unchanged": max(0, int(sync_result.get("note_event_count", 0)) - len(new_events)),
            "new_highlights": sum(row.get("event") == "weread-highlight" for row in new_events),
            "new_reviews": sum(row.get("event") == "weread-review" for row in new_events),
        },
        "location": {
            "new_event_status_counts": status_counts,
            "discussion_routes": routes,
            "unlocated": unlocated,
            "non_locatable": non_locatable,
        },
        "idempotent": len(new_events) == 0,
    }
    atomic_json(workspace / "integrations" / "weread" / "reports" / f"{stable_id}.json", report)
    return report


def normalized(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value).casefold())


def topic_score(topic: str | None, value: str) -> int:
    query = normalized(topic or "")
    if not query:
        return 0
    target = normalized(value)
    tokens = {query}
    if len(query) >= 2:
        tokens.update(query[index : index + 2] for index in range(len(query) - 1))
    return sum(4 if token == query else 1 for token in tokens if token and token in target)


def guide_packet(book_dir: Path, topic: str | None = None, limit: int = 3) -> dict[str, Any]:
    book_dir = book_dir.resolve()
    local = sync_weread.book_fields(book_dir)
    overview_path = book_dir / "derived" / "overview-result.json"
    state_path = book_dir / "derived" / "analysis-state.json"
    if not overview_path.exists() or not state_path.exists():
        raise DailyFlowFailure("validated overview artifacts are missing")
    overview = load_json(overview_path)
    state = load_json(state_path)
    if state.get("status") != "discussion-ready":
        raise DailyFlowFailure("book thought layer is not discussion-ready")
    if not (book_dir / "derived" / "book-map.md").exists() or not (book_dir / "derived" / "author-lens.md").exists():
        raise DailyFlowFailure("book-map.md or author-lens.md is missing")
    available_locators = {
        str(row.get("locator")) for row in load_jsonl(book_dir / "parsed" / "paragraphs.jsonl")
    }
    completed = set((state.get("chapters") or {}).get("completed") or [])
    recommendations = []
    for order, section in enumerate(overview.get("sections") or []):
        citations = [str(value) for value in section.get("citations") or []]
        if not citations or any(value not in available_locators for value in citations):
            raise DailyFlowFailure("overview contains an unresolved recommendation citation")
        chapters = [str(value) for value in section.get("chapter_ids") or []]
        blob = " ".join([
            str(section.get("title") or ""),
            str(section.get("statement") or ""),
            str(section.get("support") or ""),
        ])
        existing_claims = []
        for chapter_id in chapters:
            chapter_result = book_dir / "derived" / "chapter-results" / f"{chapter_id}.json"
            if chapter_result.exists():
                chapter = load_json(chapter_result)
                for claim in (chapter.get("claims") or [])[:2]:
                    claim_citations = [str(value) for value in claim.get("citations") or []]
                    if not claim_citations or any(value not in available_locators for value in claim_citations):
                        raise DailyFlowFailure("chapter result contains an unresolved guide citation")
                    existing_claims.append({
                        "title": claim.get("title"),
                        "evidence_class": claim.get("evidence_class"),
                        "citations": claim_citations,
                    })
        recommendations.append({
            "title": section.get("title"),
            "chapter_ids": chapters,
            "reason": section.get("statement"),
            "evidence_class": section.get("evidence_class"),
            "citations": citations,
            "conditions": section.get("conditions") or [],
            "chapter_analysis": {
                chapter_id: "generated" if chapter_id in completed else "overview-only" for chapter_id in chapters
            },
            "existing_chapter_claims": existing_claims,
            "topic_score": topic_score(topic, blob),
            "source_order": order,
        })
    recommendations.sort(key=lambda row: (-int(row["topic_score"]), int(row["source_order"])))
    sections = overview.get("sections") or []
    lenses = overview.get("lenses") or []
    return {
        "schema_version": 1,
        "flow_version": FLOW_VERSION,
        "result": "evidence-guide",
        "book": {
            "stable_book_id": local.get("stable_book_id"),
            "title": local.get("title"),
            "authors": local.get("authors"),
        },
        "topic": topic,
        "introduction": {
            "route": [{
                "title": item.get("title"),
                "statement": item.get("statement"),
                "evidence_class": item.get("evidence_class"),
                "citations": item.get("citations") or [],
            } for item in sections],
            "author_lenses": [{
                "title": item.get("title"),
                "statement": item.get("statement"),
                "evidence_class": item.get("evidence_class"),
                "citations": item.get("citations") or [],
            } for item in lenses],
        },
        "recommendations": recommendations[:limit],
        "scope_limits": overview.get("scope_limits") or [],
        "source_artifacts": [
            "derived/overview-result.json",
            "derived/book-map.md",
            "derived/author-lens.md",
            "derived/chapter-results/<chapter-id>.json when generated",
        ],
        "rendering_rules": {
            "separate_evidence_classes": ["作者明确表达", "作者立场推断", "AI延伸应用"],
            "do_not_call_overview_sampling_full_chapter_proof": True,
            "recommendation_is_ai_routing": True,
        },
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    init_parser = sub.add_parser("init")
    init_parser.add_argument("--workspace", type=Path, required=True)

    import_parser = sub.add_parser("import")
    import_parser.add_argument("source", type=Path)
    import_parser.add_argument("--workspace", type=Path, required=True)
    import_parser.add_argument("--title")
    import_parser.add_argument("--author", action="append", default=[])
    import_parser.add_argument("--language")
    import_parser.add_argument("--encoding")
    import_parser.add_argument("--confirm-no-headings", action="store_true")
    import_parser.add_argument("--max-chapter-chars", type=int, default=DEFAULT_MAX_CHAPTER_CHARS)
    import_parser.add_argument("--max-chapter-paragraphs", type=int, default=DEFAULT_MAX_CHAPTER_PARAGRAPHS)

    versions = sub.add_parser("versions")
    versions.add_argument("--workspace", type=Path, required=True)

    decide = sub.add_parser("decide-version")
    decide.add_argument("--workspace", type=Path, required=True)
    decide.add_argument("--candidate-id", required=True)
    decide.add_argument("--decision", choices=("confirm", "reject"), required=True)

    sync_parser = sub.add_parser("sync-current")
    sync_parser.add_argument("--workspace", type=Path, required=True)
    sync_parser.add_argument("--book-dir", type=Path, required=True)

    guide = sub.add_parser("guide")
    guide.add_argument("--book-dir", type=Path, required=True)
    guide.add_argument("--topic")
    guide.add_argument("--limit", type=int, choices=range(1, 11), default=3)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    configure_console()
    args = parse_args(argv or sys.argv[1:])
    try:
        if args.command == "init":
            result = initialize_workspace(args.workspace)
        elif args.command == "import":
            result = unified_import(
                args.source,
                args.workspace,
                title=args.title,
                authors=args.author,
                language=args.language,
                encoding=args.encoding,
                confirm_no_headings=args.confirm_no_headings,
                max_chapter_chars=args.max_chapter_chars,
                max_chapter_paragraphs=args.max_chapter_paragraphs,
            )
        elif args.command == "versions":
            result = refresh_version_queue(args.workspace)
        elif args.command == "decide-version":
            result = decide_version(args.workspace, args.candidate_id, args.decision)
        elif args.command == "sync-current":
            try:
                api_key = resolve_weread_api_key(args.workspace)
            except CredentialFailure as exc:
                print(json.dumps({"result": "blocked", "reason": str(exc)}, ensure_ascii=False), file=sys.stderr)
                return 3
            result = sync_current_book(args.workspace, args.book_dir, api_key)
        else:
            result = guide_packet(args.book_dir, args.topic, args.limit)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 3 if result.get("result") == "needs-confirmation" else 0
    except (DailyFlowFailure, ImportFailure, sync_weread.SyncFailure, OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"result": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
