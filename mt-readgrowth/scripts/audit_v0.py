#!/usr/bin/env python3
"""Audit an imported V0 book against source, locator, and derivation invariants."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

from import_epub import normalize_search, sha256_file
from weread_credentials import CredentialFailure, key_file_path, resolve_weread_api_key


SOURCE_CONTRACTS = {
    "source/original.epub": {
        "media_type": "application/epub+zip",
        "source_format": "epub",
        "parser_source_format_required": False,
    },
    "source/original.txt": {
        "media_type": "text/plain",
        "source_format": "txt",
        "parser_source_format_required": True,
    },
}


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def yaml_scalar(content: str, key: str, indent: int = 0) -> str | None:
    match = re.search(rf"(?m)^{' ' * indent}{re.escape(key)}:\s*(.+?)\s*$", content)
    if not match or match.group(1) == "null":
        return None
    raw = match.group(1)
    try:
        return str(json.loads(raw))
    except json.JSONDecodeError:
        return raw.strip("\"'")


def check(condition: bool, message: str) -> dict[str, Any]:
    return {"passed": bool(condition), "message": message}


def resolve_preserved_source(book_dir: Path, book_yaml: str) -> tuple[Path, str, str, str | None]:
    relative_path = yaml_scalar(book_yaml, "relative_path", indent=2)
    media_type = yaml_scalar(book_yaml, "media_type", indent=2)
    parser_source_format = yaml_scalar(book_yaml, "source_format", indent=2)
    declared_sha256 = yaml_scalar(book_yaml, "sha256", indent=2)
    if relative_path not in SOURCE_CONTRACTS:
        raise ValueError("book.yaml source.relative_path must be source/original.epub or source/original.txt")
    contract = SOURCE_CONTRACTS[relative_path]
    if media_type != contract["media_type"]:
        raise ValueError("book.yaml source.media_type does not match source.relative_path")
    if parser_source_format is not None and parser_source_format != contract["source_format"]:
        raise ValueError("book.yaml parser.source_format does not match source.relative_path")
    if contract["parser_source_format_required"] and parser_source_format is None:
        raise ValueError("book.yaml parser.source_format is required for TXT")

    resolved_book = book_dir.resolve()
    source = (resolved_book / relative_path).resolve()
    try:
        source.relative_to(resolved_book)
    except ValueError as exc:
        raise ValueError("book.yaml source.relative_path resolves outside the book directory") from exc
    if not source.is_file():
        raise ValueError("book.yaml source.relative_path does not resolve to a preserved source file")
    return source, relative_path, str(contract["source_format"]), declared_sha256


def audit(book_dir: Path, workspace: Path) -> dict[str, Any]:
    book_dir = book_dir.resolve()
    workspace = workspace.resolve()
    book_yaml = (book_dir / "book.yaml").read_text(encoding="utf-8")
    stable_id = yaml_scalar(book_yaml, "stable_book_id") or ""
    status = yaml_scalar(book_yaml, "status")
    mapping_status = yaml_scalar(book_yaml, "mapping_status", indent=2)
    edition_status = yaml_scalar(book_yaml, "edition_status", indent=2)
    source, source_relative_path, source_format, declared_source_hash = resolve_preserved_source(book_dir, book_yaml)
    source_hash = sha256_file(source)
    toc = json.loads((book_dir / "parsed" / "toc.json").read_text(encoding="utf-8"))
    paragraphs = load_jsonl(book_dir / "parsed" / "paragraphs.jsonl")
    search = load_jsonl(book_dir / "search" / "normalized.jsonl")
    by_chapter: dict[str, list[dict[str, Any]]] = {}
    for paragraph in paragraphs:
        by_chapter.setdefault(paragraph["chapter_id"], []).append(paragraph)

    locators = [paragraph["locator"] for paragraph in paragraphs]
    locator_set = set(locators)
    paragraph_hashes_ok = all(
        hashlib.sha256(paragraph["text"].encode("utf-8")).hexdigest() == paragraph["text_sha256"]
        for paragraph in paragraphs
    )
    href_hashes_ok = all(
        hashlib.sha256(chapter["source_href"].encode("utf-8")).hexdigest() == chapter["source_href_sha256"]
        for chapter in toc["chapters"]
    )
    chapter_hashes_ok = True
    chapter_counts_ok = True
    for chapter in toc["chapters"]:
        values = by_chapter.get(chapter["chapter_id"], [])
        chapter_counts_ok &= len(values) == chapter["paragraph_count"]
        content = "\n\n".join(value["text"] for value in values)
        chapter_hashes_ok &= hashlib.sha256(content.encode("utf-8")).hexdigest() == chapter["content_sha256"]
    search_by_locator = {item["locator"]: item for item in search}
    search_ok = len(search) == len(paragraphs) and all(
        search_by_locator.get(paragraph["locator"], {}).get("normalized_text") == normalize_search(paragraph["text"])
        and search_by_locator.get(paragraph["locator"], {}).get("text_sha256") == paragraph["text_sha256"]
        for paragraph in paragraphs
    )
    populated = [chapter for chapter in toc["chapters"] if chapter["paragraph_count"] > 0]
    checkpoints = [populated[0], populated[len(populated) // 2], populated[-1]] if populated else []
    checkpoints_ok = len(checkpoints) == 3 and all(by_chapter.get(chapter["chapter_id"]) for chapter in checkpoints)
    chapter_files_ok = all((book_dir / "parsed" / "chapters" / f"{chapter['chapter_id']}.md").exists() for chapter in toc["chapters"])

    derived_files = [book_dir / "derived" / "book-map.md", book_dir / "derived" / "author-lens.md"]
    derived_exist = all(path.exists() for path in derived_files)
    derived_locators = []
    for path in derived_files:
        if path.exists():
            derived_locators.extend(re.findall(r"[0-9a-f]{64}#ch-\d{3}:p-\d{4}", path.read_text(encoding="utf-8")))
    derived_citations_ok = bool(derived_locators) and all(locator in locator_set for locator in derived_locators)
    analysis_state_path = book_dir / "derived" / "analysis-state.json"
    analysis_state = json.loads(analysis_state_path.read_text(encoding="utf-8")) if analysis_state_path.exists() else {}
    analysis_ready = analysis_state.get("status") == "discussion-ready"
    analysis_identity_ok = analysis_state.get("stable_book_id") == stable_id

    secret_pattern = re.compile(r"wrk-[A-Za-z0-9_-]{8,}")
    secret_hits = []
    credential_path = key_file_path(workspace).resolve()
    for path in workspace.rglob("*"):
        if not path.is_file() or path.resolve() == credential_path:
            continue
        if path.suffix.lower() in {".md", ".json", ".jsonl", ".yaml", ".yml", ".txt", ".log"} and secret_pattern.search(
            path.read_text(encoding="utf-8", errors="replace")
        ):
            secret_hits.append(str(path))
    credential_file_valid = True
    if credential_path.exists():
        try:
            resolve_weread_api_key(workspace, environ={})
        except CredentialFailure:
            credential_file_valid = False

    catalog = workspace / "catalog" / "books.jsonl"
    catalog_events = load_jsonl(catalog) if catalog.exists() else []
    catalog_ok = any(event.get("source_sha256") == stable_id and event.get("event") == "book-imported" for event in catalog_events)
    snapshot_path = workspace / "integrations" / "weread" / "books" / f"{stable_id}.json"
    notes_path = workspace / "integrations" / "weread" / "notes" / f"{stable_id}.jsonl"
    matches_path = workspace / "integrations" / "weread" / "matches" / f"{stable_id}.jsonl"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8")) if snapshot_path.exists() else {}
    note_events = load_jsonl(notes_path) if notes_path.exists() else []
    match_events = load_jsonl(matches_path) if matches_path.exists() else []
    match_statuses = [event.get("result", {}).get("status") for event in match_events]
    checks = {
        "status_text_ready": check(status == "text-ready", "book text status is text-ready"),
        "source_hash": check(
            source_hash == stable_id and declared_source_hash == stable_id,
            "preserved source and declared source hash equal stable book id",
        ),
        "toc_identity": check(toc.get("stable_book_id") == stable_id, "TOC belongs to the source hash"),
        "locator_uniqueness": check(len(locators) == len(locator_set), "all paragraph locators are unique"),
        "paragraph_hashes": check(paragraph_hashes_ok, "all display paragraph hashes verify"),
        "href_hashes": check(href_hashes_ok, "all spine href hashes verify"),
        "chapter_hashes": check(chapter_hashes_ok and chapter_counts_ok, "chapter counts and content hashes verify"),
        "search_copy": check(search_ok, "search records match display records and remain separate"),
        "content_checkpoints": check(checkpoints_ok, "first, middle, and final populated spine documents are readable"),
        "chapter_files": check(chapter_files_ok, "every spine item has a rendered chapter file"),
        "derived_files": check(derived_exist, "book-map and author-lens exist"),
        "derived_citations": check(derived_citations_ok, "all full derived locators resolve to display text"),
        "analysis_state": check(analysis_ready and analysis_identity_ok, "thought layer is discussion-ready and belongs to the source"),
        "catalog_event": check(catalog_ok, "catalog contains the immutable import event"),
        "weread_mapping": check(mapping_status == "confirmed" and edition_status == "confirmed", "WeRead book and edition mapping are confirmed"),
        "weread_snapshot": check(snapshot.get("stable_book_id") == stable_id and snapshot.get("weread_book_id"), "current WeRead metadata and progress snapshot exists"),
        "weread_note_events": check(bool(note_events) and all(event.get("event_id") for event in note_events), "normalized WeRead note events exist with stable IDs"),
        "weread_local_matches": check(bool(match_events) and all(status in {"exact", "high-confidence"} for status in match_statuses), "all synchronized V0 note texts have acceptable local matches"),
        "credential_file": check(
            credential_file_valid,
            "the optional .weread-api-key contains exactly one non-empty UTF-8 line",
        ),
        "secret_scan": check(
            not secret_hits,
            "workspace files outside .weread-api-key contain no WeRead API key pattern",
        ),
    }
    failed = [name for name, result in checks.items() if not result["passed"]]
    return {
        "result": "passed" if not failed else "failed",
        "book_dir": str(book_dir),
        "source_relative_path": source_relative_path,
        "source_format": source_format,
        "chapter_count": len(toc["chapters"]),
        "paragraph_count": len(paragraphs),
        "checkpoint_chapters": [chapter["chapter_id"] for chapter in checkpoints],
        "checks": checks,
        "failed_checks": failed,
        "secret_hits": secret_hits,
    }


def main(argv: list[str] | None = None) -> int:
    configure_console()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--book-dir", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args(argv or sys.argv[1:])
    try:
        result = audit(args.book_dir, args.workspace)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["result"] == "passed" else 1
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"result": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
