#!/usr/bin/env python3
"""Build manual evidence-tiered weekly reports and manage belief decisions."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

sys.dont_write_bytecode = True

import belief_store


SCHEMA_VERSION = 1
FLOW_VERSION = "1.0.0"
BELIEF_EVENT_PATH = belief_store.BELIEF_EVENT_PATH
BELIEF_DIR = belief_store.BELIEF_DIR
WEEKLY_DIR = Path("knowledge/weekly")
ACTIVE_BELIEF_STATUSES = belief_store.ACTIVE_BELIEF_STATUSES
TERMINAL_STATUSES = belief_store.TERMINAL_STATUSES


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)


def atomic_json(path: Path, value: Any) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    values: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number} is not a JSON object")
        values.append(value)
    return values


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(canonical_json(value) + "\n")


def relative_path(path: Path, workspace: Path) -> str:
    return path.resolve().relative_to(workspace.resolve()).as_posix()


def parse_rfc3339(value: str) -> datetime:
    normalized = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp has no timezone: {value}")
    return parsed


def format_rfc3339(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_fixed_timezone(value: str) -> timezone:
    match = re.fullmatch(r"([+-])(\d{2}):(\d{2})", value)
    if not match:
        raise ValueError("timezone must use +HH:MM or -HH:MM")
    sign = 1 if match.group(1) == "+" else -1
    hours, minutes = int(match.group(2)), int(match.group(3))
    if hours > 23 or minutes > 59:
        raise ValueError("timezone offset is out of range")
    return timezone(sign * timedelta(hours=hours, minutes=minutes))


def week_bounds(week: str, tz: timezone) -> tuple[datetime, datetime, date, date]:
    match = re.fullmatch(r"(\d{4})-W(\d{2})", week)
    if not match:
        raise ValueError("week must use ISO form YYYY-Www")
    monday = date.fromisocalendar(int(match.group(1)), int(match.group(2)), 1)
    next_monday = monday + timedelta(days=7)
    start = datetime.combine(monday, time.min, tzinfo=tz)
    end = datetime.combine(next_monday, time.min, tzinfo=tz)
    return start, end, monday, next_monday - timedelta(days=1)


def within(value: datetime, start: datetime, end: datetime) -> bool:
    return start <= value.astimezone(start.tzinfo) < end


def yaml_scalar(content: str, key: str) -> str | None:
    match = re.search(rf"(?m)^{re.escape(key)}:\s*(.+?)\s*$", content)
    if not match:
        return None
    raw = match.group(1)
    if raw == "null":
        return None
    try:
        value = json.loads(raw)
        return str(value)
    except json.JSONDecodeError:
        return raw.strip("'\"")


def yaml_list(content: str, key: str) -> list[str]:
    lines = content.splitlines()
    start_index: int | None = None
    inline_empty = False
    for index, line in enumerate(lines):
        match = re.fullmatch(rf"{re.escape(key)}:\s*(\[\])?\s*", line)
        if match:
            start_index = index + 1
            inline_empty = bool(match.group(1))
            break
    if start_index is None or inline_empty:
        return []
    values: list[str] = []
    for line in lines[start_index:]:
        match = re.fullmatch(r"  - (.+?)\s*", line)
        if not match:
            break
        raw = match.group(1)
        try:
            values.append(str(json.loads(raw)))
        except json.JSONDecodeError:
            values.append(raw.strip("'\""))
    return values


def markdown_sections(content: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    matches = list(re.finditer(r"(?m)^##\s+(.+?)\s*$", content))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        body = content[match.end():end].strip()
        items = [line[2:].strip() for line in body.splitlines() if line.startswith("- ")]
        sections[match.group(1).strip()] = items if items else ([body] if body else [])
    return sections


def summary_values(session_dir: Path) -> dict[str, Any]:
    structured_path = session_dir / "summary-input.json"
    if structured_path.exists():
        value = json.loads(structured_path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            return {**value, "profile_candidates": value.get("profile_candidates", [])}
    summary_path = session_dir / "summary.md"
    if not summary_path.exists():
        return {}
    sections = markdown_sections(summary_path.read_text(encoding="utf-8"))
    mapping = {
        "用户原始问题": "question",
        "使用的书籍与原文位置": "sources",
        "作者明确表达": "author_explicit",
        "作者立场推断": "author_inference",
        "AI延伸应用": "ai_application",
        "用户明确确认": "user_confirmed",
        "分歧与适用条件": "disagreements",
        "候选认知（未经用户确认）": "candidate_insights",
        "候选用户资料（未经用户确认）": "profile_candidates",
        "未解决问题": "unresolved",
        "建议行动或后续阅读": "actions",
    }
    result: dict[str, Any] = {}
    for title, key in mapping.items():
        values = sections.get(title, [])
        if key == "question":
            result[key] = values[0] if values else ""
        else:
            result[key] = values
    return result


def normalized_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    items: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            text = item.get("text") or item.get("claim") or canonical_json(item)
            locator = item.get("locator") or item.get("source")
            items.append({"text": str(text), "locator": str(locator) if locator else None})
        else:
            items.append({"text": str(item), "locator": None})
    return items


def book_titles(workspace: Path) -> dict[str, str]:
    titles: dict[str, str] = {}
    books_dir = workspace / "books"
    if not books_dir.exists():
        return titles
    for metadata in books_dir.glob("*/book.yaml"):
        content = metadata.read_text(encoding="utf-8")
        stable_id = yaml_scalar(content, "stable_book_id")
        title_value = yaml_scalar(content, "title")
        if stable_id and title_value:
            titles[stable_id] = title_value
    return titles


def source_manifest(paths: Iterable[Path], workspace: Path) -> list[dict[str, str]]:
    unique = sorted({path.resolve() for path in paths if path.exists()}, key=lambda item: str(item).lower())
    return [{"path": relative_path(path, workspace), "sha256": file_sha256(path)} for path in unique]


def candidate_id(session_id: str, index: int, content: str) -> str:
    return belief_store.candidate_id(session_id, index, content)


def belief_id_for(candidate: str) -> str:
    return belief_store.belief_id_for(candidate)


def candidate_event(candidate: dict[str, Any], at: str) -> dict[str, Any]:
    return belief_store.candidate_event(candidate, at)


def event_states(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return belief_store.event_states(events)


def register_candidates(workspace: Path, candidates: list[dict[str, Any]], at: str) -> tuple[int, list[dict[str, Any]]]:
    return belief_store.register_candidates(workspace, candidates, at)


def session_records(workspace: Path, start: datetime | None = None, end: datetime | None = None) -> tuple[list[dict[str, Any]], list[Path]]:
    records: list[dict[str, Any]] = []
    sources: list[Path] = []
    sessions_root = workspace / "sessions"
    if not sessions_root.exists():
        return records, sources
    for session_yaml in sorted(sessions_root.glob("*/*/session.yaml")):
        content = session_yaml.read_text(encoding="utf-8")
        started_raw = yaml_scalar(content, "started_at")
        if not started_raw:
            continue
        started = parse_rfc3339(started_raw)
        if start is not None and end is not None and not within(started, start, end):
            continue
        session_dir = session_yaml.parent
        transcript_path = session_dir / "transcript.jsonl"
        transcript = load_jsonl(transcript_path)
        event_types = [str(event.get("event_type", "")) for event in transcript]
        skipped = "discussion-skipped-by-user" in event_types
        ended_at = yaml_scalar(content, "ended_at")
        summary_status = yaml_scalar(content, "summary_status")
        # A started session is an append-only work in progress, not yet a saved
        # discussion. Only a finalized summary may enter weekly evidence or
        # materialize belief candidates. This also keeps abandoned sessions with
        # provisional metadata out of downstream audits without rewriting them.
        if not ended_at or summary_status not in {"generated", "confirmed"}:
            continue
        summary = summary_values(session_dir)
        summary_path = session_dir / "summary.md"
        source_paths = [session_yaml]
        for path in (transcript_path, summary_path, session_dir / "summary-input.json"):
            if path.exists():
                source_paths.append(path)
        sources.extend(source_paths)
        records.append({
            "session_id": yaml_scalar(content, "session_id") or session_dir.name,
            "started_at": format_rfc3339(started),
            "ended_at": ended_at,
            "stable_book_ids": yaml_list(content, "stable_book_ids"),
            "summary_status": summary_status,
            "evidence_mode": yaml_scalar(content, "evidence_mode"),
            "discussion_status": "skipped-by-user" if skipped else "completed",
            "event_types": event_types,
            "summary": summary,
            "source_paths": [relative_path(path, workspace) for path in source_paths],
        })
    return records, sources


def all_candidates(workspace: Path) -> list[dict[str, Any]]:
    sessions, _sources = session_records(workspace)
    candidates: list[dict[str, Any]] = []
    for session in sessions:
        values = normalized_items(session["summary"].get("candidate_insights", []))
        summary_path = next((path for path in session["source_paths"] if path.endswith("summary.md")), None)
        for index, item in enumerate(values, start=1):
            content = item["text"].strip()
            if not content or content == "无":
                continue
            cid = candidate_id(session["session_id"], index, content)
            candidates.append({
                "candidate_id": cid,
                "belief_id": belief_id_for(cid),
                "content": content,
                "source": {
                    "session_id": session["session_id"],
                    "summary_path": summary_path,
                    "candidate_index": index,
                    "locator": item.get("locator"),
                },
            })
    return candidates


def catalog_facts(workspace: Path, start: datetime, end: datetime) -> tuple[list[dict[str, Any]], list[Path]]:
    path = workspace / "catalog" / "books.jsonl"
    facts = []
    for event in load_jsonl(path):
        at = event.get("at")
        if at and within(parse_rfc3339(str(at)), start, end):
            facts.append(event)
    return facts, ([path] if path.exists() else [])


def note_facts(workspace: Path, start: datetime, end: datetime) -> tuple[list[dict[str, Any]], list[Path]]:
    facts: list[dict[str, Any]] = []
    paths: list[Path] = []
    note_dir = workspace / "integrations" / "weread" / "notes"
    if not note_dir.exists():
        return facts, paths
    for path in sorted(note_dir.glob("*.jsonl")):
        paths.append(path)
        for event in load_jsonl(path):
            timestamp: datetime | None = None
            create_time = event.get("create_time")
            if isinstance(create_time, (int, float)) and create_time > 0:
                timestamp = datetime.fromtimestamp(create_time, tz=timezone.utc)
            elif event.get("synced_at"):
                timestamp = parse_rfc3339(str(event["synced_at"]))
            if timestamp and within(timestamp, start, end):
                facts.append({**event, "occurred_at": format_rfc3339(timestamp)})
    return facts, paths


def flatten_summary(session: dict[str, Any], key: str, titles: dict[str, str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    evidence_locators = [
        item["locator"] for item in normalized_items(session["summary"].get("sources", [])) if item.get("locator")
    ]
    for item in normalized_items(session["summary"].get(key, [])):
        result.append({
            "session_id": session["session_id"],
            "books": [titles.get(book_id, book_id) for book_id in session["stable_book_ids"]],
            "text": item["text"],
            "locator": item.get("locator"),
            "evidence_locators": evidence_locators,
            "summary_path": next((path for path in session["source_paths"] if path.endswith("summary.md")), None),
        })
    return result


def is_next_reading_proposal(text: str) -> bool:
    return bool(
        re.search(r"重读", text)
        or re.search(r"(?:阅读|读)(?:.{0,16})(?:章节|第.{1,8}[章节回部篇]|ch-\d+)", text, re.I)
        or re.search(r"(?:章节|第.{1,8}[章节回部篇]|ch-\d+)(?:.{0,16})(?:阅读|读)", text, re.I)
    )


def build_report(workspace: Path, week: str, tz_text: str, now: str | None = None) -> dict[str, Any]:
    workspace = workspace.resolve()
    tz = parse_fixed_timezone(tz_text)
    start, end, monday, sunday = week_bounds(week, tz)
    generated = parse_rfc3339(now) if now else datetime.now(timezone.utc)
    titles = book_titles(workspace)
    catalog, catalog_paths = catalog_facts(workspace, start, end)
    notes, note_paths = note_facts(workspace, start, end)
    sessions, session_paths = session_records(workspace, start, end)
    week_candidates: list[dict[str, Any]] = []
    for session in sessions:
        for index, item in enumerate(normalized_items(session["summary"].get("candidate_insights", [])), start=1):
            content = item["text"].strip()
            if not content or content == "无":
                continue
            cid = candidate_id(session["session_id"], index, content)
            week_candidates.append({
                "candidate_id": cid,
                "belief_id": belief_id_for(cid),
                "content": content,
                "source": {
                    "session_id": session["session_id"],
                    "summary_path": next((path for path in session["source_paths"] if path.endswith("summary.md")), None),
                    "candidate_index": index,
                    "locator": item.get("locator"),
                },
            })
    appended, events = register_candidates(workspace, week_candidates, format_rfc3339(generated))
    states = event_states(events)
    completed = [session for session in sessions if session["discussion_status"] == "completed"]
    skipped = [session for session in sessions if session["discussion_status"] == "skipped-by-user"]
    content_facts = [item for session in completed for item in flatten_summary(session, "author_explicit", titles)]
    user_confirmed = [item for session in sessions for item in flatten_summary(session, "user_confirmed", titles)]
    ai_inferences = [item for session in completed for key in ("author_inference", "ai_application") for item in flatten_summary(session, key, titles)]
    unresolved = [item for session in sessions for item in flatten_summary(session, "unresolved", titles)]
    actions = [item for session in sessions for item in flatten_summary(session, "actions", titles)]
    next_chapters = [item for item in actions if is_next_reading_proposal(item["text"])]
    imports = [event for event in catalog if event.get("event") == "book-imported"]
    report = {
        "schema_version": SCHEMA_VERSION,
        "flow_version": FLOW_VERSION,
        "report_type": "manual-weekly-reading-review",
        "week": week,
        "timezone": tz_text,
        "period": {
            "local_start": monday.isoformat(),
            "local_end": sunday.isoformat(),
            "utc_start": format_rfc3339(start),
            "utc_end_exclusive": format_rfc3339(end),
        },
        "generated_at": format_rfc3339(generated),
        "behavior_facts": {
            "book_imports": imports,
            "weread_notes": notes,
            "completed_discussions": [{key: value for key, value in session.items() if key not in {"summary", "source_paths", "event_types"}} for session in completed],
            "skipped_discussions": [{key: value for key, value in session.items() if key not in {"summary", "source_paths", "event_types"}} for session in skipped],
        },
        "content_facts": content_facts,
        "user_confirmed": user_confirmed,
        "ai_inferences": ai_inferences,
        "candidate_beliefs": [{**candidate, "status": states[candidate["candidate_id"]]["status"]} for candidate in week_candidates],
        "unresolved": unresolved,
        "next_chapters": next_chapters,
        "evidence_gaps": [],
        "candidate_events_appended": appended,
    }
    if not notes:
        report["evidence_gaps"].append("本周没有可核验的新增微信划线或想法记录。")
    if not sessions:
        report["evidence_gaps"].append("本周没有保存的阅读会话。")
    if not content_facts:
        report["evidence_gaps"].append("本周没有来自已完成讨论的作者明确表达记录。")
    if not week_candidates:
        report["evidence_gaps"].append("本周没有候选认知；不得据此编造成长结论。")
    if not next_chapters:
        report["evidence_gaps"].append("本周没有用户明确选择的下一步章节。")
    report_paths = session_paths + catalog_paths + note_paths
    belief_path = workspace / BELIEF_EVENT_PATH
    if belief_path.exists():
        report_paths.append(belief_path)
    report["source_manifest"] = source_manifest(report_paths, workspace)
    output_dir = workspace / WEEKLY_DIR / week
    json_path = output_dir / "report.json"
    markdown_path = output_dir / "report.md"
    atomic_json(json_path, report)
    atomic_write(markdown_path, render_report(report))
    return {
        "result": "generated",
        "week": week,
        "report_json": str(json_path),
        "report_markdown": str(markdown_path),
        "counts": {
            "book_imports": len(imports),
            "weread_notes": len(notes),
            "completed_discussions": len(completed),
            "skipped_discussions": len(skipped),
            "candidate_beliefs": len(week_candidates),
        },
        "candidate_events_appended": appended,
        "candidate_beliefs": report["candidate_beliefs"],
        "evidence_gaps": report["evidence_gaps"],
    }


def markdown_items(values: list[Any], formatter: Any) -> str:
    if not values:
        return "- 无可核验证据。\n"
    return "\n".join(f"- {formatter(value)}" for value in values) + "\n"


def render_report(report: dict[str, Any]) -> str:
    behavior = report["behavior_facts"]
    sections = [
        f"# {report['week']} 阅读周报\n",
        f"> 手动生成；统计区间：{report['period']['local_start']} 至 {report['period']['local_end']}（UTC{report['timezone']}）。周报是证据汇总，不能代替认知确认。\n",
        "## 阅读行为事实\n",
        f"- 本周导入本地书籍：{len(behavior['book_imports'])} 本。\n",
        f"- 本周新增微信划线/想法：{len(behavior['weread_notes'])} 条。\n",
        f"- 已完成并保存的真实讨论：{len(behavior['completed_discussions'])} 次。\n",
        f"- 用户明确跳过的讨论：{len(behavior['skipped_discussions'])} 次，不计入真实讨论。\n",
        "## 内容事实（作者明确表达）\n",
        markdown_items(
            report["content_facts"],
            lambda item: f"{item['text']}（会话：`{item['session_id']}`；总结：`{item['summary_path']}`"
            + (f"；原文位置：{', '.join(f'`{value}`' for value in item['evidence_locators'])}" if item["evidence_locators"] else "")
            + "）",
        ),
        "## 用户明确确认\n",
        markdown_items(report["user_confirmed"], lambda item: f"{item['text']}（会话：`{item['session_id']}`）"),
        "## AI 推断与延伸（不是作者原话或用户信念）\n",
        markdown_items(report["ai_inferences"], lambda item: f"{item['text']}（会话：`{item['session_id']}`）"),
        "## 候选认知（仍需显式决定）\n",
        markdown_items(report["candidate_beliefs"], lambda item: f"`{item['candidate_id']}` [{item['status']}] {item['content']}（会话：`{item['source']['session_id']}`）"),
        "## 未解决问题\n",
        markdown_items(report["unresolved"], lambda item: f"{item['text']}（会话：`{item['session_id']}`）"),
        "## 下一步章节（建议或空缺，不冒充用户选择）\n",
        markdown_items(report["next_chapters"], lambda item: f"{item['text']}（会话：`{item['session_id']}`）"),
        "## 证据空缺与限制\n",
        markdown_items(report["evidence_gaps"], lambda item: item),
        "## 追溯信息\n",
        f"- 机器报告：`knowledge/weekly/{report['week']}/report.json`\n",
        f"- 生成时间：`{report['generated_at']}`\n",
        f"- 源文件清单：{len(report['source_manifest'])} 个文件，路径和 SHA-256 见机器报告。\n",
    ]
    return "\n".join(sections).rstrip() + "\n"


def list_candidates(workspace: Path, now: str | None = None) -> dict[str, Any]:
    return belief_store.list_candidates(workspace, all_candidates(workspace), now)


def decision_event(state: dict[str, Any], decision: str, user_statement: str, revised_content: str | None, at: str) -> dict[str, Any]:
    return belief_store.decision_event(state, decision, user_statement, revised_content, at)


def belief_markdown(state: dict[str, Any]) -> str:
    return belief_store.belief_markdown(state)


def write_belief_view(workspace: Path, state: dict[str, Any]) -> Path | None:
    return belief_store.write_belief_view(workspace, state)


def decide_belief(
    workspace: Path,
    candidate: str,
    decision: str,
    user_statement: str,
    revised_content: str | None = None,
    at: str | None = None,
) -> dict[str, Any]:
    workspace = workspace.resolve()
    list_candidates(workspace, now=at)
    return belief_store.decide_belief(
        workspace,
        candidate,
        decision,
        user_statement,
        revised_content,
        at,
    )


def audit(workspace: Path) -> dict[str, Any]:
    workspace = workspace.resolve()
    belief_audit = belief_store.audit(workspace)
    checks = dict(belief_audit["checks"])
    known_book_ids = set(book_titles(workspace))
    weekly_reports: list[dict[str, Any]] = []
    weekly_paths = sorted((workspace / WEEKLY_DIR).glob("*/report.json")) if (workspace / WEEKLY_DIR).exists() else []
    try:
        for report_path in weekly_paths:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if not isinstance(report, dict):
                raise ValueError("weekly report is not an object")
            weekly_reports.append(report)
        checks["weekly_report_schema"] = all(
            report.get("schema_version") == SCHEMA_VERSION
            and report.get("report_type") == "manual-weekly-reading-review"
            for report in weekly_reports
        )
        report_sessions = [
            session
            for report in weekly_reports
            for key in ("completed_discussions", "skipped_discussions")
            for session in report.get("behavior_facts", {}).get(key, [])
        ]
        checks["weekly_book_ids"] = all(
            isinstance(book_id, str) and re.fullmatch(r"[0-9a-f]{64}", book_id) and book_id in known_book_ids
            for session in report_sessions
            for book_id in session.get("stable_book_ids", [])
        )
        checks["weekly_discussion_partition"] = all(
            not (
                {item.get("session_id") for item in report.get("behavior_facts", {}).get("completed_discussions", [])}
                & {item.get("session_id") for item in report.get("behavior_facts", {}).get("skipped_discussions", [])}
            )
            for report in weekly_reports
        )
        checks["weekly_source_paths"] = all(
            isinstance(item.get("path"), str)
            and not Path(item["path"]).is_absolute()
            and (workspace / item["path"]).exists()
            and isinstance(item.get("sha256"), str)
            and bool(re.fullmatch(r"[0-9a-f]{64}", item["sha256"]))
            for report in weekly_reports
            for item in report.get("source_manifest", [])
        )
    except (OSError, ValueError, json.JSONDecodeError, TypeError):
        checks["weekly_report_schema"] = False
        checks["weekly_book_ids"] = False
        checks["weekly_discussion_partition"] = False
        checks["weekly_source_paths"] = False
    failed = [name for name, passed in checks.items() if not passed]
    return {
        "result": "passed" if not failed else "failed",
        "checks": checks,
        "failed_checks": failed,
        "counts": {
            "events": belief_audit["counts"]["events"],
            "candidates": belief_audit["counts"]["candidates"],
            "durable_beliefs": belief_audit["counts"]["durable_beliefs"],
            "weekly_reports": len(weekly_reports),
        },
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    report = subparsers.add_parser("report")
    report.add_argument("--workspace", type=Path, required=True)
    report.add_argument("--week", required=True, help="ISO week, for example 2026-W30")
    report.add_argument("--timezone", default="+08:00")
    report.add_argument("--now", help=argparse.SUPPRESS)
    candidates = subparsers.add_parser("candidates")
    candidates.add_argument("--workspace", type=Path, required=True)
    candidates.add_argument("--now", help=argparse.SUPPRESS)
    decide = subparsers.add_parser("decide")
    decide.add_argument("--workspace", type=Path, required=True)
    decide.add_argument("--candidate-id", required=True)
    decide.add_argument("--decision", choices=["confirm", "revise", "reject", "retire"], required=True)
    decide.add_argument("--user-statement", required=True)
    decide.add_argument("--revised-content")
    decide.add_argument("--at", help=argparse.SUPPRESS)
    audit_parser = subparsers.add_parser("audit")
    audit_parser.add_argument("--workspace", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    configure_console()
    args = parse_args(argv or sys.argv[1:])
    try:
        if args.command == "report":
            result = build_report(args.workspace, args.week, args.timezone, args.now)
        elif args.command == "candidates":
            result = list_candidates(args.workspace.resolve(), args.now)
        elif args.command == "decide":
            result = decide_belief(
                args.workspace,
                args.candidate_id,
                args.decision,
                args.user_statement,
                args.revised_content,
                args.at,
            )
        else:
            result = audit(args.workspace)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("result") not in {"error", "failed"} else 2
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"result": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
