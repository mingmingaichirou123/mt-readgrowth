#!/usr/bin/env python3
"""Build a five-layer evidence package around one located paragraph."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

from personal_context import assemble_personal_context
from record_session import load_discussion_context


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                records.append(json.loads(line))
    return records


def derived_text(book_dir: Path, relative: str) -> str | None:
    path = book_dir / relative
    return path.read_text(encoding="utf-8") if path.exists() else None


def analysis_state(book_dir: Path) -> dict[str, Any]:
    path = book_dir / "derived" / "analysis-state.json"
    if not path.exists():
        return {"status": "not-started", "chapters": {"coverage": 0.0, "completed": []}}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {"status": "failed", "chapters": {"coverage": 0.0, "completed": []}}


def context_for_locator(
    book_dir: Path,
    workspace: Path,
    locator: str,
    before: int,
    after: int,
    *,
    session_dir: Path | None = None,
) -> dict[str, Any]:
    paragraphs = load_jsonl(book_dir / "parsed" / "paragraphs.jsonl")
    focus_index = next((index for index, item in enumerate(paragraphs) if item["locator"] == locator), None)
    if focus_index is None:
        return {"evidence_mode": "restricted", "reason": "focus locator was not found", "focus_locator": locator}
    focus = paragraphs[focus_index]
    same_chapter = [item for item in paragraphs if item["chapter_id"] == focus["chapter_id"]]
    chapter_position = next(index for index, item in enumerate(same_chapter) if item["locator"] == locator)
    surrounding = same_chapter[max(0, chapter_position - before):chapter_position] + same_chapter[chapter_position + 1:chapter_position + 1 + after]
    toc = json.loads((book_dir / "parsed" / "toc.json").read_text(encoding="utf-8"))
    chapter = next(item for item in toc["chapters"] if item["chapter_id"] == focus["chapter_id"])
    if session_dir is None:
        personal_context = assemble_personal_context(workspace, route="discussion")
        personal_context_status = "review-required-before-use"
    else:
        frozen = load_discussion_context(workspace, session_dir)
        relative_session = Path("sessions") / session_dir.resolve().relative_to((workspace / "sessions").resolve())
        personal_context = {
            "schema_version": frozen["schema_version"],
            "route": "discussion",
            "session_id": frozen["session_id"],
            "context_path": (relative_session / "context.json").as_posix(),
            "context_sha256": frozen["context_sha256"],
            "source_bundle_sha256": frozen["source_bundle_sha256"],
            "source_snapshots": frozen["source_snapshots"],
            "selected_sources": frozen["selected_sources"],
            "evidence_policy": frozen["evidence_policy"],
        }
        personal_context_status = "frozen-confirmed"
    chapter_analysis = derived_text(book_dir, f"derived/chapters/{focus['chapter_id']}.md")
    book_map = derived_text(book_dir, "derived/book-map.md")
    author_lens = derived_text(book_dir, "derived/author-lens.md")
    thought_layer = analysis_state(book_dir)
    discussion_ready = thought_layer.get("status") == "discussion-ready" and bool(book_map) and bool(author_lens)
    return {
        "evidence_mode": "full" if discussion_ready else "restricted",
        "restriction_reason": None if discussion_ready else "book text is available but the validated thought layer is not discussion-ready",
        "source_file_sha256": focus["stable_book_id"],
        "focus": focus,
        "surrounding": surrounding,
        "chapter": {
            **chapter,
            "book_position": f"{chapter['spine_index'] + 1}/{len(toc['chapters'])}",
            "analysis": chapter_analysis,
            "analysis_status": "available" if chapter_analysis else "not-generated",
        },
        "book_map": book_map,
        "book_map_status": "available" if book_map else "not-generated",
        "author_lens": author_lens,
        "author_lens_status": "available" if author_lens else "not-generated",
        "thought_layer": {
            "status": thought_layer.get("status", "not-started"),
            "chapter_coverage": thought_layer.get("chapters", {}).get("coverage", 0.0),
            "focus_chapter_generated": focus["chapter_id"] in thought_layer.get("chapters", {}).get("completed", []),
        },
        "personal_context": personal_context,
        "personal_context_status": personal_context_status,
        "prior_user_context": [],
        "prior_user_context_status": "disabled-until-explicit-selection",
        "rules": {
            "quote_only_display_text": True,
            "required_labels": ["作者明确表达", "作者立场推断", "AI延伸应用"],
            "candidate_beliefs_require_confirmation": True,
            "personal_context_requires_frozen_session_context": True,
            "personal_context_cannot_change_evidence_classification": True,
            "profile_context_role": ["explanation-angle", "examples", "application-mapping"],
        },
    }


def main(argv: list[str] | None = None) -> int:
    configure_console()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--book-dir", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--session-dir", type=Path)
    parser.add_argument("--locator", required=True)
    parser.add_argument("--before", type=int, choices=range(2, 6), default=3)
    parser.add_argument("--after", type=int, choices=range(2, 6), default=3)
    args = parser.parse_args(argv or sys.argv[1:])
    try:
        result = context_for_locator(
            args.book_dir.resolve(),
            args.workspace.resolve(),
            args.locator,
            args.before,
            args.after,
            session_dir=args.session_dir,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("evidence_mode") == "full" else 4
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"evidence_mode": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
