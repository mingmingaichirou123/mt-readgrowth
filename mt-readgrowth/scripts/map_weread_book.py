#!/usr/bin/env python3
"""Create or confirm a WeRead-to-local-book mapping without using secrets."""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

sys.dont_write_bytecode = True


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE)


def yaml_value(text: str, key: str) -> str | None:
    match = re.search(rf"^{re.escape(key)}:\s*[\"']?(.*?)[\"']?\s*$", text, re.M)
    return match.group(1) if match else None


def map_book(
    book_dir: Path,
    book_id: str,
    weread_title: str,
    weread_author: str,
    confirm: bool,
    confirmation_basis: str | None = None,
) -> dict:
    path = book_dir.resolve() / "book.yaml"
    content = path.read_text(encoding="utf-8")
    local_title = yaml_value(content, "title") or ""
    author_match = re.search(r"^authors:\s*\n\s+-\s*[\"']?(.*?)[\"']?\s*$", content, re.M)
    local_author = author_match.group(1) if author_match else ""
    title_match = normalize(local_title) == normalize(weread_title)
    author_match_value = normalize(local_author) in normalize(weread_author) or normalize(weread_author) in normalize(local_author)
    if not title_match:
        mapping_status = "mismatch"
        edition_status = "mismatch"
    elif confirm:
        mapping_status = "confirmed"
        edition_status = "confirmed" if author_match_value else "needs-confirmation"
    else:
        mapping_status = "candidate"
        edition_status = "needs-confirmation"
    confirmed_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z") if confirm else None

    replacements = {
        r"(?m)^  book_id:.*$": f"  book_id: {json.dumps(book_id, ensure_ascii=False)}",
        r"(?m)^  mapping_status:.*$": f"  mapping_status: {json.dumps(mapping_status)}",
        r"(?m)^  edition_status:.*$": f"  edition_status: {json.dumps(edition_status)}",
        r"(?m)^  confirmed_at:.*$": f"  confirmed_at: {json.dumps(confirmed_at) if confirmed_at else 'null'}",
    }
    if re.search(r"(?m)^  confirmation_basis:.*$", content):
        replacements[r"(?m)^  confirmation_basis:.*$"] = (
            f"  confirmation_basis: {json.dumps(confirmation_basis) if confirm and confirmation_basis else 'null'}"
        )
    else:
        replacements[r"(?m)^(  confirmed_at:.*)$"] = (
            r"\1\n  confirmation_basis: "
            + (json.dumps(confirmation_basis) if confirm and confirmation_basis else "null")
        )
    updated = content
    for pattern, replacement in replacements.items():
        updated, count = re.subn(pattern, replacement, updated, count=1)
        if count != 1:
            raise ValueError(f"book.yaml is missing expected field for pattern: {pattern}")
    temporary = path.with_suffix(".yaml.tmp")
    temporary.write_text(updated, encoding="utf-8", newline="\n")
    temporary.replace(path)
    return {
        "mapping_status": mapping_status,
        "edition_status": edition_status,
        "book_id": book_id,
        "local_title": local_title,
        "weread_title": weread_title,
        "title_match": title_match,
        "local_author": local_author,
        "weread_author": weread_author,
        "author_match": author_match_value,
        "requires_user_confirmation": mapping_status != "confirmed" or edition_status != "confirmed",
    }


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--book-dir", type=Path, required=True)
    parser.add_argument("--book-id", required=True)
    parser.add_argument("--weread-title", required=True)
    parser.add_argument("--weread-author", required=True)
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--confirmation-basis")
    args = parser.parse_args(argv or sys.argv[1:])
    try:
        print(json.dumps(map_book(
            args.book_dir,
            args.book_id,
            args.weread_title,
            args.weread_author,
            args.confirm,
            args.confirmation_basis,
        ), ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
