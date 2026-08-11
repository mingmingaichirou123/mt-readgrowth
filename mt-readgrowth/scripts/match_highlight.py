#!/usr/bin/env python3
"""Locate a WeRead highlight in an imported MT-readgrowth book."""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True


MIN_AUTO_MATCH_CHARS = 6
MAX_CANDIDATES = 10


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def normalize(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value).casefold())


def strip_punctuation(value: str) -> str:
    return "".join(ch for ch in value if not unicodedata.category(ch).startswith(("P", "S")))


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records = []
    with path.open("r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if line.strip():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Invalid JSONL at {path}:{number}: {exc}") from exc
    return records


def edition_status(book_dir: Path) -> str:
    book_yaml = book_dir / "book.yaml"
    if not book_yaml.exists():
        return "unknown"
    match = re.search(r"^\s+edition_status:\s*[\"']?([^\"'\s]+)", book_yaml.read_text(encoding="utf-8"), re.M)
    return match.group(1) if match else "unknown"


def candidate(record: dict[str, Any], match_type: str, score: float = 1.0) -> dict[str, Any]:
    text = record["text"]
    excerpt = text if len(text) <= 180 else text[:177] + "..."
    return {
        "match_type": match_type,
        "score": round(score, 4),
        "locator": record["locator"],
        "chapter_id": record["chapter_id"],
        "chapter_title": record.get("chapter_title"),
        "paragraph_index": record["paragraph_index"],
        "text_sha256": record["text_sha256"],
        "excerpt": excerpt,
    }


def partial_similarity(query: str, text: str) -> float:
    if not query or not text:
        return 0.0
    if len(text) <= len(query):
        return difflib.SequenceMatcher(None, query, text).ratio()
    step = max(1, len(query) // 6)
    window = min(len(text), max(len(query) + max(4, len(query) // 5), len(query)))
    scores = []
    for start in range(0, max(1, len(text) - window + 1), step):
        scores.append(difflib.SequenceMatcher(None, query, text[start:start + window]).ratio())
    scores.append(difflib.SequenceMatcher(None, query, text[-window:]).ratio())
    return max(scores)


def range_start(value: str | None) -> int | None:
    if not value:
        return None
    match = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", value)
    return int(match.group(1)) if match else None


def narrow_by_relative_range(
    candidates: list[dict[str, Any]],
    normalized_records: list[tuple[dict[str, Any], str]],
    associated_text: str | None,
    highlight_range: str | None,
    associated_range: str | None,
) -> list[dict[str, Any]] | None:
    """Use same-chapter WeRead range order without pretending offsets are equal."""
    highlight_start = range_start(highlight_range)
    anchor_start = range_start(associated_range)
    associated = normalize(associated_text or "")
    if highlight_start is None or anchor_start is None or not associated:
        return None
    anchors = [record for record, text in normalized_records if associated in text]
    if len(anchors) != 1:
        return None
    anchor = anchors[0]
    candidate_records = {record["locator"]: record for record, _ in normalized_records}
    if highlight_start > anchor_start:
        narrowed = [item for item in candidates if candidate_records[item["locator"]]["chapter_id"] == anchor["chapter_id"] and item["paragraph_index"] > anchor["paragraph_index"]]
    elif highlight_start < anchor_start:
        narrowed = [item for item in candidates if candidate_records[item["locator"]]["chapter_id"] == anchor["chapter_id"] and item["paragraph_index"] < anchor["paragraph_index"]]
    else:
        narrowed = [item for item in candidates if item["locator"] == anchor["locator"]]
    if not narrowed:
        return None
    return [{**item, "match_type": "normalized-substring+relative-range-order", "score": 0.99} for item in narrowed]


def match_highlight(
    book_dir: Path,
    highlight: str,
    chapter_title: str | None = None,
    associated_text: str | None = None,
    highlight_range: str | None = None,
    associated_range: str | None = None,
) -> dict[str, Any]:
    book_dir = book_dir.resolve()
    if edition_status(book_dir) == "mismatch":
        return {"status": "edition-mismatch", "reason": "book.yaml marks the WeRead edition as mismatched", "candidates": []}
    query = normalize(highlight)
    if not query:
        return {"status": "not-found", "reason": "highlight is empty after normalization", "candidates": []}
    records = load_jsonl(book_dir / "parsed" / "paragraphs.jsonl")
    normalized_records = [(record, normalize(record["text"])) for record in records]

    exact = [candidate(record, "normalized-substring") for record, text in normalized_records if query in text]
    if chapter_title and exact:
        chapter_query = normalize(chapter_title)
        narrowed = [item for item in exact if chapter_query in normalize(item.get("chapter_title") or "")]
        if narrowed:
            exact = narrowed
    if associated_text and len(exact) > 1:
        associated = normalize(associated_text)
        associated_matches = {
            record["locator"] for record, text in normalized_records if associated and associated in text
        }
        narrowed = [item for item in exact if item["locator"] in associated_matches]
        if narrowed:
            exact = narrowed

    range_narrowed = narrow_by_relative_range(
        exact, normalized_records, associated_text, highlight_range, associated_range
    )
    if range_narrowed:
        exact = range_narrowed
        if len(exact) == 1:
            return {
                "status": "high-confidence",
                "reason": "same-chapter WeRead ranges place the short highlight on one side of a uniquely located anchor",
                "candidates": exact,
                "range_evidence": {
                    "highlight_range": highlight_range,
                    "associated_range": associated_range,
                    "offsets_treated_as_relative_order_only": True,
                },
            }

    if len(exact) == 1 and len(query) >= MIN_AUTO_MATCH_CHARS:
        return {"status": "exact", "reason": "one normalized source paragraph contains the highlight", "candidates": exact}
    if exact:
        reason = "highlight is too short for automatic confirmation" if len(query) < MIN_AUTO_MATCH_CHARS else "multiple source locations contain the highlight"
        return {"status": "ambiguous", "reason": reason, "candidates": exact[:MAX_CANDIDATES], "candidate_count": len(exact)}

    punctuation_free = strip_punctuation(query)
    loose = []
    if len(punctuation_free) >= MIN_AUTO_MATCH_CHARS:
        for record, text in normalized_records:
            if punctuation_free in strip_punctuation(text):
                loose.append(candidate(record, "punctuation-insensitive", 0.97))
    if chapter_title and loose:
        chapter_query = normalize(chapter_title)
        narrowed = [item for item in loose if chapter_query in normalize(item.get("chapter_title") or "")]
        if narrowed:
            loose = narrowed
    if len(loose) == 1:
        return {"status": "high-confidence", "reason": "one punctuation-insensitive location matches", "candidates": loose}
    if loose:
        return {"status": "ambiguous", "reason": "multiple punctuation-insensitive locations match", "candidates": loose[:MAX_CANDIDATES], "candidate_count": len(loose)}

    fuzzy = []
    if len(query) >= 12:
        for record, text in normalized_records:
            score = partial_similarity(query, text)
            if score >= 0.88:
                fuzzy.append(candidate(record, "fuzzy", score))
        fuzzy.sort(key=lambda item: item["score"], reverse=True)
    if fuzzy:
        top = fuzzy[0]
        runner_up = fuzzy[1]["score"] if len(fuzzy) > 1 else 0.0
        if top["score"] >= 0.94 and top["score"] - runner_up >= 0.05:
            return {"status": "high-confidence", "reason": "one fuzzy candidate is clearly stronger", "candidates": [top]}
        return {"status": "ambiguous", "reason": "fuzzy candidates require confirmation", "candidates": fuzzy[:MAX_CANDIDATES], "candidate_count": len(fuzzy)}
    return {"status": "not-found", "reason": "no acceptable local-text match", "candidates": []}


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--book-dir", type=Path, required=True)
    parser.add_argument("--text", required=True)
    parser.add_argument("--chapter-title")
    parser.add_argument("--associated-text")
    parser.add_argument("--highlight-range")
    parser.add_argument("--associated-range")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    configure_console()
    args = parse_args(argv or sys.argv[1:])
    try:
        result = match_highlight(
            args.book_dir,
            args.text,
            args.chapter_title,
            args.associated_text,
            args.highlight_range,
            args.associated_range,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
