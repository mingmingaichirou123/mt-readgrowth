#!/usr/bin/env python3
"""Import one EPUB into an MT-readgrowth workspace using only the stdlib."""

from __future__ import annotations

import argparse
import hashlib
import html.parser
import json
import os
import posixpath
import re
import shutil
import sys
import tempfile
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

sys.dont_write_bytecode = True

from source_quality import QUALITY_WARNING, build_registry


PARSER_NAME = "mt-readgrowth-stdlib-epub"
PARSER_VERSION = "0.2.2"
ANALYSIS_WORKFLOW_VERSION = "0.2.0"
MAX_EPUB_BYTES = 200 * 1024 * 1024
MAX_MEMBER_BYTES = 100 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED = 750 * 1024 * 1024
MAX_MEMBERS = 20_000
DOCUMENT_MEDIA_TYPES = {"application/xhtml+xml", "text/html"}
BLOCK_TAGS = {
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote",
    "pre", "figcaption", "dt", "dd",
}
SKIP_TAGS = {"script", "style", "head", "svg"}


class ImportFailure(RuntimeError):
    pass


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def decode_xml(data: bytes) -> str:
    declaration = re.match(br"\s*<\?xml[^>]*encoding=[\"']([^\"']+)", data[:300], re.I)
    candidates: list[str] = []
    if declaration:
        candidates.append(declaration.group(1).decode("ascii", errors="ignore"))
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        candidates.append("utf-16")
    candidates.extend(["utf-8-sig", "utf-8", "gb18030"])
    for encoding in candidates:
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def parse_xml(data: bytes, label: str) -> ET.Element:
    try:
        return ET.fromstring(decode_xml(data).lstrip())
    except ET.ParseError as exc:
        raise ImportFailure(f"Invalid XML in {label}: {exc}") from exc


def validate_epub(path: Path) -> None:
    if not path.is_file():
        raise ImportFailure(f"EPUB not found: {path}")
    if path.suffix.lower() != ".epub":
        raise ImportFailure("V0 accepts EPUB files only")
    if path.stat().st_size > MAX_EPUB_BYTES:
        raise ImportFailure(f"EPUB exceeds {MAX_EPUB_BYTES} bytes")
    if not zipfile.is_zipfile(path):
        raise ImportFailure("File is not a valid ZIP-based EPUB")
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_MEMBERS:
            raise ImportFailure("EPUB contains too many archive members")
        total = 0
        for info in infos:
            pure = PurePosixPath(info.filename.replace("\\", "/"))
            if pure.is_absolute() or ".." in pure.parts:
                raise ImportFailure(f"Unsafe archive member path: {info.filename}")
            if info.file_size > MAX_MEMBER_BYTES:
                raise ImportFailure(f"Archive member is too large: {info.filename}")
            total += info.file_size
            if total > MAX_TOTAL_UNCOMPRESSED:
                raise ImportFailure("EPUB uncompressed size exceeds safety limit")
        names = set(archive.namelist())
        if "META-INF/container.xml" not in names and not any(n.lower().endswith(".opf") for n in names):
            raise ImportFailure("EPUB has neither container.xml nor an OPF package")


def find_opf_path(archive: zipfile.ZipFile) -> str:
    try:
        root = parse_xml(archive.read("META-INF/container.xml"), "META-INF/container.xml")
        for element in root.iter():
            if local_name(element.tag) == "rootfile" and element.get("full-path"):
                candidate = urllib.parse.unquote(element.get("full-path", ""))
                if candidate in archive.namelist():
                    return candidate
    except KeyError:
        pass
    candidates = sorted(name for name in archive.namelist() if name.lower().endswith(".opf"))
    if not candidates:
        raise ImportFailure("No OPF package found")
    return candidates[0]


def text_of_first(root: ET.Element, wanted: str) -> str | None:
    for element in root.iter():
        if local_name(element.tag) == wanted and element.text and element.text.strip():
            return element.text.strip()
    return None


def texts_of(root: ET.Element, wanted: str) -> list[str]:
    values = []
    for element in root.iter():
        if local_name(element.tag) == wanted and element.text and element.text.strip():
            values.append(element.text.strip())
    return values


def canonical_href(opf_path: str, href: str) -> str:
    clean = urllib.parse.unquote(href.split("#", 1)[0])
    return posixpath.normpath(posixpath.join(posixpath.dirname(opf_path), clean))


def parse_package(archive: zipfile.ZipFile, opf_path: str) -> dict[str, Any]:
    root = parse_xml(archive.read(opf_path), opf_path)
    manifest: dict[str, dict[str, str]] = {}
    manifest_order: list[str] = []
    spine_ids: list[str] = []
    for element in root.iter():
        name = local_name(element.tag)
        if name == "item" and element.get("id") and element.get("href"):
            item_id = element.get("id", "")
            manifest[item_id] = {
                "href": canonical_href(opf_path, element.get("href", "")),
                "media_type": element.get("media-type", ""),
                "properties": element.get("properties", ""),
            }
            manifest_order.append(item_id)
        elif name == "itemref" and element.get("idref"):
            spine_ids.append(element.get("idref", ""))

    documents: list[dict[str, str]] = []
    seen: set[str] = set()
    for item_id in spine_ids:
        item = manifest.get(item_id)
        if item and item["href"] not in seen:
            documents.append({"manifest_id": item_id, **item})
            seen.add(item["href"])
    if not documents:
        for item_id in manifest_order:
            item = manifest[item_id]
            if item["media_type"] in DOCUMENT_MEDIA_TYPES and "nav" not in item["properties"].split():
                documents.append({"manifest_id": item_id, **item})

    if not documents:
        raise ImportFailure("OPF contains no readable spine or XHTML documents")
    return {
        "title": text_of_first(root, "title") or "Untitled",
        "authors": texts_of(root, "creator"),
        "language": text_of_first(root, "language"),
        "identifiers": texts_of(root, "identifier"),
        "documents": documents,
    }


class ParagraphExtractor(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.paragraphs: list[tuple[str, str]] = []
        self._current_tag: str | None = None
        self._buffer: list[str] = []
        self._skip_depth = 0
        self._all_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in BLOCK_TAGS:
            self._flush()
            self._current_tag = tag
        elif tag == "br" and self._current_tag:
            self._buffer.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if not self._skip_depth and tag == self._current_tag:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        self._all_text.append(data)
        if self._current_tag:
            self._buffer.append(data)

    def close(self) -> None:
        super().close()
        self._flush()

    def _flush(self) -> None:
        if self._current_tag:
            text = clean_display_text("".join(self._buffer))
            if text:
                self.paragraphs.append((self._current_tag, text))
        self._current_tag = None
        self._buffer = []

    def fallback_text(self) -> list[tuple[str, str]]:
        text = clean_display_text("".join(self._all_text))
        return [("p", text)] if text else []


def clean_display_text(value: str) -> str:
    lines = [re.sub(r"[\t\r\f\v ]+", " ", line).strip() for line in value.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def normalize_search(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"\s+", "", normalized)


def parse_document(raw: bytes) -> tuple[str | None, list[str]]:
    parser = ParagraphExtractor()
    parser.feed(decode_xml(raw))
    parser.close()
    blocks = parser.paragraphs or parser.fallback_text()
    title = next((text for tag, text in blocks if tag in {"h1", "h2", "h3"}), None)
    return title, [text for _, text in blocks]


def safe_component(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return (value or "untitled")[:80].rstrip(" .")


def json_line(record: dict[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=False, separators=(",", ":"))


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def render_book_yaml(record: dict[str, Any]) -> str:
    lines = [
        f"schema_version: {record['schema_version']}",
        f"stable_book_id: {yaml_scalar(record['stable_book_id'])}",
        f"title: {yaml_scalar(record['title'])}",
        "authors:",
    ]
    lines.extend(f"  - {yaml_scalar(author)}" for author in record["authors"])
    if not record["authors"]:
        lines[-1] = "authors: []"
    lines.extend([
        f"language: {yaml_scalar(record['language'])}",
        "identifiers:",
    ])
    lines.extend(f"  - {yaml_scalar(identifier)}" for identifier in record["identifiers"])
    if not record["identifiers"]:
        lines[-1] = "identifiers: []"
    lines.extend([
        f"status: {yaml_scalar(record['status'])}",
        f"quality_warning: {yaml_scalar(record.get('quality_warning'))}",
        "source:",
        f"  file_name: {yaml_scalar(record['source']['file_name'])}",
        f"  relative_path: {yaml_scalar(record['source']['relative_path'])}",
        f"  media_type: {yaml_scalar(record['source']['media_type'])}",
        f"  size_bytes: {record['source']['size_bytes']}",
        f"  sha256: {yaml_scalar(record['source']['sha256'])}",
        f"  imported_at: {yaml_scalar(record['source']['imported_at'])}",
        "parser:",
        f"  name: {yaml_scalar(record['parser']['name'])}",
        f"  version: {yaml_scalar(record['parser']['version'])}",
        f"  parsed_at: {yaml_scalar(record['parser']['parsed_at'])}",
    ])
    for key in (
        "source_format",
        "opf_path",
        "encoding",
        "encoding_confidence",
        "chapter_strategy",
        "streaming",
        "max_chapter_chars",
        "max_chapter_paragraphs",
    ):
        if key in record["parser"]:
            lines.append(f"  {key}: {yaml_scalar(record['parser'][key])}")
    lines.extend([
        "weread:",
        "  book_id: null",
        '  mapping_status: "unlinked"',
        '  edition_status: "unknown"',
        "  confirmed_at: null",
        "  confirmation_basis: null",
    ])
    return "\n".join(lines) + "\n"


def initial_analysis_state(stable_book_id: str, parser_version: str, chapter_total: int, at: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "stable_book_id": stable_book_id,
        "workflow_version": ANALYSIS_WORKFLOW_VERSION,
        "mode": "progressive",
        "status": "not-started",
        "source": {"sha256": stable_book_id, "parser_version": parser_version},
        "overview": {
            "status": "not-started",
            "input_sha256": None,
            "result_sha256": None,
            "generated_at": None,
            "generator": None,
        },
        "chapters": {"total": chapter_total, "completed": [], "failed": {}, "coverage": 0.0},
        "chapter_results": {},
        "last_error": None,
        "updated_at": at,
    }


def find_duplicate(workspace: Path, source_hash: str) -> Path | None:
    books_dir = workspace / "books"
    if not books_dir.exists():
        return None
    pattern = re.compile(rf'^\s+sha256:\s*["\']?{re.escape(source_hash)}["\']?\s*$', re.M)
    for book_yaml in books_dir.glob("*/book.yaml"):
        try:
            if pattern.search(book_yaml.read_text(encoding="utf-8")):
                return book_yaml.parent
        except OSError:
            continue
    return None


def append_catalog_event(workspace: Path, event: dict[str, Any]) -> None:
    catalog = workspace / "catalog" / "books.jsonl"
    catalog.parent.mkdir(parents=True, exist_ok=True)
    with catalog.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json_line(event) + "\n")


def import_epub(source: Path, workspace: Path) -> dict[str, Any]:
    source = source.resolve()
    workspace = workspace.resolve()
    validate_epub(source)
    source_hash = sha256_file(source)
    duplicate = find_duplicate(workspace, source_hash)
    if duplicate:
        return {
            "result": "duplicate",
            "source_sha256": source_hash,
            "book_dir": str(duplicate),
        }

    with zipfile.ZipFile(source) as archive:
        opf_path = find_opf_path(archive)
        package = parse_package(archive, opf_path)
        chapters: list[dict[str, Any]] = []
        paragraph_records: list[dict[str, Any]] = []
        search_records: list[dict[str, Any]] = []
        chapter_markdown: dict[str, str] = {}
        stable_book_id = source_hash

        for spine_index, document in enumerate(package["documents"]):
            href = document["href"]
            try:
                raw = archive.read(href)
            except KeyError as exc:
                raise ImportFailure(f"Spine document is missing: {href}") from exc
            extracted_title, paragraphs = parse_document(raw)
            chapter_id = f"ch-{spine_index + 1:03d}"
            chapter_title = extracted_title or PurePosixPath(href).stem
            href_hash = sha256_bytes(href.encode("utf-8"))
            chapter_content_hash = sha256_bytes("\n\n".join(paragraphs).encode("utf-8"))
            chapter = {
                "chapter_id": chapter_id,
                "source_href": href,
                "source_href_sha256": href_hash,
                "spine_index": spine_index,
                "title": chapter_title,
                "paragraph_count": len(paragraphs),
                "content_sha256": chapter_content_hash,
            }
            chapters.append(chapter)
            md_parts = [f"# {chapter_title}", ""]
            for paragraph_index, text in enumerate(paragraphs, start=1):
                locator = f"{stable_book_id}#{chapter_id}:p-{paragraph_index:04d}"
                text_hash = sha256_bytes(text.encode("utf-8"))
                record = {
                    "schema_version": 1,
                    "stable_book_id": stable_book_id,
                    "locator": locator,
                    "chapter_id": chapter_id,
                    "chapter_title": chapter_title,
                    "spine_index": spine_index,
                    "paragraph_index": paragraph_index,
                    "text": text,
                    "text_sha256": text_hash,
                    "source_href_sha256": href_hash,
                }
                paragraph_records.append(record)
                search_records.append({
                    "schema_version": 1,
                    "stable_book_id": stable_book_id,
                    "locator": locator,
                    "normalized_text": normalize_search(text),
                    "text_sha256": text_hash,
                    "source_href_sha256": href_hash,
                })
                md_parts.extend([f"<!-- locator: {locator} -->", text, ""])
            chapter_markdown[chapter_id] = "\n".join(md_parts).rstrip() + "\n"

    populated = [chapter for chapter in chapters if chapter["paragraph_count"] > 0]
    if len(populated) < 3:
        raise ImportFailure("Fewer than three readable spine documents were extracted")
    checkpoints = [populated[0], populated[len(populated) // 2], populated[-1]]
    if any(not chapter["content_sha256"] for chapter in checkpoints):
        raise ImportFailure("First/middle/final content verification failed")
    quality_registry = build_registry(paragraph_records, source_hash)
    quality_warning = QUALITY_WARNING if quality_registry else None

    imported_at = utc_now()
    book_folder_name = f"{safe_component(package['title'])}__{source_hash[:12]}"
    final_dir = workspace / "books" / book_folder_name
    if final_dir.exists():
        raise ImportFailure(f"Target book directory already exists: {final_dir}")
    workspace.mkdir(parents=True, exist_ok=True)
    staging_root = workspace / "cache"
    staging_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="import-", dir=staging_root))
    try:
        for relative in ("source", "parsed/chapters", "search", "derived/chapters"):
            (staging / relative).mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, staging / "source" / "original.epub")
        toc = {"schema_version": 1, "stable_book_id": source_hash, "chapters": chapters}
        atomic_write(staging / "parsed" / "toc.json", json.dumps(toc, ensure_ascii=False, indent=2) + "\n")
        atomic_write(staging / "parsed" / "paragraphs.jsonl", "\n".join(map(json_line, paragraph_records)) + "\n")
        atomic_write(staging / "search" / "normalized.jsonl", "\n".join(map(json_line, search_records)) + "\n")
        for chapter_id, markdown in chapter_markdown.items():
            atomic_write(staging / "parsed" / "chapters" / f"{chapter_id}.md", markdown)
        book_record = {
            "schema_version": 2,
            "stable_book_id": source_hash,
            "title": package["title"],
            "authors": package["authors"],
            "language": package["language"],
            "identifiers": package["identifiers"],
            "status": "text-ready",
            "quality_warning": quality_warning,
            "source": {
                "file_name": source.name,
                "relative_path": "source/original.epub",
                "media_type": "application/epub+zip",
                "size_bytes": source.stat().st_size,
                "sha256": source_hash,
                "imported_at": imported_at,
            },
            "parser": {
                "name": PARSER_NAME,
                "version": PARSER_VERSION,
                "parsed_at": imported_at,
                "opf_path": opf_path,
            },
        }
        atomic_write(staging / "book.yaml", render_book_yaml(book_record))
        if quality_registry:
            atomic_write(staging / "derived" / "source-quality.json", json.dumps(quality_registry, ensure_ascii=False, indent=2) + "\n")
        analysis_state = initial_analysis_state(source_hash, PARSER_VERSION, len(populated), imported_at)
        atomic_write(staging / "derived" / "analysis-state.json", json.dumps(analysis_state, ensure_ascii=False, indent=2) + "\n")
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
        "status": "text-ready",
        "analysis_status": "not-started",
        "parser_version": PARSER_VERSION,
        "quality_warning": quality_warning,
    }
    append_catalog_event(workspace, event)
    return {
        "result": "imported",
        "status": "text-ready",
        "analysis_status": "not-started",
        "quality_warning": quality_warning,
        "stable_book_id": source_hash,
        "book_dir": str(final_dir),
        "title": package["title"],
        "authors": package["authors"],
        "chapter_count": len(chapters),
        "paragraph_count": len(paragraph_records),
        "quality_summary": quality_registry["summary"] if quality_registry else None,
        "checkpoints": [chapter["chapter_id"] for chapter in checkpoints],
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("epub", type=Path, help="Path to a user-provided, DRM-free EPUB")
    parser.add_argument("--workspace", type=Path, required=True, help="MT-readgrowth workspace root")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    configure_console()
    args = parse_args(argv or sys.argv[1:])
    try:
        result = import_epub(args.epub, args.workspace)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except ImportFailure as exc:
        print(json.dumps({"result": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
