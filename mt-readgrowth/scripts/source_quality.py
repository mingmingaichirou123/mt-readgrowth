#!/usr/bin/env python3
"""Register and audit deterministic, deletion-only EPUB watermark cleaning."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True


SCHEMA_VERSION = 1
QUALITY_WARNING = "embedded-watermark"
REGISTERED_RULES = (
    {
        "rule_id": "embedded-watermark-foufoushu-share-v1",
        "match_type": "literal",
        "literal": "[书分-享薇foufoushu]",
        "operation": "delete-exact-match-only",
        "registration_basis": "deterministic-project-signature",
    },
)
QUALITY_NOTICE = "本书源文件含已登记分发水印；思想层生成时已按精确删除规则忽略，原始展示文本未修改。"


class QualityFailure(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def clean_registered_watermark(text: str) -> dict[str, Any] | None:
    matched_rules = [rule for rule in REGISTERED_RULES if str(rule["literal"]) in text]
    occurrences = sum(text.count(str(rule["literal"])) for rule in matched_rules)
    if not occurrences:
        return None
    cleaned = text
    for rule in matched_rules:
        cleaned = cleaned.replace(str(rule["literal"]), "")
    reasons = []
    if "<" in cleaned or ">" in cleaned:
        reasons.append("residual-angle-bracket-fragment")
    if not cleaned.strip():
        reasons.append("empty-after-cleaning")
    elif not any(character.isalnum() for character in cleaned):
        reasons.append("separator-only-after-cleaning")
    return {
        "occurrences": occurrences,
        "cleaned_text": cleaned,
        "cleaned_text_sha256": sha256_text(cleaned),
        "clean_status": "skipped" if reasons else "usable",
        "skip_reasons": reasons,
    }


def registered_rules() -> list[dict[str, Any]]:
    return [dict(rule) for rule in REGISTERED_RULES]


def notice_for_rules(rules: list[dict[str, Any]]) -> str:
    return QUALITY_NOTICE


def build_registry(paragraphs: list[dict[str, Any]], stable_book_id: str) -> dict[str, Any] | None:
    affected = []
    for record in paragraphs:
        cleaned = clean_registered_watermark(str(record.get("text", "")))
        if cleaned is None:
            continue
        affected.append({
            "locator": record["locator"],
            "original_text_sha256": record["text_sha256"],
            **cleaned,
        })
    if not affected:
        return None
    usable = sum(item["clean_status"] == "usable" for item in affected)
    source_text = "\n".join(str(record.get("text", "")) for record in paragraphs)
    rules = [rule for rule in registered_rules() if str(rule["literal"]) in source_text]
    return {
        "schema_version": SCHEMA_VERSION,
        "stable_book_id": stable_book_id,
        "quality_warning": QUALITY_WARNING,
        "notice": notice_for_rules(rules),
        "rules": rules,
        "rules_sha256": sha256_text(canonical_json(rules)),
        "summary": {
            "affected_locators": len(affected),
            "usable_after_cleaning": usable,
            "skipped_after_cleaning": len(affected) - usable,
        },
        "affected": affected,
    }


def validate_registry(
    registry: dict[str, Any], paragraphs: list[dict[str, Any]], stable_book_id: str
) -> list[str]:
    errors: list[str] = []
    expected = build_registry(paragraphs, stable_book_id)
    if expected is None:
        return ["registry exists but source text contains no registered watermark signature"]
    for field in ("schema_version", "stable_book_id", "quality_warning", "notice", "rules", "rules_sha256", "summary", "affected"):
        if registry.get(field) != expected.get(field):
            errors.append(f"source-quality mismatch: {field}")
    return errors


def load_registry(book_dir: Path) -> dict[str, Any] | None:
    path = book_dir / "derived" / "source-quality.json"
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise QualityFailure("source-quality.json must contain an object")
    return value


def require_registry(
    book_dir: Path,
    paragraphs: list[dict[str, Any]],
    stable_book_id: str,
    quality_warning: str | None,
) -> dict[str, Any] | None:
    registry = load_registry(book_dir)
    if quality_warning is None:
        if registry is not None:
            raise QualityFailure("book has no quality_warning but source-quality.json exists")
        return None
    if quality_warning != QUALITY_WARNING:
        raise QualityFailure(f"unsupported quality_warning: {quality_warning}")
    if registry is None:
        raise QualityFailure("quality_warning requires derived/source-quality.json")
    errors = validate_registry(registry, paragraphs, stable_book_id)
    if errors:
        raise QualityFailure("; ".join(errors))
    return registry


def registry_index(registry: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if registry is None:
        return {}
    return {item["locator"]: item for item in registry["affected"]}


def cleaning_rule_id(record: dict[str, Any], registry: dict[str, Any]) -> str:
    matched_rule_ids = [
        str(rule["rule_id"])
        for rule in registry.get("rules", [])
        if str(rule.get("literal", "")) in str(record.get("text", ""))
    ]
    if not matched_rule_ids:
        raise QualityFailure("registered affected paragraph matches no registry rule")
    return "+".join(matched_rule_ids)


def analysis_text(record: dict[str, Any], registry: dict[str, Any] | None) -> dict[str, Any] | None:
    item = registry_index(registry).get(record["locator"])
    if item is None:
        return {
            "text": record["text"],
            "text_sha256": record["text_sha256"],
            "cleaning": "none",
        }
    if item["clean_status"] == "skipped":
        return None
    return {
        "text": item["cleaned_text"],
        "text_sha256": item["cleaned_text_sha256"],
        "cleaning": cleaning_rule_id(record, registry),
    }


def yaml_scalar(content: str, key: str, indent: int = 0) -> str | None:
    match = re.search(rf"(?m)^{' ' * indent}{re.escape(key)}:\s*(.+?)\s*$", content)
    if not match or match.group(1) == "null":
        return None
    raw = match.group(1)
    try:
        return str(json.loads(raw))
    except json.JSONDecodeError:
        return raw.strip("\"'")


def register_existing_book(book_dir: Path) -> dict[str, Any]:
    book_dir = book_dir.resolve()
    book_yaml = book_dir / "book.yaml"
    content = book_yaml.read_text(encoding="utf-8")
    stable_book_id = yaml_scalar(content, "stable_book_id") or ""
    parser_version = yaml_scalar(content, "version", indent=2) or "unknown"
    paragraphs = [json.loads(line) for line in (book_dir / "parsed" / "paragraphs.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    registry = build_registry(paragraphs, stable_book_id)
    if registry is None:
        raise QualityFailure("book contains no registered deterministic watermark signature")
    existing_path = book_dir / "derived" / "source-quality.json"
    if (
        existing_path.exists()
        and yaml_scalar(content, "status") == "text-ready"
        and yaml_scalar(content, "quality_warning") == QUALITY_WARNING
    ):
        existing = json.loads(existing_path.read_text(encoding="utf-8"))
        errors = validate_registry(existing, paragraphs, stable_book_id)
        if not errors:
            state_path = book_dir / "derived" / "analysis-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
            return {
                "result": "unchanged",
                "book_dir": str(book_dir),
                "status": "text-ready",
                "analysis_status": state.get("status"),
                **registry["summary"],
            }
    if (book_dir / "derived" / "book-map.md").exists() or (book_dir / "derived" / "author-lens.md").exists():
        raise QualityFailure("refusing to reset a book with applied overview artifacts")
    if any((book_dir / "derived" / "chapters").glob("ch-*.md")):
        raise QualityFailure("refusing to reset a book with applied chapter artifacts")
    atomic_write(book_dir / "derived" / "source-quality.json", json.dumps(registry, ensure_ascii=False, indent=2) + "\n")
    content, count = re.subn(r"(?m)^status:\s*.*$", 'status: "text-ready"', content, count=1)
    if count != 1:
        raise QualityFailure("book.yaml has no status field")
    if re.search(r"(?m)^quality_warning:", content):
        content = re.sub(r"(?m)^quality_warning:\s*.*$", f'quality_warning: "{QUALITY_WARNING}"', content, count=1)
    else:
        content = content.replace('status: "text-ready"\n', f'status: "text-ready"\nquality_warning: "{QUALITY_WARNING}"\n', 1)
    atomic_write(book_yaml, content)
    toc = json.loads((book_dir / "parsed" / "toc.json").read_text(encoding="utf-8"))
    total = sum(item.get("paragraph_count", 0) > 0 for item in toc.get("chapters", []))
    state = {
        "schema_version": 1,
        "stable_book_id": stable_book_id,
        "workflow_version": "0.2.0",
        "mode": "progressive",
        "status": "not-started",
        "source": {"sha256": stable_book_id, "parser_version": parser_version},
        "overview": {"status": "not-started", "input_sha256": None, "result_sha256": None, "generated_at": None, "generator": None},
        "chapters": {"total": total, "completed": [], "failed": {}, "coverage": 0.0},
        "chapter_results": {},
        "last_error": None,
        "updated_at": utc_now(),
    }
    atomic_write(book_dir / "derived" / "analysis-state.json", json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    workspace = book_dir.parent.parent
    event = {
        "event": "book-quality-warning-registered",
        "at": utc_now(),
        "stable_book_id": stable_book_id,
        "book_dir": book_dir.relative_to(workspace).as_posix(),
        "status": "text-ready",
        "quality_warning": QUALITY_WARNING,
        "analysis_status": "not-started",
        **registry["summary"],
    }
    catalog = workspace / "catalog" / "books.jsonl"
    with catalog.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(canonical_json(event) + "\n")
    return {"result": "registered", "book_dir": str(book_dir), "status": "text-ready", "analysis_status": "not-started", **registry["summary"]}


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("register",))
    parser.add_argument("--book-dir", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        result = register_existing_book(args.book_dir)
    except (OSError, ValueError, json.JSONDecodeError, QualityFailure) as exc:
        print(json.dumps({"result": "error", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
