#!/usr/bin/env python3
"""Inspect or import one plain-text book into an MT-readgrowth workspace."""

from __future__ import annotations

import argparse
import codecs
import json
import math
import os
import re
import shutil
import sys
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Iterator

sys.dont_write_bytecode = True

from import_epub import (
    ImportFailure,
    append_catalog_event,
    atomic_write,
    clean_display_text,
    configure_console,
    find_duplicate,
    initial_analysis_state,
    json_line,
    normalize_search,
    render_book_yaml,
    safe_component,
    sha256_bytes,
    sha256_file,
    utc_now,
)


PARSER_NAME = "mt-readgrowth-stdlib-txt"
PARSER_VERSION = "0.3.0"
MAX_TXT_BYTES = 1024 * 1024 * 1024
ENCODING_SAMPLE_BYTES = 512 * 1024
DEFAULT_MAX_CHAPTER_CHARS = 80_000
DEFAULT_MAX_CHAPTER_PARAGRAPHS = 800
MAX_PARAGRAPH_CHARS = 8_000

_NUMERALS = "0-9０-９零〇一二三四五六七八九十百千万两壹贰叁肆伍陆柒捌玖拾佰仟"
_HEADING_PATTERNS = (
    re.compile(rf"^第\s*[{_NUMERALS}]+\s*[章节回卷部篇集幕](?:\s*[：:、.．\-—]?\s*.{{0,60}})?$", re.I),
    re.compile(rf"^[卷部篇集]\s*[{_NUMERALS}]+(?:\s*[：:、.．\-—]?\s*.{{0,60}})?$", re.I),
    re.compile(r"^(序章|序言|前言|楔子|引子|引言|后记|尾声|终章|结语|跋|附录(?:\s*[一二三四五六七八九十0-9]+)?)(?:\s*[：:、.．\-—]?\s*.{0,50})?$", re.I),
    re.compile(r"^(chapter|part|book)\s+[0-9ivxlcdm]+(?:\s*[：:._\-—]?\s*.{0,60})?$", re.I),
)


def validate_txt(path: Path) -> None:
    if not path.is_file():
        raise ImportFailure(f"TXT not found: {path}")
    if path.suffix.lower() != ".txt":
        raise ImportFailure("V0.2 TXT importer accepts .txt files only")
    size = path.stat().st_size
    if size == 0:
        raise ImportFailure("TXT file is empty")
    if size > MAX_TXT_BYTES:
        raise ImportFailure(f"TXT exceeds {MAX_TXT_BYTES} bytes")


def _validate_full_decode(path: Path, encoding: str) -> bool:
    try:
        with path.open("r", encoding=encoding, errors="strict", newline=None) as handle:
            while handle.read(1024 * 1024):
                pass
        return True
    except (LookupError, UnicodeDecodeError):
        return False


def _decoded_score(value: str) -> float:
    if not value:
        return -1.0
    controls = sum(ord(char) < 32 and char not in "\n\r\t" for char in value)
    if controls / len(value) > 0.01:
        return -1.0
    visible = [char for char in value if not char.isspace()]
    if not visible:
        return -1.0
    cjk = sum("\u3400" <= char <= "\u9fff" for char in visible)
    common = sum(char in "，。！？；：、“”‘’（）《》章节第的了是在和" for char in visible)
    mojibake = sum(char in "锟斤拷鈥銆鐨勫湪" for char in visible)
    return (cjk / len(visible)) * 3.0 + (common / len(visible)) - (mojibake / len(visible)) * 2.0


def detect_txt_encoding(path: Path, explicit: str | None = None) -> dict[str, Any]:
    sample = path.read_bytes()[:ENCODING_SAMPLE_BYTES]
    if explicit:
        try:
            encoding = codecs.lookup(explicit).name
        except LookupError as exc:
            raise ImportFailure(f"Unknown text encoding: {explicit}") from exc
        if not _validate_full_decode(path, encoding):
            raise ImportFailure(f"TXT cannot be decoded strictly as {encoding}")
        return {"status": "detected", "encoding": encoding, "confidence": "explicit", "candidates": [encoding]}

    for bom, encoding in (
        (codecs.BOM_UTF8, "utf-8-sig"),
        (codecs.BOM_UTF16_LE, "utf-16"),
        (codecs.BOM_UTF16_BE, "utf-16"),
    ):
        if sample.startswith(bom):
            if not _validate_full_decode(path, encoding):
                raise ImportFailure(f"TXT BOM declares {encoding}, but strict decoding failed")
            return {"status": "detected", "encoding": encoding, "confidence": "bom", "candidates": [encoding]}

    if b"\x00" in sample:
        raise ImportFailure("TXT contains NUL bytes without a supported BOM; encoding is unsafe to guess")

    if _validate_full_decode(path, "utf-8"):
        return {"status": "detected", "encoding": "utf-8", "confidence": "high", "candidates": ["utf-8"]}

    scored: list[tuple[float, str]] = []
    for encoding in ("gb18030", "big5"):
        if not _validate_full_decode(path, encoding):
            continue
        try:
            text = sample.decode(encoding, errors="strict")
        except UnicodeDecodeError:
            continue
        score = _decoded_score(text)
        if score >= 0:
            scored.append((score, encoding))
    if not scored:
        raise ImportFailure("TXT encoding is unsupported or the file is not plain text")
    scored.sort(reverse=True)
    candidates = [encoding for _, encoding in scored]
    if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.08:
        return {
            "status": "ambiguous",
            "encoding": None,
            "confidence": "low",
            "candidates": candidates,
        }
    return {
        "status": "detected",
        "encoding": scored[0][1],
        "confidence": "medium",
        "candidates": candidates,
    }


def is_chapter_heading(value: str) -> bool:
    candidate = unicodedata.normalize("NFKC", value).strip()
    if not candidate or len(candidate) > 100 or "\t" in candidate:
        return False
    if candidate.endswith(("。", "！", "？", "；", ".", "!", "?", ";")):
        return False
    return any(pattern.fullmatch(candidate) for pattern in _HEADING_PATTERNS)


def split_long_paragraph(value: str, limit: int = MAX_PARAGRAPH_CHARS) -> list[str]:
    if len(value) <= limit:
        return [value]
    parts: list[str] = []
    remaining = value
    punctuation = "。！？；.!?;\n"
    while len(remaining) > limit:
        floor = max(1, int(limit * 0.6))
        cut = max((remaining.rfind(mark, floor, limit + 1) + 1 for mark in punctuation), default=0)
        if cut < floor:
            cut = limit
        part = remaining[:cut].strip()
        if part:
            parts.append(part)
        remaining = remaining[cut:].strip()
    if remaining:
        parts.append(remaining)
    return parts


def iter_display_lines(path: Path, encoding: str) -> Iterator[tuple[int, str]]:
    with path.open("r", encoding=encoding, errors="strict", newline=None) as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            value = clean_display_text(raw_line)
            if value:
                yield line_number, value


def scan_txt(path: Path, encoding: str) -> dict[str, Any]:
    line_count = 0
    paragraph_count = 0
    character_count = 0
    heading_count = 0
    headings: list[dict[str, Any]] = []
    for line_number, value in iter_display_lines(path, encoding):
        line_count = line_number
        pieces = split_long_paragraph(value)
        paragraph_count += len(pieces)
        character_count += sum(len(piece) for piece in pieces)
        if is_chapter_heading(value):
            heading_count += 1
            if len(headings) < 20:
                headings.append({"line": line_number, "title": value})
    if paragraph_count == 0:
        raise ImportFailure("TXT contains no readable text")
    return {
        "line_count": line_count,
        "paragraph_count": paragraph_count,
        "character_count": character_count,
        "heading_count": heading_count,
        "heading_preview": headings,
    }


def inspect_txt(
    source: Path,
    *,
    encoding: str | None = None,
    max_chapter_chars: int = DEFAULT_MAX_CHAPTER_CHARS,
    max_chapter_paragraphs: int = DEFAULT_MAX_CHAPTER_PARAGRAPHS,
) -> dict[str, Any]:
    if max_chapter_chars < 1 or max_chapter_paragraphs < 1:
        raise ImportFailure("TXT segmentation limits must be positive")
    source = source.resolve()
    validate_txt(source)
    detection = detect_txt_encoding(source, encoding)
    if detection["status"] == "ambiguous":
        return {
            "result": "needs-confirmation",
            "reason": "ambiguous-encoding",
            "source": str(source),
            "source_sha256": sha256_file(source),
            "encoding": detection,
            "next_action": "rerun with --encoding <candidate>",
        }
    scan = scan_txt(source, detection["encoding"])
    no_headings = scan["heading_count"] == 0
    proposed_segments = max(
        1,
        math.ceil(scan["character_count"] / max_chapter_chars),
        math.ceil(scan["paragraph_count"] / max_chapter_paragraphs),
    )
    return {
        "result": "needs-confirmation" if no_headings else "ready",
        "reason": "no-chapter-headings" if no_headings else None,
        "source": str(source),
        "source_sha256": sha256_file(source),
        "encoding": detection,
        **scan,
        "chapter_strategy": "fixed-segments-pending-confirmation" if no_headings else "detected-headings",
        "proposed_minimum_segments": proposed_segments,
        "next_action": "rerun import with --confirm-no-headings" if no_headings else "run import",
    }


def import_txt(
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
    if max_chapter_chars < 1 or max_chapter_paragraphs < 1:
        raise ImportFailure("TXT segmentation limits must be positive")
    source = source.resolve()
    workspace = workspace.resolve()
    validate_txt(source)
    source_hash = sha256_file(source)
    duplicate = find_duplicate(workspace, source_hash)
    if duplicate:
        return {"result": "duplicate", "source_sha256": source_hash, "book_dir": str(duplicate)}

    inspection = inspect_txt(
        source,
        encoding=encoding,
        max_chapter_chars=max_chapter_chars,
        max_chapter_paragraphs=max_chapter_paragraphs,
    )
    if inspection["reason"] == "ambiguous-encoding":
        return inspection
    if inspection["reason"] == "no-chapter-headings" and not confirm_no_headings:
        return inspection

    detected_encoding = inspection["encoding"]["encoding"]
    chapter_strategy = "confirmed-fixed-segments" if inspection["heading_count"] == 0 else "detected-headings"
    book_title = clean_display_text(title or source.stem) or "Untitled"
    imported_at = utc_now()
    final_dir = workspace / "books" / f"{safe_component(book_title)}__{source_hash[:12]}"
    if final_dir.exists():
        raise ImportFailure(f"Target book directory already exists: {final_dir}")
    workspace.mkdir(parents=True, exist_ok=True)
    staging_root = workspace / "cache"
    staging_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="import-txt-", dir=staging_root))

    chapters: list[dict[str, Any]] = []
    paragraph_count = 0
    current_title = "正文" if inspection["heading_count"] == 0 else "前置内容"
    current_paragraphs: list[tuple[int, str]] = []
    current_chars = 0
    part_index = 1

    try:
        for relative in ("source", "parsed/chapters", "search", "derived/chapters"):
            (staging / relative).mkdir(parents=True, exist_ok=True)
        paragraph_path = staging / "parsed" / "paragraphs.jsonl"
        search_path = staging / "search" / "normalized.jsonl"

        with paragraph_path.open("w", encoding="utf-8", newline="\n") as paragraph_handle, search_path.open(
            "w", encoding="utf-8", newline="\n"
        ) as search_handle:

            def emit_chapter() -> None:
                nonlocal current_paragraphs, current_chars, paragraph_count, part_index
                if not current_paragraphs:
                    return
                chapter_number = len(chapters) + 1
                chapter_id = f"ch-{chapter_number:03d}"
                chapter_title = current_title if part_index == 1 else f"{current_title}（续 {part_index}）"
                start_line = current_paragraphs[0][0]
                end_line = current_paragraphs[-1][0]
                source_ref = f"original.txt#L{start_line}-L{end_line}:segment-{chapter_number}"
                source_ref_hash = sha256_bytes(source_ref.encode("utf-8"))
                texts = [text for _, text in current_paragraphs]
                content_hash = sha256_bytes("\n\n".join(texts).encode("utf-8"))
                chapter = {
                    "chapter_id": chapter_id,
                    "source_href": source_ref,
                    "source_href_sha256": source_ref_hash,
                    "spine_index": chapter_number - 1,
                    "title": chapter_title,
                    "paragraph_count": len(texts),
                    "content_sha256": content_hash,
                    "source_line_start": start_line,
                    "source_line_end": end_line,
                }
                chapters.append(chapter)
                md_parts = [f"# {chapter_title}", ""]
                for paragraph_index, text in enumerate(texts, start=1):
                    locator = f"{source_hash}#{chapter_id}:p-{paragraph_index:04d}"
                    text_hash = sha256_bytes(text.encode("utf-8"))
                    record = {
                        "schema_version": 1,
                        "stable_book_id": source_hash,
                        "locator": locator,
                        "chapter_id": chapter_id,
                        "chapter_title": chapter_title,
                        "spine_index": chapter_number - 1,
                        "paragraph_index": paragraph_index,
                        "text": text,
                        "text_sha256": text_hash,
                        "source_href_sha256": source_ref_hash,
                    }
                    paragraph_handle.write(json_line(record) + "\n")
                    search_handle.write(json_line({
                        "schema_version": 1,
                        "stable_book_id": source_hash,
                        "locator": locator,
                        "normalized_text": normalize_search(text),
                        "text_sha256": text_hash,
                        "source_href_sha256": source_ref_hash,
                    }) + "\n")
                    md_parts.extend([f"<!-- locator: {locator} -->", text, ""])
                atomic_write(staging / "parsed" / "chapters" / f"{chapter_id}.md", "\n".join(md_parts).rstrip() + "\n")
                paragraph_count += len(texts)
                current_paragraphs = []
                current_chars = 0
                part_index += 1

            for line_number, value in iter_display_lines(source, detected_encoding):
                if inspection["heading_count"] and is_chapter_heading(value):
                    emit_chapter()
                    current_title = value
                    current_paragraphs = []
                    current_chars = 0
                    part_index = 1
                for piece in split_long_paragraph(value):
                    would_overflow = current_paragraphs and (
                        current_chars + len(piece) > max_chapter_chars
                        or len(current_paragraphs) >= max_chapter_paragraphs
                    )
                    if would_overflow:
                        emit_chapter()
                    current_paragraphs.append((line_number, piece))
                    current_chars += len(piece)
            emit_chapter()

        if not chapters or paragraph_count == 0:
            raise ImportFailure("TXT parsing produced no readable chapters")
        shutil.copy2(source, staging / "source" / "original.txt")
        toc = {"schema_version": 1, "stable_book_id": source_hash, "chapters": chapters}
        atomic_write(staging / "parsed" / "toc.json", json.dumps(toc, ensure_ascii=False, indent=2) + "\n")
        book_record = {
            "schema_version": 2,
            "stable_book_id": source_hash,
            "title": book_title,
            "authors": authors or [],
            "language": language,
            "identifiers": [],
            "status": "text-ready",
            "quality_warning": None,
            "source": {
                "file_name": source.name,
                "relative_path": "source/original.txt",
                "media_type": "text/plain",
                "size_bytes": source.stat().st_size,
                "sha256": source_hash,
                "imported_at": imported_at,
            },
            "parser": {
                "name": PARSER_NAME,
                "version": PARSER_VERSION,
                "parsed_at": imported_at,
                "source_format": "txt",
                "encoding": detected_encoding,
                "encoding_confidence": inspection["encoding"]["confidence"],
                "chapter_strategy": chapter_strategy,
                "streaming": True,
                "max_chapter_chars": max_chapter_chars,
                "max_chapter_paragraphs": max_chapter_paragraphs,
            },
        }
        atomic_write(staging / "book.yaml", render_book_yaml(book_record))
        atomic_write(
            staging / "derived" / "analysis-state.json",
            json.dumps(initial_analysis_state(source_hash, PARSER_VERSION, len(chapters), imported_at), ensure_ascii=False, indent=2) + "\n",
        )
        checkpoints = [chapters[0], chapters[len(chapters) // 2], chapters[-1]]
        if any(not chapter["content_sha256"] for chapter in checkpoints):
            raise ImportFailure("First/middle/final TXT content verification failed")
        final_dir.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, final_dir)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    event = {
        "event": "book-imported",
        "at": imported_at,
        "stable_book_id": source_hash,
        "book_dir": final_dir.relative_to(workspace).as_posix(),
        "source_sha256": source_hash,
        "source_format": "txt",
        "status": "text-ready",
        "analysis_status": "not-started",
        "parser_version": PARSER_VERSION,
        "encoding": detected_encoding,
        "chapter_strategy": chapter_strategy,
    }
    append_catalog_event(workspace, event)
    checkpoint_ids = list(dict.fromkeys(chapter["chapter_id"] for chapter in checkpoints))
    return {
        "result": "imported",
        "status": "text-ready",
        "analysis_status": "not-started",
        "quality_warning": None,
        "stable_book_id": source_hash,
        "book_dir": str(final_dir),
        "title": book_title,
        "authors": authors or [],
        "source_format": "txt",
        "encoding": detected_encoding,
        "encoding_confidence": inspection["encoding"]["confidence"],
        "chapter_strategy": chapter_strategy,
        "streaming": True,
        "chapter_count": len(chapters),
        "paragraph_count": paragraph_count,
        "checkpoints": checkpoint_ids,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("inspect", "import"))
    parser.add_argument("txt", type=Path, help="Path to a user-provided plain-text book")
    parser.add_argument("--workspace", type=Path, help="MT-readgrowth workspace root; required for import")
    parser.add_argument("--title")
    parser.add_argument("--author", action="append", default=[])
    parser.add_argument("--language")
    parser.add_argument("--encoding", help="Explicit encoding after reviewing an ambiguous detection")
    parser.add_argument("--confirm-no-headings", action="store_true")
    parser.add_argument("--max-chapter-chars", type=int, default=DEFAULT_MAX_CHAPTER_CHARS)
    parser.add_argument("--max-chapter-paragraphs", type=int, default=DEFAULT_MAX_CHAPTER_PARAGRAPHS)
    args = parser.parse_args(argv)
    if args.command == "import" and args.workspace is None:
        parser.error("--workspace is required for import")
    return args


def main(argv: list[str] | None = None) -> int:
    configure_console()
    args = parse_args(argv or sys.argv[1:])
    try:
        if args.command == "inspect":
            result = inspect_txt(
                args.txt,
                encoding=args.encoding,
                max_chapter_chars=args.max_chapter_chars,
                max_chapter_paragraphs=args.max_chapter_paragraphs,
            )
        else:
            result = import_txt(
                args.txt,
                args.workspace,
                title=args.title,
                authors=args.author,
                language=args.language,
                encoding=args.encoding,
                confirm_no_headings=args.confirm_no_headings,
                max_chapter_chars=args.max_chapter_chars,
                max_chapter_paragraphs=args.max_chapter_paragraphs,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 3 if result.get("result") == "needs-confirmation" else 0
    except (ImportFailure, OSError, UnicodeError, ValueError) as exc:
        print(json.dumps({"result": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
