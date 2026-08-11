#!/usr/bin/env python3
"""Synchronize a lightweight WeRead shelf and build the V1 reading-system overview."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

sys.dont_write_bytecode = True

from weread_credentials import CredentialFailure, resolve_weread_api_key


GATEWAY = "https://i.weread.qq.com/api/agent/gateway"
SKILL_VERSION = "1.0.4"
SCRIPT_VERSION = "0.4.0"
STABLE_ID_RE = re.compile(r"^[0-9a-f]{64}$")
TEXT_STATES = {
    "registered",
    "source-missing",
    "source-imported",
    "needs-confirmation",
    "text-ready",
    "mismatch",
    "error",
}
ANALYSIS_STATES = {"not-started", "partial", "discussion-ready", "failed"}


class ShelfSyncFailure(RuntimeError):
    def __init__(self, code: str, safe_message: str):
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)


def write_json(path: Path, value: Any) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def yaml_scalar(text: str, key: str, indent: int = 0) -> str | None:
    prefix = " " * indent
    match = re.search(rf"(?m)^{re.escape(prefix + key)}:\s*(.+?)\s*$", text)
    if not match:
        return None
    raw = match.group(1)
    if raw == "null":
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = raw.strip("\"'")
    return str(value)


def yaml_list(text: str, key: str) -> list[str]:
    lines = text.splitlines()
    try:
        start = lines.index(f"{key}:") + 1
    except ValueError:
        return []
    values: list[str] = []
    for line in lines[start:]:
        if not line.startswith("  - "):
            break
        raw = line[4:].strip()
        try:
            values.append(str(json.loads(raw)))
        except json.JSONDecodeError:
            values.append(raw.strip("\"'"))
    return values


def top_level_block(text: str, key: str) -> list[str]:
    lines = text.splitlines()
    try:
        start = lines.index(f"{key}:")
    except ValueError:
        return []
    result = [lines[start]]
    for line in lines[start + 1 :]:
        if line and not line.startswith((" ", "\t")):
            break
        result.append(line)
    while result and not result[-1].strip():
        result.pop()
    return result


def render_sync_state(
    existing: str,
    *,
    synced_at: str | None = None,
    snapshot_sha256: str | None = None,
    entry_count: int | None = None,
    error: dict[str, str] | None = None,
) -> str:
    old_at = yaml_scalar(existing, "last_shelf_sync_at")
    old_hash = yaml_scalar(existing, "last_shelf_snapshot_sha256")
    old_count = yaml_scalar(existing, "shelf_entry_count")
    books = top_level_block(existing, "books") or ["books:"]
    count_value: int | None = entry_count
    if count_value is None and old_count is not None:
        try:
            count_value = int(old_count)
        except ValueError:
            count_value = None
    lines = [
        "schema_version: 2",
        "provider: weread",
        f"provider_skill_version: {json.dumps(SKILL_VERSION)}",
        f"last_shelf_sync_at: {json.dumps(synced_at or old_at) if (synced_at or old_at) else 'null'}",
        f"last_shelf_snapshot_sha256: {json.dumps(snapshot_sha256 or old_hash) if (snapshot_sha256 or old_hash) else 'null'}",
        f"shelf_entry_count: {count_value if count_value is not None else 'null'}",
        *books,
    ]
    if error is None:
        lines.append("last_error: null")
    else:
        lines.extend(
            [
                "last_error:",
                f"  code: {json.dumps(error['code'])}",
                f"  at: {json.dumps(error['at'])}",
                f"  safe_message: {json.dumps(error['safe_message'], ensure_ascii=False)}",
            ]
        )
    return "\n".join(lines) + "\n"


def sync_state_path(workspace: Path) -> Path:
    return workspace / "integrations" / "weread" / "sync-state.yaml"


def update_sync_state_success(workspace: Path, synced_at: str, snapshot_hash: str, entry_count: int) -> None:
    path = sync_state_path(workspace)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    atomic_write(
        path,
        render_sync_state(
            existing,
            synced_at=synced_at,
            snapshot_sha256=snapshot_hash,
            entry_count=entry_count,
        ),
    )


def update_sync_state_error(workspace: Path, failure: ShelfSyncFailure, at: str) -> None:
    path = sync_state_path(workspace)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    atomic_write(
        path,
        render_sync_state(
            existing,
            error={"code": failure.code, "at": at, "safe_message": failure.safe_message},
        ),
    )


def gateway_call(api_key: str, api_name: str, **params: Any) -> dict[str, Any]:
    body = {"api_name": api_name, **params, "skill_version": SKILL_VERSION}
    request = urllib.request.Request(
        GATEWAY,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": f"mt-readgrowth/{SCRIPT_VERSION}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ShelfSyncFailure("http-error", f"WeRead request failed with HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ShelfSyncFailure("network-error", "WeRead network request failed") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ShelfSyncFailure("invalid-json", "WeRead returned invalid JSON") from exc
    if payload.get("upgrade_info"):
        raise ShelfSyncFailure("upgrade-required", "The installed WeRead Skill must be upgraded before synchronization")
    if payload.get("errcode") not in (None, 0):
        raise ShelfSyncFailure("provider-error", f"WeRead API returned error code {payload.get('errcode')}")
    data = payload.get("data")
    result = data if isinstance(data, dict) else payload
    if not isinstance(result, dict):
        raise ShelfSyncFailure("invalid-response", "WeRead shelf response is not an object")
    return result


def normalized(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[\W_]+", "", text)


def title_forms(title: str) -> set[str]:
    full = normalized(title)
    base = normalized(re.split(r"[：:]", title, maxsplit=1)[0])
    return {value for value in (full, base) if value}


def authors_match(remote_author: str, local_authors: list[str]) -> bool:
    remote = normalized(remote_author)
    if not remote:
        return False
    return any(
        local and (local in remote or remote in local)
        for local in (normalized(author) for author in local_authors)
    )


def bool_value(value: Any) -> bool:
    return value in (True, 1, "1")


def normalize_shelf(payload: dict[str, Any], synced_at: str) -> dict[str, Any]:
    books = payload.get("books") or []
    albums = payload.get("albums") or []
    mp = payload.get("mp")
    if not isinstance(books, list) or not isinstance(albums, list):
        raise ShelfSyncFailure("invalid-response", "WeRead shelf books or albums is not a list")
    entries: list[dict[str, Any]] = []
    identities: set[str] = set()

    def add(entry: dict[str, Any]) -> None:
        identity = entry["provider_identity"]
        if identity in identities:
            raise ShelfSyncFailure("duplicate-identity", "WeRead shelf returned a duplicate provider identity")
        identities.add(identity)
        entry["shelf_order"] = len(entries) + 1
        entries.append(entry)

    for raw in books:
        if not isinstance(raw, dict) or not str(raw.get("bookId") or "").strip():
            raise ShelfSyncFailure("missing-identity", "A WeRead shelf book has no bookId")
        item_id = str(raw["bookId"])
        add(
            {
                "provider_identity": f"weread:book:{item_id}",
                "provider": "weread",
                "provider_type": "book",
                "provider_item_id": item_id,
                "weread_book_id": item_id,
                "title": str(raw.get("title") or "未命名书籍"),
                "author": str(raw.get("author") or ""),
                "category": raw.get("category"),
                "read_update_time": raw.get("readUpdateTime"),
                "finish_reading": bool_value(raw.get("finishReading")),
                "update_time": raw.get("updateTime"),
                "is_top": bool_value(raw.get("isTop")),
                "is_private": bool_value(raw.get("secret")),
                "deep_link": raw.get("deepLink"),
            }
        )
    for raw in albums:
        if not isinstance(raw, dict):
            raise ShelfSyncFailure("invalid-response", "A WeRead shelf album is not an object")
        info = raw.get("albumInfo") or {}
        extra = raw.get("albumInfoExtra") or {}
        if not isinstance(info, dict) or not isinstance(extra, dict) or not str(info.get("albumId") or "").strip():
            raise ShelfSyncFailure("missing-identity", "A WeRead shelf album has no albumId")
        item_id = str(info["albumId"])
        add(
            {
                "provider_identity": f"weread:album:{item_id}",
                "provider": "weread",
                "provider_type": "album",
                "provider_item_id": item_id,
                "weread_book_id": None,
                "title": str(info.get("name") or "未命名专辑"),
                "author": str(info.get("authorName") or ""),
                "track_count": info.get("trackCount"),
                "finish_reading": bool_value(info.get("finish")),
                "update_time": info.get("updateTime"),
                "read_update_time": extra.get("lectureReadUpdateTime"),
                "is_top": bool_value(extra.get("isTop")),
                "is_private": bool_value(extra.get("secret")),
                "deep_link": raw.get("deepLink") or info.get("deepLink"),
            }
        )
    if mp:
        if not isinstance(mp, dict):
            raise ShelfSyncFailure("invalid-response", "WeRead mp shelf entry is not an object")
        add(
            {
                "provider_identity": "weread:mp:collection",
                "provider": "weread",
                "provider_type": "mp",
                "provider_item_id": "collection",
                "weread_book_id": None,
                "title": str(mp.get("title") or mp.get("name") or "文章收藏"),
                "author": "",
                "finish_reading": False,
                "is_top": False,
                "is_private": True,
                "deep_link": mp.get("deepLink"),
            }
        )
    return {
        "schema_version": 1,
        "provider": "weread",
        "provider_skill_version": SKILL_VERSION,
        "synced_at": synced_at,
        "scope": {
            "api_calls": ["/shelf/sync"],
            "full_text_fetched": False,
            "notes_fetched": False,
        },
        "counts": {
            "visible_entries": len(books) + len(albums) + (1 if mp else 0),
            "books": len(books),
            "albums": len(albums),
            "mp": 1 if mp else 0,
        },
        "entries": entries,
    }


def read_local_books(workspace: Path) -> list[dict[str, Any]]:
    books_dir = workspace / "books"
    result: list[dict[str, Any]] = []
    if not books_dir.exists():
        return result
    for book_dir in sorted((path for path in books_dir.iterdir() if path.is_dir()), key=lambda path: path.name.casefold()):
        book_yaml = book_dir / "book.yaml"
        try:
            content = book_yaml.read_text(encoding="utf-8")
        except FileNotFoundError:
            result.append(
                {
                    "book_dir": book_dir.relative_to(workspace).as_posix(),
                    "stable_book_id": None,
                    "title": book_dir.name,
                    "authors": [],
                    "text_status": "error",
                    "analysis_status": "failed",
                    "chapter_coverage": None,
                    "weread_book_id": None,
                    "mapping_status": "unlinked",
                    "edition_status": "unknown",
                    "local_error": "book.yaml is missing",
                }
            )
            continue
        except OSError:
            # A live shelf sync must remain useful when one local import is
            # unavailable to the process performing the network call.  Keep
            # that import visible as an error instead of discarding a fresh
            # provider snapshot or attempting to alter its file permissions.
            result.append(
                {
                    "book_dir": book_dir.relative_to(workspace).as_posix(),
                    "stable_book_id": None,
                    "title": book_dir.name,
                    "authors": [],
                    "text_status": "error",
                    "analysis_status": "failed",
                    "chapter_coverage": None,
                    "weread_book_id": None,
                    "mapping_status": "unlinked",
                    "edition_status": "unknown",
                    "local_error": "book.yaml is inaccessible",
                }
            )
            continue
        stable_id = yaml_scalar(content, "stable_book_id")
        text_status = yaml_scalar(content, "status") or "error"
        local_error = None
        if stable_id is None or not STABLE_ID_RE.fullmatch(stable_id):
            local_error = "book.yaml has an invalid stable_book_id"
            text_status = "error"
        if text_status not in TEXT_STATES:
            local_error = "book.yaml has an invalid text status"
            text_status = "error"
        analysis_status = "not-started"
        chapter_coverage: float | None = 0.0
        analysis_path = book_dir / "derived" / "analysis-state.json"
        if analysis_path.exists():
            try:
                analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
                candidate_status = analysis.get("status")
                if candidate_status not in ANALYSIS_STATES:
                    raise ValueError("invalid status")
                analysis_status = candidate_status
                chapter_coverage = (analysis.get("chapters") or {}).get("coverage")
            except (OSError, ValueError, json.JSONDecodeError):
                analysis_status = "failed"
                chapter_coverage = None
                local_error = local_error or "analysis-state.json is invalid"
        result.append(
            {
                "book_dir": book_dir.relative_to(workspace).as_posix(),
                "stable_book_id": stable_id,
                "title": yaml_scalar(content, "title") or book_dir.name,
                "authors": yaml_list(content, "authors"),
                "text_status": text_status,
                "analysis_status": analysis_status,
                "chapter_coverage": chapter_coverage,
                "weread_book_id": yaml_scalar(content, "book_id", indent=2),
                "mapping_status": yaml_scalar(content, "mapping_status", indent=2) or "unlinked",
                "edition_status": yaml_scalar(content, "edition_status", indent=2) or "unknown",
                "local_error": local_error,
            }
        )
    return result


def candidate_local(remote: dict[str, Any], locals_: list[dict[str, Any]], used: set[str]) -> tuple[dict[str, Any] | None, str | None]:
    remote_title = normalized(remote.get("title"))
    matches: list[tuple[dict[str, Any], str]] = []
    for local in locals_:
        stable_id = local.get("stable_book_id")
        if not stable_id or stable_id in used or local.get("text_status") == "error":
            continue
        if not authors_match(str(remote.get("author") or ""), local.get("authors") or []):
            continue
        forms = title_forms(str(local.get("title") or ""))
        full = normalized(local.get("title"))
        if remote_title == full:
            matches.append((local, "exact-title-author"))
        elif remote_title and remote_title in forms:
            matches.append((local, "base-title-author"))
    unique = {item[0]["stable_book_id"]: item for item in matches}
    if len(unique) == 1:
        return next(iter(unique.values()))
    return None, None


def next_action(entry: dict[str, Any]) -> str:
    if entry["provider_type"] in {"album", "mp"}:
        return "仅登记；节点 1 不导入全文或同步笔记"
    if entry.get("local_error"):
        return "修复本地书籍状态后重新生成总览"
    if entry.get("link_status") == "needs-confirmation":
        return "确认微信版与本地源文件是否为同一版本"
    if entry.get("link_status") == "mismatch":
        return "处理版本冲突；不要定位划线或引用本地正文"
    if entry.get("text_status") == "source-missing":
        return "如需全文讨论，请直接提供 EPUB/TXT；默认视为拥有本次本地处理权"
    if entry.get("analysis_status") in {"not-started", "partial", "failed"}:
        return "生成或修复思想层概览，并通过 discussion-ready 审计"
    if entry.get("discussion_available"):
        return "可直接讨论；逐章思想层继续按需生成"
    if entry.get("registration_status") == "local-only":
        return "等待微信书架匹配或手动确认版本"
    return "保留当前状态"


def reconcile(snapshot: dict[str, Any], locals_: list[dict[str, Any]]) -> dict[str, Any]:
    used: set[str] = set()
    entries: list[dict[str, Any]] = []
    direct: dict[str, list[dict[str, Any]]] = {}
    for local in locals_:
        if local.get("weread_book_id"):
            direct.setdefault(str(local["weread_book_id"]), []).append(local)

    for remote in snapshot["entries"]:
        entry = {
            **remote,
            "registration_status": "registered",
            "stable_book_id": None,
            "book_dir": None,
            "link_status": "not-applicable" if remote["provider_type"] != "book" else "unlinked",
            "link_basis": None,
            "mapping_status": "not-applicable" if remote["provider_type"] != "book" else "unlinked",
            "edition_status": "not-applicable" if remote["provider_type"] != "book" else "unknown",
            "text_status": "registered" if remote["provider_type"] != "book" else "source-missing",
            "local_text_status": None,
            "analysis_status": None if remote["provider_type"] != "book" else "not-started",
            "chapter_coverage": None,
            "discussion_available": False,
            "local_error": None,
        }
        local: dict[str, Any] | None = None
        basis: str | None = None
        if remote["provider_type"] == "book":
            direct_matches = direct.get(str(remote["provider_item_id"]), [])
            if len(direct_matches) > 1:
                raise ShelfSyncFailure("duplicate-local-link", "Multiple local books claim the same WeRead bookId")
            if direct_matches:
                local = direct_matches[0]
                basis = "book-yaml-weread-id"
            else:
                local, basis = candidate_local(remote, locals_, used)
        if local is not None:
            stable_id = local.get("stable_book_id")
            if stable_id:
                used.add(stable_id)
            entry.update(
                {
                    "stable_book_id": stable_id,
                    "book_dir": local.get("book_dir"),
                    "link_basis": basis,
                    "mapping_status": local.get("mapping_status"),
                    "edition_status": local.get("edition_status"),
                    "local_text_status": local.get("text_status"),
                    "analysis_status": local.get("analysis_status"),
                    "chapter_coverage": local.get("chapter_coverage"),
                    "local_error": local.get("local_error"),
                }
            )
            confirmed = local.get("mapping_status") == "confirmed" and local.get("edition_status") == "confirmed"
            mismatch = local.get("edition_status") == "mismatch" or local.get("mapping_status") == "mismatch"
            if mismatch:
                entry["link_status"] = "mismatch"
                entry["text_status"] = "mismatch"
            elif confirmed and basis == "book-yaml-weread-id":
                entry["link_status"] = "confirmed"
                entry["text_status"] = local.get("text_status")
            else:
                entry["link_status"] = "needs-confirmation"
                entry["mapping_status"] = "candidate"
                entry["edition_status"] = "needs-confirmation"
                entry["text_status"] = "needs-confirmation"
            entry["discussion_available"] = bool(
                entry["link_status"] == "confirmed"
                and entry["local_text_status"] == "text-ready"
                and entry["analysis_status"] == "discussion-ready"
            )
        entry["next_action"] = next_action(entry)
        entries.append(entry)

    for local in locals_:
        stable_id = local.get("stable_book_id")
        if stable_id and stable_id in used:
            continue
        entry = {
            "provider_identity": None,
            "provider": None,
            "provider_type": "local-book",
            "provider_item_id": None,
            "weread_book_id": local.get("weread_book_id"),
            "shelf_order": None,
            "title": local.get("title"),
            "author": "、".join(local.get("authors") or []),
            "registration_status": "local-only",
            "stable_book_id": stable_id,
            "book_dir": local.get("book_dir"),
            "link_status": "unlinked",
            "link_basis": None,
            "mapping_status": local.get("mapping_status"),
            "edition_status": local.get("edition_status"),
            "text_status": local.get("text_status"),
            "local_text_status": local.get("text_status"),
            "analysis_status": local.get("analysis_status"),
            "chapter_coverage": local.get("chapter_coverage"),
            "discussion_available": False,
            "local_error": local.get("local_error"),
        }
        entry["next_action"] = next_action(entry)
        entries.append(entry)

    status_counts = Counter(str(entry["text_status"]) for entry in entries)
    analysis_counts = Counter(str(entry["analysis_status"]) for entry in entries if entry.get("analysis_status") is not None)
    mapping_counts = Counter(str(entry["link_status"]) for entry in entries)
    all_local_ids = {entry["stable_book_id"] for entry in entries if entry.get("stable_book_id")}
    linked_local_ids = {
        entry["stable_book_id"]
        for entry in entries
        if entry.get("provider_identity") and entry.get("stable_book_id")
    }
    local_full_ids = {
        entry["stable_book_id"]
        for entry in entries
        if entry.get("stable_book_id") and entry.get("local_text_status") == "text-ready"
    }
    analysis_ready_ids = {
        entry["stable_book_id"]
        for entry in entries
        if entry.get("stable_book_id") and entry.get("analysis_status") == "discussion-ready"
    }
    return {
        "schema_version": 1,
        "generated_at": utc_now(),
        "source": {
            "provider": "weread",
            "shelf_synced_at": snapshot["synced_at"],
            "shelf_snapshot_sha256": sha256_json(snapshot),
            "provider_skill_version": snapshot["provider_skill_version"],
        },
        "summary": {
            **snapshot["counts"],
            "combined_entries": len(entries),
            "local_books_total": len(all_local_ids),
            "linked_local_books": len(linked_local_ids),
            "local_full_text_books": len(local_full_ids),
            "analysis_ready_books": len(analysis_ready_ids),
            "discussion_available_books": sum(1 for entry in entries if entry.get("discussion_available")),
            "text_status_distribution": dict(sorted(status_counts.items())),
            "analysis_status_distribution": dict(sorted(analysis_counts.items())),
            "link_status_distribution": dict(sorted(mapping_counts.items())),
        },
        "entries": entries,
    }


def markdown_cell(value: Any) -> str:
    if value is None or value == "":
        return "—"
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def render_coverage(overview: dict[str, Any]) -> str:
    summary = overview["summary"]
    source = overview["source"]
    lines = [
        "# 阅读系统总览",
        "",
        f"> 微信书架快照：`{source['shelf_synced_at']}`；WeRead Skill `{source['provider_skill_version']}`。",
        "",
        "## 汇总",
        "",
        f"- 书架可见条目：{summary['visible_entries']}（电子书 {summary['books']}、专辑/有声书 {summary['albums']}、文章收藏 {summary['mp']}）",
        f"- 本地书总数：{summary['local_books_total']}；已与微信书架关联：{summary['linked_local_books']}；具备本地全文：{summary['local_full_text_books']}；思想层就绪：{summary['analysis_ready_books']}；当前可直接讨论：{summary['discussion_available_books']}",
        f"- 文本状态分布：{json.dumps(summary['text_status_distribution'], ensure_ascii=False, sort_keys=True)}",
        f"- 思想层状态分布：{json.dumps(summary['analysis_status_distribution'], ensure_ascii=False, sort_keys=True)}",
        "",
        "## 逐书状态与待办",
        "",
        "| # | 书籍/条目 | 类型 | 总览状态 | 本地全文 | 思想层 | 版本关联 | 下一步 |",
        "|---:|---|---|---|---|---|---|---|",
    ]
    for index, entry in enumerate(overview["entries"], start=1):
        lines.append(
            "| "
            + " | ".join(
                [
                    str(index),
                    markdown_cell(entry.get("title")),
                    markdown_cell(entry.get("provider_type")),
                    markdown_cell(entry.get("text_status")),
                    markdown_cell(entry.get("local_text_status")),
                    markdown_cell(entry.get("analysis_status")),
                    markdown_cell(entry.get("link_status")),
                    markdown_cell(entry.get("next_action")),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## 边界",
            "",
            "本总览只调用 `/shelf/sync`，不抓取全书架全文、划线或想法。`needs-confirmation` 只表示发现了候选本地书，不能据此引用本地正文或定位微信划线。",
            "",
        ]
    )
    return "\n".join(lines)


def write_snapshot_and_overview(workspace: Path, snapshot: dict[str, Any], overview: dict[str, Any]) -> dict[str, str]:
    shelf_path = workspace / "integrations" / "weread" / "shelf.json"
    overview_path = workspace / "catalog" / "overview.json"
    coverage_path = workspace / "catalog" / "coverage.md"
    write_json(shelf_path, snapshot)
    write_json(overview_path, overview)
    atomic_write(coverage_path, render_coverage(overview))
    return {
        "shelf": str(shelf_path),
        "overview": str(overview_path),
        "coverage": str(coverage_path),
    }


def sync_library(
    workspace: Path,
    api_key: str,
    *,
    gateway: Callable[..., dict[str, Any]] = gateway_call,
    now: str | None = None,
) -> dict[str, Any]:
    workspace = workspace.resolve()
    at = now or utc_now()
    try:
        payload = gateway(api_key, "/shelf/sync")
        snapshot = normalize_shelf(payload, at)
        overview = reconcile(snapshot, read_local_books(workspace))
        paths = write_snapshot_and_overview(workspace, snapshot, overview)
        snapshot_hash = sha256_json(snapshot)
        update_sync_state_success(workspace, at, snapshot_hash, snapshot["counts"]["visible_entries"])
    except ShelfSyncFailure as exc:
        update_sync_state_error(workspace, exc, at)
        raise
    except Exception as exc:
        failure = ShelfSyncFailure("internal-error", "Reading-system overview synchronization failed safely")
        try:
            update_sync_state_error(workspace, failure, at)
        except OSError:
            pass
        raise failure from exc
    return {
        "result": "synced",
        "synced_at": at,
        "provider_skill_version": SKILL_VERSION,
        "api_calls": ["/shelf/sync"],
        "visible_entries": snapshot["counts"]["visible_entries"],
        "combined_entries": overview["summary"]["combined_entries"],
        "linked_local_books": overview["summary"]["linked_local_books"],
        "local_full_text_books": overview["summary"]["local_full_text_books"],
        "analysis_ready_books": overview["summary"]["analysis_ready_books"],
        "discussion_available_books": overview["summary"]["discussion_available_books"],
        "paths": paths,
    }


def rebuild_overview(workspace: Path) -> dict[str, Any]:
    workspace = workspace.resolve()
    shelf_path = workspace / "integrations" / "weread" / "shelf.json"
    if not shelf_path.exists():
        raise ShelfSyncFailure("missing-snapshot", "No valid WeRead shelf snapshot exists")
    try:
        snapshot = json.loads(shelf_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ShelfSyncFailure("invalid-snapshot", "The saved WeRead shelf snapshot is invalid") from exc
    overview = reconcile(snapshot, read_local_books(workspace))
    paths = write_snapshot_and_overview(workspace, snapshot, overview)
    return {"result": "rebuilt", "shelf_synced_at": snapshot.get("synced_at"), "summary": overview["summary"], "paths": paths}


def audit_overview(workspace: Path) -> dict[str, Any]:
    workspace = workspace.resolve()
    shelf_path = workspace / "integrations" / "weread" / "shelf.json"
    overview_path = workspace / "catalog" / "overview.json"
    coverage_path = workspace / "catalog" / "coverage.md"
    try:
        shelf = json.loads(shelf_path.read_text(encoding="utf-8"))
        overview = json.loads(overview_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ShelfSyncFailure("missing-artifact", "Shelf overview artifacts are missing or invalid") from exc
    identities = [entry.get("provider_identity") for entry in shelf.get("entries") or []]
    non_null_overview_ids = [entry.get("provider_identity") for entry in overview.get("entries") or [] if entry.get("provider_identity")]
    locals_by_id = {book.get("stable_book_id") for book in read_local_books(workspace) if book.get("stable_book_id")}
    linked_ids = {entry.get("stable_book_id") for entry in overview.get("entries") or [] if entry.get("stable_book_id")}
    counts = shelf.get("counts") or {}
    recalculated_status = dict(sorted(Counter(str(entry.get("text_status")) for entry in overview.get("entries") or []).items()))
    checks = {
        "schema": shelf.get("schema_version") == 1 and overview.get("schema_version") == 1,
        "provider_scope": shelf.get("scope") == {
            "api_calls": ["/shelf/sync"],
            "full_text_fetched": False,
            "notes_fetched": False,
        },
        "visible_count": counts.get("visible_entries") == counts.get("books", 0) + counts.get("albums", 0) + counts.get("mp", 0),
        "shelf_identity_uniqueness": bool(all(identities)) and len(identities) == len(set(identities)),
        "overview_identity_uniqueness": len(non_null_overview_ids) == len(set(non_null_overview_ids)),
        "snapshot_hash": (overview.get("source") or {}).get("shelf_snapshot_sha256") == sha256_json(shelf),
        "local_links": linked_ids.issubset(locals_by_id),
        "status_distribution": ((overview.get("summary") or {}).get("text_status_distribution") or {}) == recalculated_status,
        "coverage_markdown": coverage_path.exists() and "逐书状态与待办" in coverage_path.read_text(encoding="utf-8"),
    }
    failed = [name for name, passed in checks.items() if not passed]
    return {"result": "passed" if not failed else "failed", "checks": checks, "failed_checks": failed}


def main(argv: list[str] | None = None) -> int:
    configure_console()
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("sync", "overview", "audit"):
        command = subparsers.add_parser(name)
        command.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args(argv or sys.argv[1:])
    try:
        if args.command == "sync":
            try:
                api_key = resolve_weread_api_key(args.workspace)
            except CredentialFailure as exc:
                print(json.dumps({"result": "blocked", "reason": str(exc)}, ensure_ascii=False))
                return 3
            result = sync_library(args.workspace, api_key)
        elif args.command == "overview":
            result = rebuild_overview(args.workspace)
        else:
            result = audit_overview(args.workspace)
            if result["result"] != "passed":
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except ShelfSyncFailure as exc:
        print(json.dumps({"result": "error", "code": exc.code, "error": exc.safe_message}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
