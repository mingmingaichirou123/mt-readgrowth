#!/usr/bin/env python3
"""Locate synchronized WeRead highlights and review abstracts in local EPUB text."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

from match_highlight import match_highlight, range_start


MATCHER_VERSION = "0.2.0"


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def choose_anchors(event: dict[str, Any], reviews: list[dict[str, Any]]) -> list[dict[str, Any]]:
    event_start = range_start(event.get("range"))
    same_chapter = [
        review for review in reviews
        if review.get("chapter_uid") == event.get("chapter_uid")
        and review.get("abstract")
        and range_start(review.get("range")) is not None
    ]
    if event_start is None:
        return same_chapter
    return sorted(same_chapter, key=lambda review: abs(event_start - (range_start(review.get("range")) or 0)))


def locate_event(book_dir: Path, event: dict[str, Any], reviews: list[dict[str, Any]]) -> dict[str, Any]:
    if event.get("event") == "weread-review" and event.get("abstract"):
        return match_highlight(
            book_dir,
            event["abstract"],
            chapter_title=event.get("chapter_name"),
        )
    if event.get("event") == "weread-highlight" and event.get("mark_text"):
        anchors = choose_anchors(event, reviews)
        for anchor in anchors:
            result = match_highlight(
                book_dir,
                event["mark_text"],
                chapter_title=event.get("chapter_title"),
                associated_text=anchor.get("abstract"),
                highlight_range=event.get("range"),
                associated_range=anchor.get("range"),
            )
            if result.get("status") in {"exact", "high-confidence"}:
                result["anchor_event_id"] = anchor.get("event_id")
                return result
        return match_highlight(
            book_dir,
            event["mark_text"],
            chapter_title=event.get("chapter_title"),
        )
    return {"status": "not-found", "reason": "event has no locatable source text", "candidates": []}


def locate_notes(book_dir: Path, notes_path: Path, output_path: Path) -> dict[str, Any]:
    events = load_jsonl(notes_path)
    reviews = [event for event in events if event.get("event") == "weread-review"]
    known = set()
    if output_path.exists():
        for record in load_jsonl(output_path):
            known.add(record.get("event_id"))
    records = []
    counts: dict[str, int] = {}
    for event in events:
        match_id = f"match:{event.get('event_id')}:{MATCHER_VERSION}"
        if match_id in known:
            continue
        result = locate_event(book_dir, event, reviews)
        status = result.get("status", "error")
        counts[status] = counts.get(status, 0) + 1
        records.append({
            "schema_version": 1,
            "event_id": match_id,
            "event": "local-text-match",
            "matched_at": utc_now(),
            "matcher_version": MATCHER_VERSION,
            "source_event_id": event.get("event_id"),
            "source_event_type": event.get("event"),
            "stable_book_id": event.get("stable_book_id"),
            "weread_book_id": event.get("weread_book_id"),
            "result": result,
        })
    if records:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("a", encoding="utf-8", newline="\n") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    return {"result": "completed", "new_match_events": len(records), "status_counts": counts, "output": str(output_path)}


def main(argv: list[str] | None = None) -> int:
    configure_console()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--book-dir", type=Path, required=True)
    parser.add_argument("--notes", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv or sys.argv[1:])
    try:
        print(json.dumps(locate_notes(args.book_dir.resolve(), args.notes.resolve(), args.output.resolve()), ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"result": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
