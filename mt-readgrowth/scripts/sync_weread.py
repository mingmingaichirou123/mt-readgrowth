#!/usr/bin/env python3
"""Synchronize one mapped V0 book through the installed WeRead Agent Gateway."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

from weread_credentials import CredentialFailure, resolve_weread_api_key


GATEWAY = "https://i.weread.qq.com/api/agent/gateway"
SKILL_VERSION = "1.0.4"


class SyncFailure(RuntimeError):
    pass


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize(value: str) -> str:
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", value).casefold())


def yaml_scalar(text: str, key: str, indent: int = 0) -> str | None:
    prefix = " " * indent
    match = re.search(rf"(?m)^{re.escape(prefix + key)}:\s*(.+?)\s*$", text)
    if not match:
        return None
    raw = match.group(1)
    if raw == "null":
        return None
    try:
        return str(json.loads(raw))
    except json.JSONDecodeError:
        return raw.strip("\"'")


def yaml_list(text: str, key: str) -> list[str]:
    lines = text.splitlines()
    try:
        start = lines.index(f"{key}:") + 1
    except ValueError:
        return []
    values = []
    for line in lines[start:]:
        if not line.startswith("  - "):
            break
        raw = line[4:].strip()
        try:
            values.append(str(json.loads(raw)))
        except json.JSONDecodeError:
            values.append(raw.strip("\"'"))
    return values


def book_fields(book_dir: Path) -> dict[str, Any]:
    content = (book_dir / "book.yaml").read_text(encoding="utf-8")
    return {
        "content": content,
        "stable_book_id": yaml_scalar(content, "stable_book_id"),
        "title": yaml_scalar(content, "title") or "",
        "authors": yaml_list(content, "authors"),
        "identifiers": yaml_list(content, "identifiers"),
        "weread_book_id": yaml_scalar(content, "book_id", indent=2),
        "mapping_status": yaml_scalar(content, "mapping_status", indent=2),
        "edition_status": yaml_scalar(content, "edition_status", indent=2),
        "confirmation_basis": yaml_scalar(content, "confirmation_basis", indent=2),
    }


def gateway_call(api_key: str, api_name: str, **params: Any) -> dict[str, Any]:
    body = {"api_name": api_name, **params, "skill_version": SKILL_VERSION}
    request = urllib.request.Request(
        GATEWAY,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "mt-readgrowth/0.1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise SyncFailure(f"WeRead HTTP error: {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise SyncFailure(f"WeRead network error: {type(exc.reason).__name__}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SyncFailure("WeRead returned an invalid JSON response") from exc
    upgrade = payload.get("upgrade_info")
    if upgrade:
        message = upgrade.get("message") if isinstance(upgrade, dict) else str(upgrade)
        raise SyncFailure(f"WeRead Skill upgrade required: {message}")
    if payload.get("errcode") not in (None, 0):
        raise SyncFailure(f"WeRead API error {payload.get('errcode')}: {payload.get('errmsg', 'unknown error')}")
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def review_pages(api_key: str, book_id: str) -> list[dict[str, Any]]:
    reviews: list[dict[str, Any]] = []
    synckey = 0
    for _ in range(100):
        page = gateway_call(api_key, "/review/list/mine", bookid=book_id, synckey=synckey, count=100)
        reviews.extend(page.get("reviews") or [])
        if not page.get("hasMore"):
            return reviews
        next_key = page.get("synckey")
        if next_key in (None, synckey):
            raise SyncFailure("WeRead review pagination cursor did not advance")
        synckey = next_key
    raise SyncFailure("WeRead review pagination exceeded the V0 safety limit")


def event_id(prefix: str, external_id: str | None, payload: dict[str, Any]) -> str:
    if external_id:
        return f"{prefix}:{external_id}"
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return f"{prefix}:sha256:{digest}"


def existing_event_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    ids = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                ids.add(json.loads(line)["event_id"])
            except (json.JSONDecodeError, KeyError):
                continue
    return ids


def chapter_map(bookmarks: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(chapter.get("chapterUid")): chapter for chapter in (bookmarks.get("chapters") or [])}


def build_note_events(stable_id: str, book_id: str, bookmarks: dict[str, Any], reviews: list[dict[str, Any]], synced_at: str) -> list[dict[str, Any]]:
    chapters = chapter_map(bookmarks)
    events = []
    for mark in bookmarks.get("updated") or []:
        chapter = chapters.get(str(mark.get("chapterUid")), {})
        payload = {
            "event_id": event_id("highlight", mark.get("bookmarkId"), mark),
            "event": "weread-highlight",
            "synced_at": synced_at,
            "stable_book_id": stable_id,
            "weread_book_id": book_id,
            "bookmark_id": mark.get("bookmarkId"),
            "chapter_uid": mark.get("chapterUid"),
            "chapter_idx": chapter.get("chapterIdx"),
            "chapter_title": chapter.get("title"),
            "mark_text": mark.get("markText"),
            "range": mark.get("range"),
            "create_time": mark.get("createTime"),
            "color_style": mark.get("colorStyle"),
        }
        events.append(payload)
    for wrapper in reviews:
        review = wrapper.get("review") if isinstance(wrapper, dict) else None
        review = review if isinstance(review, dict) else wrapper
        payload = {
            "event_id": event_id("review", review.get("reviewId"), review),
            "event": "weread-review",
            "synced_at": synced_at,
            "stable_book_id": stable_id,
            "weread_book_id": book_id,
            "review_id": review.get("reviewId"),
            "content": review.get("content"),
            "abstract": review.get("abstract"),
            "range": review.get("range"),
            "chapter_uid": review.get("chapterUid"),
            "chapter_idx": review.get("chapterIdx"),
            "chapter_name": review.get("chapterName"),
            "create_time": review.get("createTime"),
            "star": review.get("star"),
        }
        events.append(payload)
    return events


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    temporary.replace(path)


def append_new_events(path: Path, events: list[dict[str, Any]]) -> list[str]:
    known = existing_event_ids(path)
    new_events = [event for event in events if event["event_id"] not in known]
    if new_events:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            for event in new_events:
                handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
    return [event["event_id"] for event in new_events]


def local_isbn(identifiers: list[str]) -> str | None:
    for value in identifiers:
        digits = re.sub(r"\D", "", value)
        if len(digits) in (10, 13):
            return digits
    return None


def mapping_result(local: dict[str, Any], remote: dict[str, Any]) -> tuple[str, str]:
    title_ok = normalize(local["title"]) == normalize(str(remote.get("title") or ""))
    remote_author = normalize(str(remote.get("author") or ""))
    author_ok = any(normalize(author) in remote_author or remote_author in normalize(author) for author in local["authors"] if remote_author)
    if not title_ok or not author_ok:
        return "mismatch", "mismatch"
    local_code = local_isbn(local["identifiers"])
    remote_code = re.sub(r"\D", "", str(remote.get("isbn") or "")) or None
    if local_code and remote_code:
        return ("confirmed", "confirmed") if local_code == remote_code else ("candidate", "mismatch")
    if local.get("mapping_status") == "confirmed" and local.get("edition_status") == "confirmed":
        return "confirmed", "confirmed"
    return "candidate", "needs-confirmation"


def update_mapping(
    book_dir: Path,
    local: dict[str, Any],
    mapping_status: str,
    edition_status: str,
    at: str,
    confirmation_basis: str | None = None,
) -> None:
    path = book_dir / "book.yaml"
    content = local["content"]
    replacements = {
        r"(?m)^  mapping_status:.*$": f'  mapping_status: "{mapping_status}"',
        r"(?m)^  edition_status:.*$": f'  edition_status: "{edition_status}"',
        r"(?m)^  confirmed_at:.*$": f'  confirmed_at: {json.dumps(at) if mapping_status == "confirmed" else "null"}',
    }
    for pattern, replacement in replacements.items():
        content, count = re.subn(pattern, replacement, content, count=1)
        if count != 1:
            raise SyncFailure("book.yaml mapping fields are incomplete")
    if mapping_status == "confirmed" and confirmation_basis:
        if re.search(r"(?m)^  confirmation_basis:.*$", content):
            content = re.sub(
                r"(?m)^  confirmation_basis:.*$",
                f"  confirmation_basis: {json.dumps(confirmation_basis)}",
                content,
                count=1,
            )
        else:
            content = re.sub(
                r"(?m)^(  confirmed_at:.*)$",
                r"\1\n  confirmation_basis: " + json.dumps(confirmation_basis),
                content,
                count=1,
            )
    temporary = path.with_suffix(".yaml.tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)


def replace_book_sync_block(existing: str, stable_id: str, book_id: str, at: str) -> str:
    """Replace one per-book state while preserving node-1 shelf fields and other books."""
    lines = existing.splitlines()
    try:
        books_start = lines.index("books:")
    except ValueError:
        books_start = len(lines)
        lines.append("books:")
    books_end = len(lines)
    for index in range(books_start + 1, len(lines)):
        if lines[index] and not lines[index].startswith((" ", "\t")):
            books_end = index
            break

    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines[books_start + 1 : books_end]:
        if re.match(r"^  (?:\".*\"|[^ ].*):\s*$", line):
            if current:
                blocks.append(current)
            current = [line]
        elif current:
            current.append(line)
    if current:
        blocks.append(current)

    def block_id(block: list[str]) -> str | None:
        raw = block[0].strip()[:-1]
        try:
            return str(json.loads(raw))
        except json.JSONDecodeError:
            return raw.strip("\"'")

    blocks = [block for block in blocks if block_id(block) != stable_id]
    blocks.append([
        f"  {json.dumps(stable_id)}:",
        f"    weread_book_id: {json.dumps(book_id)}",
        f"    last_notes_sync_at: {json.dumps(at)}",
        f"    last_progress_sync_at: {json.dumps(at)}",
        "    cursor: null",
    ])
    replacement = ["books:"] + [line for block in blocks for line in block]
    return "\n".join(lines[:books_start] + replacement + lines[books_end:]) + "\n"


def write_sync_state(workspace: Path, stable_id: str, book_id: str, at: str) -> None:
    path = workspace / "integrations" / "weread" / "sync-state.yaml"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    with_book = replace_book_sync_block(existing, stable_id, book_id, at)
    from sync_library import render_sync_state

    content = render_sync_state(with_book, error=None)
    temporary = path.with_suffix(".yaml.tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)


def sync(book_dir: Path, workspace: Path, api_key: str) -> dict[str, Any]:
    book_dir = book_dir.resolve()
    workspace = workspace.resolve()
    local = book_fields(book_dir)
    stable_id = local["stable_book_id"]
    book_id = local["weread_book_id"]
    if not stable_id or not book_id:
        raise SyncFailure("book.yaml has no candidate WeRead book mapping")
    info = gateway_call(api_key, "/book/info", bookId=book_id)
    mapping_status, edition_status = mapping_result(local, info)
    local_code = local_isbn(local["identifiers"])
    remote_code = re.sub(r"\D", "", str(info.get("isbn") or "")) or None
    confirmation_basis = (
        "remote-local-isbn-exact"
        if local_code and remote_code and local_code == remote_code
        else local.get("confirmation_basis")
    )
    update_mapping(book_dir, local, mapping_status, edition_status, utc_now(), confirmation_basis)
    if edition_status == "mismatch":
        raise SyncFailure("Local source and WeRead metadata indicate an edition mismatch")
    if mapping_status != "confirmed" or edition_status != "confirmed":
        raise SyncFailure("WeRead edition requires explicit confirmation before notes synchronization")

    chapters = gateway_call(api_key, "/book/chapterinfo", bookId=book_id)
    progress = gateway_call(api_key, "/book/getprogress", bookId=book_id)
    bookmarks = gateway_call(api_key, "/book/bookmarklist", bookId=book_id)
    reviews = review_pages(api_key, book_id)
    synced_at = utc_now()

    snapshot = {
        "schema_version": 1,
        "synced_at": synced_at,
        "stable_book_id": stable_id,
        "weread_book_id": book_id,
        "book": info,
        "chapters": chapters,
        "progress": progress,
    }
    write_json(workspace / "integrations" / "weread" / "books" / f"{stable_id}.json", snapshot)
    note_path = workspace / "integrations" / "weread" / "notes" / f"{stable_id}.jsonl"
    all_events = build_note_events(stable_id, book_id, bookmarks, reviews, synced_at)
    new_event_ids = append_new_events(note_path, all_events)
    update_mapping(book_dir, local, mapping_status, edition_status, synced_at, confirmation_basis)
    write_sync_state(workspace, stable_id, book_id, synced_at)
    return {
        "result": "synced",
        "synced_at": synced_at,
        "mapping_status": mapping_status,
        "edition_status": edition_status,
        "highlight_count": len(bookmarks.get("updated") or []),
        "review_count": len(reviews),
        "note_event_count": len(all_events),
        "new_note_events": len(new_event_ids),
        "new_event_ids": new_event_ids,
        "progress": (progress.get("book") or {}).get("progress"),
    }


def main(argv: list[str] | None = None) -> int:
    configure_console()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--book-dir", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args(argv or sys.argv[1:])
    try:
        api_key = resolve_weread_api_key(args.workspace)
    except CredentialFailure as exc:
        print(json.dumps({"result": "blocked", "reason": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 3
    try:
        print(json.dumps(sync(args.book_dir, args.workspace, api_key), ensure_ascii=False, indent=2))
        return 0
    except SyncFailure as exc:
        print(json.dumps({"result": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
