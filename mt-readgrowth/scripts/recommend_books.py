#!/usr/bin/env python3
"""Create auditable V2 node-2 personalized WeRead book recommendations."""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable

sys.dont_write_bytecode = True

from personal_context import (
    PersonalContextFailure,
    assemble_personal_context,
    source_snapshots_current,
)

from read_for_me import (
    PROCESS_CONTEXT,
    ReadForMeFailure,
    SENSITIVE_CONTEXT,
    append_jsonl,
    atomic_json,
    atomic_write,
    canonical_json,
    ensure_workspace,
    file_sha256,
    load_json,
    load_jsonl,
    relative_path,
    sha256_text,
    utc_now,
)
from weread_credentials import CredentialFailure, MISSING_KEY_MESSAGE, resolve_weread_api_key


SCHEMA_VERSION = 1
FLOW_VERSION = "0.1.0"
PROVIDER_SKILL_VERSION = "1.0.4"
GATEWAY = "https://i.weread.qq.com/api/agent/gateway"
TASK_ROOT = Path("knowledge/recommendations")
ROUTES = ("deepen", "counter", "transfer")
ROUTE_LABELS = {"deepen": "深化", "counter": "反方", "transfer": "迁移"}
VALID_JOB_STATES = {"context-review", "candidates-ready", "report-ready", "completed"}
PERSONAL_EVIDENCE_CLASS = "AI延伸应用"
PRIVACY_MODE = "abstract-keywords-only"


class RecommendationFailure(RuntimeError):
    """A safe validation, lifecycle, or provider failure."""


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def object_hash(value: dict[str, Any], field: str) -> str:
    return sha256_text(canonical_json({key: item for key, item in value.items() if key != field}))


def request_hash(request: dict[str, Any]) -> str:
    semantic = {key: value for key, value in request.items() if key not in {"request_sha256", "recorded_at"}}
    return sha256_text(canonical_json(semantic))


def context_hash(context: dict[str, Any]) -> str:
    semantic = json.loads(canonical_json(context))
    semantic.pop("context_sha256", None)
    semantic.pop("reviewed_at", None)
    for field in ("available_sources", "selected_sources"):
        for item in semantic.get(field) or []:
            if isinstance(item, dict) and item.get("source_type") == "current-recommendation-correction":
                source = item.get("source")
                if isinstance(source, dict):
                    source.pop("recorded_at", None)
    return sha256_text(canonical_json(semantic))


def normalized(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[\W_]+", "", text)


def task_dir(workspace: Path, recommendation_id: str) -> Path:
    if not re.fullmatch(r"book-recommendation-[0-9a-f]{16}", recommendation_id):
        raise RecommendationFailure("invalid recommendation_id")
    return ensure_workspace(workspace) / TASK_ROOT / recommendation_id


def event_id(event: str, recommendation_id: str, fingerprint: str) -> str:
    return "book-recommendation-event-" + sha256_text(f"{event}\0{recommendation_id}\0{fingerprint}")


def append_event(
    directory: Path,
    event: str,
    fingerprint: str,
    payload: dict[str, Any],
    at: str,
) -> bool:
    path = directory / "events.jsonl"
    identifier = event_id(event, directory.name, fingerprint)
    if identifier in {str(item.get("event_id")) for item in load_jsonl(path)}:
        return False
    append_jsonl(
        path,
        {
            "schema_version": SCHEMA_VERSION,
            "event_id": identifier,
            "event": event,
            "at": at,
            "recommendation_id": directory.name,
            "payload": payload,
        },
    )
    return True


def save_job(directory: Path, job: dict[str, Any], *, status: str | None = None, now: str | None = None) -> None:
    if status is not None:
        if status not in VALID_JOB_STATES:
            raise RecommendationFailure(f"invalid job status: {status}")
        job["status"] = status
    job["updated_at"] = now or utc_now()
    atomic_json(directory / "job.json", job)


def next_action(job: dict[str, Any]) -> str:
    status = job.get("status")
    if status == "context-review":
        return "review personal context and run context --confirm"
    if status == "candidates-ready":
        return "generate result JSON from recommendation-input.json, then run apply"
    if status == "report-ready":
        return "run audit"
    if status == "completed":
        return "read report.md, inspect a selected book, or start a new recommendation request"
    return "inspect the recommendation task"


def start_recommendation(
    workspace: Path,
    *,
    goal: str,
    constraints: Iterable[str] = (),
    per_route: int = 2,
    now: str | None = None,
) -> dict[str, Any]:
    workspace = ensure_workspace(workspace)
    goal = goal.strip()
    clean_constraints = [value.strip() for value in constraints if value.strip()]
    if not goal:
        raise RecommendationFailure("goal cannot be empty")
    if SENSITIVE_CONTEXT.search(goal) or any(SENSITIVE_CONTEXT.search(item) for item in clean_constraints):
        raise RecommendationFailure("recommendation request appears to contain credentials or secret-bearing text")
    if per_route not in {1, 2, 3}:
        raise RecommendationFailure("per_route must be 1, 2, or 3")
    base = {
        "schema_version": SCHEMA_VERSION,
        "goal": goal,
        "constraints": clean_constraints,
        "per_route": per_route,
    }
    recommendation_id = "book-recommendation-" + sha256_text(canonical_json(base))[:16]
    directory = task_dir(workspace, recommendation_id)
    recorded_at = now or utc_now()
    request = {
        **base,
        "recommendation_id": recommendation_id,
        "recorded_at": recorded_at,
        "declared_gaps": ([] if clean_constraints else ["reading_constraints"]),
    }
    request["request_sha256"] = request_hash(request)
    request_path = directory / "request.json"
    if request_path.exists():
        current = load_json(request_path)
        if current.get("request_sha256") != request["request_sha256"]:
            raise RecommendationFailure("deterministic recommendation_id already has a different request")
        job = load_json(directory / "job.json")
        return {
            "result": "unchanged",
            "recommendation_id": recommendation_id,
            "status": job.get("status"),
            "next_action": next_action(job),
        }
    directory.mkdir(parents=True, exist_ok=True)
    atomic_json(request_path, request)
    job = {
        "schema_version": SCHEMA_VERSION,
        "flow_version": FLOW_VERSION,
        "recommendation_id": recommendation_id,
        "status": "context-review",
        "created_at": recorded_at,
        "updated_at": recorded_at,
        "report_version": 0,
        "last_error": None,
        "provider_skill_version": PROVIDER_SKILL_VERSION,
    }
    atomic_json(directory / "job.json", job)
    append_event(
        directory,
        "book-recommendation-started",
        request["request_sha256"],
        {"request_sha256": request["request_sha256"], "status": "context-review"},
        recorded_at,
    )
    return {
        "result": "created",
        "recommendation_id": recommendation_id,
        "status": "context-review",
        "declared_gaps": request["declared_gaps"],
        "next_action": next_action(job),
    }


def context_source_packet(
    workspace: Path,
    request: dict[str, Any],
    *,
    now: str | None = None,
) -> dict[str, Any]:
    current_sources: list[dict[str, Any]] = [
        {
            "source_id": "request:goal",
            "source_type": "current-recommendation-goal",
            "status": "current-user-statement",
            "content": request["goal"],
            "source": {
                "path": f"knowledge/recommendations/{request['recommendation_id']}/request.json",
                "field": "goal",
            },
            "selected_by_default": True,
            "requires_explicit_selection": False,
        }
    ]
    for index, content in enumerate(request.get("constraints") or [], start=1):
        current_sources.append(
            {
                "source_id": f"request:constraint:{index}",
                "source_type": "current-recommendation-constraint",
                "status": "current-user-statement",
                "content": content,
                "source": {
                    "path": f"knowledge/recommendations/{request['recommendation_id']}/request.json",
                    "field": f"constraints[{index - 1}]",
                },
                "selected_by_default": True,
                "requires_explicit_selection": False,
            }
        )
    try:
        return assemble_personal_context(
            workspace,
            route="recommendation",
            current_sources=current_sources,
            now=now,
        )
    except PersonalContextFailure as exc:
        raise RecommendationFailure(str(exc)) from exc


def context_candidates(workspace: Path, request: dict[str, Any]) -> list[dict[str, Any]]:
    """Compatibility view of the shared context packet."""
    return context_source_packet(workspace, request)["available_sources"]


def build_reading_snapshot(workspace: Path, recommendation_id: str, *, now: str | None = None) -> dict[str, Any]:
    overview_path = workspace / "catalog" / "overview.json"
    shelf_path = workspace / "integrations" / "weread" / "shelf.json"
    overview = load_json(overview_path) if overview_path.is_file() else {}
    entries: list[dict[str, Any]] = []
    for item in overview.get("entries") or []:
        if not isinstance(item, dict) or item.get("provider_type") == "mp":
            continue
        entries.append(
            {
                "anchor_id": str(item.get("provider_identity") or f"local:{item.get('stable_book_id') or ''}"),
                "weread_book_id": item.get("weread_book_id"),
                "stable_book_id": item.get("stable_book_id"),
                "title": item.get("title"),
                "author": item.get("author"),
                "registration_status": item.get("registration_status"),
                "finish_reading": bool(item.get("finish_reading")),
                "read_update_time": item.get("read_update_time"),
                "link_status": item.get("link_status"),
                "text_status": item.get("local_text_status") or item.get("text_status"),
                "analysis_status": item.get("analysis_status"),
                "discussion_available": bool(item.get("discussion_available")),
            }
        )
    source = overview.get("source") if isinstance(overview.get("source"), dict) else {}
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "recommendation_id": recommendation_id,
        "recorded_at": now or utc_now(),
        "provider": "weread",
        "provider_skill_version": source.get("provider_skill_version") or PROVIDER_SKILL_VERSION,
        "shelf_synced_at": source.get("shelf_synced_at"),
        "shelf_is_live_for_this_request": False,
        "entries": entries,
        "source_paths": [
            relative_path(path, workspace)
            for path in (overview_path, shelf_path)
            if path.is_file()
        ],
        "evidence_limits": [
            "保存的书架是历史快照，除非本次另有实时接口证据，否则不得称为当前状态。",
            "阅读时长、完成状态和单次讨论只能作为注意力信号，不能证明长期兴趣或认同。",
            "本地书名作者匹配不自动确认微信读书版本。",
        ],
    }
    snapshot["snapshot_sha256"] = object_hash(snapshot, "snapshot_sha256")
    return snapshot


def review_context(
    workspace: Path,
    recommendation_id: str,
    *,
    include: Iterable[str] = (),
    exclude: Iterable[str] = (),
    corrections: Iterable[str] = (),
    confirm: bool = False,
    now: str | None = None,
) -> dict[str, Any]:
    workspace = ensure_workspace(workspace)
    directory = task_dir(workspace, recommendation_id)
    job = load_json(directory / "job.json")
    if job.get("status") == "completed":
        raise RecommendationFailure("completed recommendation tasks are immutable; start a new request")
    request = load_json(directory / "request.json")
    source_packet = context_source_packet(workspace, request, now=now)
    available = source_packet["available_sources"]
    existing_path = directory / "context.json"
    existing = load_json(existing_path) if existing_path.exists() else None
    if existing:
        known = {str(item.get("source_id")) for item in available}
        for item in existing.get("available_sources") or []:
            if (
                isinstance(item, dict)
                and item.get("source_type") == "current-recommendation-correction"
                and str(item.get("source_id")) not in known
            ):
                available.append(item)
                known.add(str(item.get("source_id")))
    by_id = {str(item["source_id"]): item for item in available}
    include_ids, exclude_ids = set(include), set(exclude)
    if existing:
        include_ids.update(
            str(item.get("source_id"))
            for item in existing.get("selected_sources") or []
            if isinstance(item, dict) and item.get("selection_basis") == "explicit-user-context-confirmation"
        )
    unknown = sorted((include_ids | exclude_ids) - set(by_id))
    if unknown:
        raise RecommendationFailure("unknown context source IDs: " + ", ".join(unknown))
    correction_items: list[dict[str, Any]] = []
    for raw in corrections:
        content = raw.strip()
        if not content:
            continue
        if SENSITIVE_CONTEXT.search(content):
            raise RecommendationFailure("context correction appears to contain credentials or secret-bearing text")
        source_id = "correction:" + sha256_text(content)[:16]
        correction_items.append(
            {
                "source_id": source_id,
                "source_type": "current-recommendation-correction",
                "status": "current-user-statement",
                "content": content,
                "source": {"event": "context-correction", "recorded_at": now or utc_now()},
                "selected_by_default": True,
                "requires_explicit_selection": False,
            }
        )
        include_ids.add(source_id)
        by_id[source_id] = correction_items[-1]
    available.extend(correction_items)
    selected: list[dict[str, Any]] = []
    for candidate in available:
        source_id = str(candidate["source_id"])
        is_request = str(candidate.get("source_type") or "").startswith("current-recommendation-")
        enabled = is_request
        basis = "current-recommendation-request" if is_request else None
        if confirm and candidate.get("selected_by_default") and source_id not in exclude_ids:
            enabled = True
            basis = str(candidate.get("selection_basis") or "confirmed-context-default")
        if confirm and source_id in include_ids and source_id not in exclude_ids:
            enabled, basis = True, "explicit-user-context-confirmation"
        if source_id in exclude_ids:
            enabled, basis = False, "explicit-user-context-exclusion"
        if enabled:
            selected.append({**candidate, "selection_basis": basis})
    selected_ids = {str(item["source_id"]) for item in selected}
    excluded_sources = list(source_packet["excluded_sources"])
    for candidate in available:
        source_id = str(candidate["source_id"])
        if source_id in selected_ids:
            continue
        if source_id in exclude_ids:
            exclusion_basis = "explicit-user-context-exclusion"
        elif candidate.get("requires_explicit_selection"):
            exclusion_basis = "explicit-selection-required"
        else:
            exclusion_basis = "context-default-not-confirmed"
        excluded_sources.append({**candidate, "exclusion_basis": exclusion_basis})
    context = {
        "schema_version": SCHEMA_VERSION,
        "recommendation_id": recommendation_id,
        "review_status": "confirmed" if confirm else "pending",
        "reviewed_at": now or utc_now(),
        "memory_mode": source_packet["memory_mode"],
        "memory_mode_configured": source_packet["memory_mode_configured"],
        "source_snapshots": source_packet["source_snapshots"],
        "source_bundle_sha256": source_packet["source_bundle_sha256"],
        "available_sources": available,
        "selected_sources": selected,
        "excluded_sources": excluded_sources,
        "selection_rules": {
            "request_enabled": True,
            "confirmed_revised_default_after_confirmation": True,
            "candidate_provisional_session_default": False,
            "assistant_messages_are_user_context": False,
        },
    }
    context["context_sha256"] = context_hash(context)
    atomic_json(directory / "context.json", context)
    if confirm:
        snapshot = build_reading_snapshot(workspace, recommendation_id, now=now)
        atomic_json(directory / "reading-snapshot.json", snapshot)
    append_event(
        directory,
        "book-recommendation-context-confirmed" if confirm else "book-recommendation-context-previewed",
        context["context_sha256"],
        {
            "context_sha256": context["context_sha256"],
            "selected_source_ids": [item["source_id"] for item in selected],
            "included_source_ids": sorted(include_ids),
            "excluded_source_ids": sorted(exclude_ids),
        },
        now or utc_now(),
    )
    save_job(directory, job, status="context-review", now=now)
    return {
        "result": "confirmed" if confirm else "preview",
        "recommendation_id": recommendation_id,
        "status": "context-review",
        "available_sources": available,
        "selected_sources": selected,
        "excluded_sources": excluded_sources,
        "reading_snapshot": (
            load_json(directory / "reading-snapshot.json") if confirm else None
        ),
        "next_action": "generate discovery-plan.json and run prepare" if confirm else "remove, correct, or include sources, then run context --confirm",
    }


def require_text(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise RecommendationFailure(f"{label} cannot be empty")
    return text


def validate_discovery_plan(
    plan: dict[str, Any],
    request: dict[str, Any],
    context: dict[str, Any],
    snapshot: dict[str, Any],
) -> None:
    if plan.get("schema_version") != SCHEMA_VERSION:
        raise RecommendationFailure("discovery plan has an unsupported schema_version")
    if plan.get("recommendation_id") != request.get("recommendation_id"):
        raise RecommendationFailure("discovery plan recommendation_id mismatch")
    if plan.get("request_sha256") != request.get("request_sha256"):
        raise RecommendationFailure("discovery plan request hash mismatch")
    if plan.get("context_sha256") != context.get("context_sha256"):
        raise RecommendationFailure("discovery plan context hash mismatch")
    if plan.get("reading_snapshot_sha256") != snapshot.get("snapshot_sha256"):
        raise RecommendationFailure("discovery plan reading snapshot hash mismatch")
    if plan.get("privacy_mode") != PRIVACY_MODE:
        raise RecommendationFailure(f"discovery plan privacy_mode must be {PRIVACY_MODE}")
    if plan.get("provider_feed") is not True:
        raise RecommendationFailure("discovery plan must include the WeRead personalized feed")
    generator = plan.get("generator")
    if not isinstance(generator, dict) or not generator.get("name") or not generator.get("version"):
        raise RecommendationFailure("discovery plan generator identity is required")
    route_plans = plan.get("routes")
    if not isinstance(route_plans, dict) or set(route_plans) != set(ROUTES):
        raise RecommendationFailure("discovery plan must contain deepen, counter, and transfer")
    allowed_anchors = {
        str(item.get("weread_book_id"))
        for item in snapshot.get("entries") or []
        if item.get("weread_book_id")
    }
    approved_context = [
        str(item.get("content") or "").strip()
        for item in context.get("selected_sources") or []
        if str(item.get("content") or "").strip()
    ]
    for route in ROUTES:
        route_plan = route_plans[route]
        if not isinstance(route_plan, dict):
            raise RecommendationFailure(f"{route} discovery plan must be an object")
        require_text(route_plan.get("rationale"), f"{route}.rationale")
        queries = route_plan.get("search_queries")
        anchors = route_plan.get("anchor_book_ids")
        if not isinstance(queries, list) or len(queries) > 3:
            raise RecommendationFailure(f"{route}.search_queries must contain at most three items")
        if not isinstance(anchors, list) or len(anchors) > 2:
            raise RecommendationFailure(f"{route}.anchor_book_ids must contain at most two items")
        if route in {"counter", "transfer"} and not queries:
            raise RecommendationFailure(f"{route} needs at least one abstract search query")
        if route == "deepen" and not (queries or anchors):
            raise RecommendationFailure("deepen needs a search query or a WeRead anchor")
        for query in queries:
            query_text = require_text(query, f"{route} search query")
            if len(query_text) > 80:
                raise RecommendationFailure(f"{route} search query exceeds 80 characters")
            if SENSITIVE_CONTEXT.search(query_text) or PROCESS_CONTEXT.search(query_text):
                raise RecommendationFailure(f"{route} search query contains private or process-specific text")
            for private_text in approved_context:
                if len(private_text) >= 20 and normalized(query_text) == normalized(private_text):
                    raise RecommendationFailure(f"{route} search query copies a full personal-context statement")
        unknown_anchors = sorted({str(item) for item in anchors} - allowed_anchors)
        if unknown_anchors:
            raise RecommendationFailure(f"{route} uses unknown WeRead anchors: {', '.join(unknown_anchors)}")
    require_text(route_plans["counter"].get("challenged_premise"), "counter.challenged_premise")
    require_text(route_plans["transfer"].get("transfer_bridge"), "transfer.transfer_bridge")


def unwrap_provider_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("upgrade_info"):
        raise RecommendationFailure("The installed WeRead Skill must be upgraded before recommendation discovery")
    if payload.get("errcode") not in (None, 0):
        raise RecommendationFailure(f"WeRead API returned error code {payload.get('errcode')}")
    data = payload.get("data")
    result = data if isinstance(data, dict) else payload
    if not isinstance(result, dict):
        raise RecommendationFailure("WeRead recommendation response is not an object")
    return result


class LiveWeReadClient:
    def __init__(self, api_key: str):
        if not api_key.strip():
            raise RecommendationFailure(MISSING_KEY_MESSAGE)
        self.api_key = api_key.strip()

    def call(self, api_name: str, **params: Any) -> dict[str, Any]:
        body = {"api_name": api_name, **params, "skill_version": PROVIDER_SKILL_VERSION}
        request = urllib.request.Request(
            GATEWAY,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": f"mt-readgrowth-recommend/{FLOW_VERSION}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RecommendationFailure(f"WeRead request failed with HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RecommendationFailure("WeRead recommendation request failed") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RecommendationFailure("WeRead returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RecommendationFailure("WeRead returned a non-object response")
        return unwrap_provider_payload(payload)


class FixtureWeReadClient:
    def __init__(self, path: Path):
        fixture = load_json(path.resolve())
        calls = fixture.get("calls")
        if not isinstance(calls, list):
            raise RecommendationFailure("API fixture must contain calls[]")
        self.calls = calls
        self.position = 0

    def call(self, api_name: str, **params: Any) -> dict[str, Any]:
        if self.position >= len(self.calls):
            raise RecommendationFailure(f"API fixture has no response for {api_name}")
        expected = self.calls[self.position]
        self.position += 1
        if not isinstance(expected, dict):
            raise RecommendationFailure("API fixture call must be an object")
        if expected.get("api_name") != api_name or expected.get("params") != params:
            raise RecommendationFailure(f"API fixture call mismatch at position {self.position}")
        response = expected.get("response")
        if not isinstance(response, dict):
            raise RecommendationFailure("API fixture response must be an object")
        return unwrap_provider_payload(response)


def provider_source_id(source: dict[str, Any]) -> str:
    return "provider:" + sha256_text(canonical_json(source))[:16]


def provider_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().casefold() in {"1", "true", "yes"}


def safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return 0


def book_info_from_row(row: dict[str, Any]) -> dict[str, Any]:
    nested = row.get("bookInfo")
    if isinstance(nested, dict):
        return {**nested, **{key: value for key, value in row.items() if key not in {"bookInfo", "book"}}}
    nested_book = row.get("book")
    if isinstance(nested_book, dict):
        nested_info = nested_book.get("bookInfo")
        if isinstance(nested_info, dict):
            return {**nested_info, **{key: value for key, value in row.items() if key != "book"}}
    return row


def normalize_candidate(raw: dict[str, Any], source: dict[str, Any]) -> dict[str, Any] | None:
    info = book_info_from_row(raw)
    book_id = str(info.get("bookId") or "").strip()
    title = str(info.get("title") or "").strip()
    if not book_id or not title:
        return None
    provider_source = {**source}
    reason = info.get("reason") or raw.get("reason")
    if reason:
        provider_source["provider_reason"] = str(reason)
    provider_source["source_id"] = provider_source_id(provider_source)
    soldout = provider_bool(info.get("soldout"))
    return {
        "candidate_id": f"weread-book:{book_id}",
        "book_id": book_id,
        "title": title,
        "author": str(info.get("author") or "").strip(),
        "cover": info.get("cover"),
        "deep_link": info.get("deepLink"),
        "intro": str(info.get("intro") or "").strip(),
        "category": info.get("category"),
        "publisher": info.get("publisher"),
        "new_rating": info.get("newRating"),
        "new_rating_count": info.get("newRatingCount"),
        "reading_count": info.get("readingCount"),
        "price": info.get("price"),
        "pay_type": info.get("payType"),
        "soldout": soldout,
        "eligible": not soldout,
        "ineligible_reasons": (["soldout"] if soldout else []),
        "provider_sources": [provider_source],
        "shelf_evidence": None,
        "local_evidence": None,
    }


def add_candidate(target: dict[str, dict[str, Any]], candidate: dict[str, Any] | None) -> None:
    if candidate is None:
        return
    identifier = candidate["candidate_id"]
    existing = target.get(identifier)
    if existing is None:
        target[identifier] = candidate
        return
    known = {item["source_id"] for item in existing["provider_sources"]}
    for source in candidate["provider_sources"]:
        if source["source_id"] not in known:
            existing["provider_sources"].append(source)
            known.add(source["source_id"])
    for field in (
        "author", "cover", "deep_link", "intro", "category", "publisher",
        "new_rating", "new_rating_count", "reading_count", "price", "pay_type",
    ):
        if existing.get(field) in (None, "") and candidate.get(field) not in (None, ""):
            existing[field] = candidate[field]
    if candidate.get("soldout"):
        existing["soldout"] = True
        existing["eligible"] = False
        if "soldout" not in existing["ineligible_reasons"]:
            existing["ineligible_reasons"].append("soldout")


def annotate_candidates(candidates: list[dict[str, Any]], snapshot: dict[str, Any]) -> None:
    by_book_id = {
        str(item.get("weread_book_id")): item
        for item in snapshot.get("entries") or []
        if item.get("weread_book_id")
    }
    by_title_author = {
        (normalized(item.get("title")), normalized(item.get("author"))): item
        for item in snapshot.get("entries") or []
        if item.get("title")
    }
    for candidate in candidates:
        evidence = by_book_id.get(candidate["book_id"])
        if evidence:
            candidate["shelf_evidence"] = {
                "match_basis": "weread-book-id",
                "on_saved_shelf_snapshot": True,
                "finish_reading": evidence.get("finish_reading"),
                "read_update_time": evidence.get("read_update_time"),
            }
        local = evidence or by_title_author.get((normalized(candidate["title"]), normalized(candidate["author"])))
        if local and local.get("stable_book_id"):
            candidate["local_evidence"] = {
                "match_basis": "weread-book-id" if evidence else "title-author-candidate-only",
                "stable_book_id": local.get("stable_book_id"),
                "link_status": local.get("link_status"),
                "text_status": local.get("text_status"),
                "analysis_status": local.get("analysis_status"),
                "discussion_available": local.get("discussion_available"),
            }


def collect_candidates(
    client: Any,
    plan: dict[str, Any],
    snapshot: dict[str, Any],
) -> list[dict[str, Any]]:
    collected: dict[str, dict[str, Any]] = {}
    if plan.get("provider_feed", True):
        data = client.call("/book/recommend", count=12, maxIdx=0)
        for index, raw in enumerate(data.get("books") or []):
            if isinstance(raw, dict):
                add_candidate(
                    collected,
                    normalize_candidate(
                        raw,
                        {"source_type": "weread-personal-feed", "route_hint": None, "source_index": index},
                    ),
                )
    for route in ROUTES:
        route_plan = plan["routes"][route]
        for anchor in route_plan.get("anchor_book_ids") or []:
            data = client.call("/book/similar", bookId=str(anchor), count=12, maxIdx=0)
            similar = data.get("booksimilar")
            rows = similar.get("books") if isinstance(similar, dict) else []
            for index, raw in enumerate(rows or []):
                if isinstance(raw, dict):
                    add_candidate(
                        collected,
                        normalize_candidate(
                            raw,
                            {
                                "source_type": "weread-similar",
                                "route_hint": route,
                                "anchor_book_id": str(anchor),
                                "source_index": index,
                            },
                        ),
                    )
        for query in route_plan.get("search_queries") or []:
            data = client.call("/store/search", keyword=str(query), scope=10, count=5, maxIdx=0)
            for group in data.get("results") or []:
                if not isinstance(group, dict):
                    continue
                for index, raw in enumerate(group.get("books") or []):
                    if isinstance(raw, dict):
                        add_candidate(
                            collected,
                            normalize_candidate(
                                raw,
                                {
                                    "source_type": "weread-search",
                                    "route_hint": route,
                                    "query": str(query),
                                    "source_index": index,
                                },
                            ),
                        )
    values = list(collected.values())
    annotate_candidates(values, snapshot)
    values.sort(
        key=lambda item: (
            not bool(item.get("eligible")),
            -safe_int(item.get("new_rating_count")),
            normalized(item.get("title")),
            item["candidate_id"],
        )
    )
    return values


def build_manifest(workspace: Path, directory: Path, extra_paths: Iterable[Path]) -> dict[str, Any]:
    paths = [
        directory / "request.json",
        directory / "context.json",
        directory / "reading-snapshot.json",
        directory / "discovery-plan.json",
        directory / "candidates.json",
    ]
    paths.extend(extra_paths)
    unique = {relative_path(path, workspace): path for path in paths if path.is_file()}
    return {
        "schema_version": SCHEMA_VERSION,
        "recommendation_id": directory.name,
        "sources": [
            {"path": relative, "sha256": file_sha256(path)}
            for relative, path in sorted(unique.items())
        ],
    }


def record_failure(directory: Path, job: dict[str, Any], safe_message: str, *, now: str | None = None) -> None:
    timestamp = now or utc_now()
    append_event(
        directory,
        "book-recommendation-failed",
        sha256_text(safe_message),
        {"code": "recommendation-failed", "safe_message": safe_message},
        timestamp,
    )
    job["last_error"] = {"code": "recommendation-failed", "at": timestamp, "safe_message": safe_message}
    save_job(directory, job, now=timestamp)


def prepare_candidates(
    workspace: Path,
    recommendation_id: str,
    plan_path: Path,
    *,
    api_fixture: Path | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    workspace = ensure_workspace(workspace)
    directory = task_dir(workspace, recommendation_id)
    job = load_json(directory / "job.json")
    if job.get("status") == "completed":
        raise RecommendationFailure("completed recommendation tasks are immutable; start a new request")
    request = load_json(directory / "request.json")
    context = load_json(directory / "context.json")
    if context.get("review_status") != "confirmed":
        raise RecommendationFailure("personal context has not been confirmed")
    if context.get("context_sha256") != context_hash(context):
        raise RecommendationFailure("personal context hash failed validation; review context again")
    try:
        snapshots_current = source_snapshots_current(workspace, context.get("source_snapshots"))
    except PersonalContextFailure as exc:
        raise RecommendationFailure(str(exc)) from exc
    if not snapshots_current:
        raise RecommendationFailure("personal context sources changed; review and confirm context again before prepare")
    snapshot = load_json(directory / "reading-snapshot.json")
    plan = load_json(plan_path.resolve())
    try:
        validate_discovery_plan(plan, request, context, snapshot)
        if api_fixture:
            client = FixtureWeReadClient(api_fixture)
        else:
            try:
                api_key = resolve_weread_api_key(workspace)
            except CredentialFailure as exc:
                raise RecommendationFailure(str(exc)) from exc
            client = LiveWeReadClient(api_key)
        candidates = collect_candidates(client, plan, snapshot)
        needed = int(request["per_route"]) * len(ROUTES)
        eligible_count = sum(1 for item in candidates if item.get("eligible"))
        if eligible_count < needed:
            raise RecommendationFailure(f"only {eligible_count} eligible candidates were found; at least {needed} are required")
    except (RecommendationFailure, ReadForMeFailure) as exc:
        record_failure(directory, job, str(exc), now=now)
        raise
    atomic_json(directory / "discovery-plan.json", plan)
    candidate_packet = {
        "schema_version": SCHEMA_VERSION,
        "recommendation_id": recommendation_id,
        "provider": "weread",
        "provider_skill_version": PROVIDER_SKILL_VERSION,
        "fetched_at": now or utc_now(),
        "candidate_count": len(candidates),
        "eligible_count": eligible_count,
        "candidates": candidates,
    }
    candidate_packet["candidates_sha256"] = object_hash(candidate_packet, "candidates_sha256")
    atomic_json(directory / "candidates.json", candidate_packet)
    extra_paths = [workspace / path for path in snapshot.get("source_paths") or []]
    manifest = build_manifest(workspace, directory, extra_paths)
    atomic_json(directory / "source-manifest.json", manifest)
    recommendation_input = {
        "schema_version": SCHEMA_VERSION,
        "recommendation_id": recommendation_id,
        "request": request,
        "request_sha256": request["request_sha256"],
        "context_sha256": context["context_sha256"],
        "reading_snapshot_sha256": snapshot["snapshot_sha256"],
        "discovery_plan_sha256": file_sha256(directory / "discovery-plan.json"),
        "candidates_sha256": candidate_packet["candidates_sha256"],
        "provider_fetched_at": candidate_packet["fetched_at"],
        "source_manifest_sha256": file_sha256(directory / "source-manifest.json"),
        "context_sources": context["selected_sources"],
        "reading_snapshot": snapshot,
        "discovery_routes": plan["routes"],
        "candidates": candidates,
        "route_requirements": {
            "routes": list(ROUTES),
            "per_route": request["per_route"],
            "evidence_class": PERSONAL_EVIDENCE_CLASS,
            "counter_requires_challenged_premise": True,
            "transfer_requires_bridge": True,
        },
        "scope_limits": [
            "候选书信息来自微信读书书城元数据、相似推荐和搜索结果，不等于已核验全书观点。",
            "微信读书个性化推荐是黑盒候选来源，不能据此断言用户长期兴趣。",
            "推荐是 AI 路由，不代表作者明确表达，也不自动下载、导入或确认版本。",
        ],
    }
    recommendation_input["input_sha256"] = object_hash(recommendation_input, "input_sha256")
    atomic_json(directory / "recommendation-input.json", recommendation_input)
    job["last_error"] = None
    save_job(directory, job, status="candidates-ready", now=now)
    append_event(
        directory,
        "book-recommendation-candidates-prepared",
        recommendation_input["input_sha256"],
        {
            "input_sha256": recommendation_input["input_sha256"],
            "candidate_count": len(candidates),
            "eligible_count": eligible_count,
        },
        now or utc_now(),
    )
    return {
        "result": "prepared",
        "recommendation_id": recommendation_id,
        "status": "candidates-ready",
        "candidate_count": len(candidates),
        "eligible_count": eligible_count,
        "input_path": relative_path(directory / "recommendation-input.json", workspace),
        "next_action": next_action(job),
    }


def validate_input_fresh(directory: Path, workspace: Path, packet: dict[str, Any]) -> None:
    if packet.get("input_sha256") != object_hash(packet, "input_sha256"):
        raise RecommendationFailure("recommendation input hash is stale")
    request = load_json(directory / "request.json")
    context = load_json(directory / "context.json")
    snapshot = load_json(directory / "reading-snapshot.json")
    candidates = load_json(directory / "candidates.json")
    if request.get("request_sha256") != request_hash(request) or request.get("request_sha256") != packet.get("request_sha256"):
        raise RecommendationFailure("recommendation request changed after preparation")
    if context.get("context_sha256") != context_hash(context) or context.get("context_sha256") != packet.get("context_sha256"):
        raise RecommendationFailure("personal context changed after preparation")
    if snapshot.get("snapshot_sha256") != object_hash(snapshot, "snapshot_sha256") or snapshot.get("snapshot_sha256") != packet.get("reading_snapshot_sha256"):
        raise RecommendationFailure("reading snapshot changed after preparation")
    if candidates.get("candidates_sha256") != object_hash(candidates, "candidates_sha256") or candidates.get("candidates_sha256") != packet.get("candidates_sha256"):
        raise RecommendationFailure("candidate snapshot changed after preparation")
    if file_sha256(directory / "discovery-plan.json") != packet.get("discovery_plan_sha256"):
        raise RecommendationFailure("discovery plan changed after preparation")
    manifest_path = directory / "source-manifest.json"
    manifest = load_json(manifest_path)
    if file_sha256(manifest_path) != packet.get("source_manifest_sha256"):
        raise RecommendationFailure("source manifest changed after preparation")
    for item in manifest.get("sources") or []:
        source = workspace / str(item.get("path") or "")
        if not source.is_file() or file_sha256(source) != item.get("sha256"):
            raise RecommendationFailure(f"prepared source changed: {item.get('path')}")


def validate_result(result: dict[str, Any], packet: dict[str, Any]) -> None:
    if result.get("schema_version") != SCHEMA_VERSION:
        raise RecommendationFailure("result has an unsupported schema_version")
    if result.get("recommendation_id") != packet.get("recommendation_id"):
        raise RecommendationFailure("result recommendation_id mismatch")
    for field in ("input_sha256", "request_sha256", "context_sha256", "reading_snapshot_sha256", "candidates_sha256", "source_manifest_sha256"):
        if result.get(field) != packet.get(field):
            raise RecommendationFailure(f"result {field} mismatch")
    generator = result.get("generator")
    if not isinstance(generator, dict) or not generator.get("name") or not generator.get("version"):
        raise RecommendationFailure("result generator identity is required")
    input_context = {
        str(item.get("source_id")): str(item.get("status"))
        for item in packet.get("context_sources") or []
    }
    result_context = result.get("context_sources")
    if not isinstance(result_context, list):
        raise RecommendationFailure("result context_sources must be a list")
    result_context_map = {
        str(item.get("source_id")): str(item.get("evidence_status"))
        for item in result_context
        if isinstance(item, dict)
    }
    if result_context_map != input_context:
        raise RecommendationFailure("result context_sources must exactly match the approved context snapshot")
    if len(result_context) != len(result_context_map):
        raise RecommendationFailure("result context_sources must not contain duplicates")
    candidates = {
        str(item.get("candidate_id")): item
        for item in packet.get("candidates") or []
        if item.get("eligible")
    }
    routes = result.get("routes")
    if not isinstance(routes, list) or len(routes) != len(ROUTES) or {str(item.get("route")) for item in routes if isinstance(item, dict)} != set(ROUTES):
        raise RecommendationFailure("result must contain deepen, counter, and transfer routes")
    used: set[str] = set()
    per_route = int(packet["route_requirements"]["per_route"])
    for route_item in routes:
        if not isinstance(route_item, dict):
            raise RecommendationFailure("each result route must be an object")
        route = str(route_item.get("route"))
        require_text(route_item.get("route_reason"), f"{route}.route_reason")
        if route == "counter":
            require_text(route_item.get("challenged_premise"), "counter.challenged_premise")
        if route == "transfer":
            require_text(route_item.get("transfer_bridge"), "transfer.transfer_bridge")
        recommendations = route_item.get("recommendations")
        if not isinstance(recommendations, list) or len(recommendations) != per_route:
            raise RecommendationFailure(f"{route} must contain exactly {per_route} recommendations")
        for index, recommendation in enumerate(recommendations):
            if not isinstance(recommendation, dict):
                raise RecommendationFailure(f"{route}.recommendations[{index}] must be an object")
            candidate_id = str(recommendation.get("candidate_id") or "")
            if candidate_id not in candidates:
                raise RecommendationFailure(f"{route} uses an unknown or ineligible candidate: {candidate_id}")
            if candidate_id in used:
                raise RecommendationFailure(f"candidate appears in more than one route: {candidate_id}")
            used.add(candidate_id)
            if recommendation.get("evidence_class") != PERSONAL_EVIDENCE_CLASS:
                raise RecommendationFailure(f"{route} recommendation evidence_class must be {PERSONAL_EVIDENCE_CLASS}")
            for field in ("why_now", "route_mapping", "uncertainty", "next_action"):
                require_text(recommendation.get(field), f"{route}.{field}")
            context_ids = recommendation.get("user_context_source_ids")
            if not isinstance(context_ids, list) or not context_ids:
                raise RecommendationFailure(f"{route} recommendation needs user context sources")
            unknown_context = sorted({str(item) for item in context_ids} - set(input_context))
            if unknown_context:
                raise RecommendationFailure(f"{route} recommendation uses unapproved context: {', '.join(unknown_context)}")
            provider_ids = recommendation.get("provider_source_ids")
            allowed_provider = {
                str(item.get("source_id"))
                for item in candidates[candidate_id].get("provider_sources") or []
            }
            if not isinstance(provider_ids, list) or not provider_ids or not set(map(str, provider_ids)).issubset(allowed_provider):
                raise RecommendationFailure(f"{route} recommendation needs valid provider source IDs")
    if not isinstance(result.get("evidence_gaps"), list) or not result["evidence_gaps"]:
        raise RecommendationFailure("result evidence_gaps cannot be empty")
    if not isinstance(result.get("scope_limits"), list) or not result["scope_limits"]:
        raise RecommendationFailure("result scope_limits cannot be empty")


def context_label(source_id: str, packet: dict[str, Any]) -> str:
    for item in packet.get("context_sources") or []:
        if str(item.get("source_id")) == source_id:
            labels = {
                "current-recommendation-goal": "本次推荐目标",
                "current-recommendation-constraint": "本次阅读约束",
                "current-recommendation-correction": "本次用户补充",
                "durable-belief": f"长期认知（{item.get('status')}）",
                "candidate-belief": f"候选认知（{item.get('status')}）",
                "historical-session-user-statement": "历史会话中的用户原话",
            }
            return labels.get(str(item.get("source_type")), str(item.get("source_type")))
    return source_id


def provider_label(source_id: str, candidate: dict[str, Any]) -> str:
    for item in candidate.get("provider_sources") or []:
        if item.get("source_id") != source_id:
            continue
        source_type = item.get("source_type")
        if source_type == "weread-personal-feed":
            return "微信读书为你推荐"
        if source_type == "weread-similar":
            return "微信读书相似推荐"
        if source_type == "weread-search":
            return f"微信书城搜索：{item.get('query')}"
        return str(source_type)
    return source_id


def format_rating(value: Any, count: Any) -> str:
    """Render WeRead's mixed 0-100 and 0-1000 rating values on a 100-point scale."""
    if value is None:
        return "暂无评分"
    try:
        rating_value = float(value)
    except (TypeError, ValueError):
        return "暂无评分"
    if rating_value > 100:
        rating_value /= 10
    rating_text = f"{rating_value:.1f}".rstrip("0").rstrip(".")
    rendered = f"{rating_text}/100"
    if count is not None:
        rendered += f"（{count} 人评分）"
    return rendered


def render_result(result: dict[str, Any], packet: dict[str, Any]) -> str:
    candidates = {str(item["candidate_id"]): item for item in packet.get("candidates") or []}
    selected_context = []
    for item in packet.get("context_sources") or []:
        selected_context.append(
            f"- {context_label(str(item['source_id']), packet)}：{item.get('content')}"
        )
    lines = [
        "# 书籍个性化推荐\n",
        f"> 推荐任务：`{result['recommendation_id']}`  \n",
        f"> 微信书城信息获取时间：`{packet.get('provider_fetched_at') or '未记录'}`  \n",
        "> 推荐属于 AI 路由；书城简介、相似度和平台推荐不等于作者观点已经过全文核验。\n",
        "## 本次要解决的问题\n",
        f"{packet['request']['goal']}\n",
        "## 本次使用的个人信息\n",
        "\n".join(selected_context) + "\n",
    ]
    snapshot = packet.get("reading_snapshot") or {}
    lines.extend(
        [
            "## 阅读数据状态\n",
            f"- 保存的微信书架快照：`{snapshot.get('shelf_synced_at') or '无'}`。\n",
            "- 本次实时候选来自微信读书书城；保存的书架快照不冒充实时书架。\n",
        ]
    )
    route_map = {str(item["route"]): item for item in result["routes"]}
    for route in ROUTES:
        route_item = route_map[route]
        lines.extend([f"## {ROUTE_LABELS[route]}推荐\n", f"{route_item['route_reason']}\n"])
        if route == "counter":
            lines.append(f"**本路线挑战的前提：** {route_item['challenged_premise']}\n")
        if route == "transfer":
            lines.append(f"**迁移桥梁：** {route_item['transfer_bridge']}\n")
        for index, recommendation in enumerate(route_item["recommendations"], start=1):
            candidate = candidates[recommendation["candidate_id"]]
            rating = format_rating(candidate.get("new_rating"), candidate.get("new_rating_count"))
            contexts = "、".join(
                context_label(str(source_id), packet)
                for source_id in recommendation["user_context_source_ids"]
            )
            providers = "、".join(
                provider_label(str(source_id), candidate)
                for source_id in recommendation["provider_source_ids"]
            )
            shelf = candidate.get("shelf_evidence") or {}
            shelf_note = "；历史快照显示已在书架" if shelf.get("on_saved_shelf_snapshot") else ""
            lines.extend(
                [
                    f"### {index}. 《{candidate['title']}》— {candidate.get('author') or '作者信息缺失'}\n",
                    f"- 书城信息：{rating}；{candidate.get('category') or '分类未提供'}{shelf_note}\n",
                    f"- 为什么现在推荐：{recommendation['why_now']}\n",
                    f"- {ROUTE_LABELS[route]}关系：{recommendation['route_mapping']}\n",
                    f"- 使用的你的信息：{contexts}\n",
                    f"- 微信书城依据：{providers}\n",
                    f"- 不确定性：{recommendation['uncertainty']}\n",
                    f"- 下一步：{recommendation['next_action']}\n",
                ]
            )
            if candidate.get("deep_link"):
                lines.append(f"- [打开阅读]({candidate['deep_link']})\n")
    lines.extend(["## 证据缺口\n", *[f"- {item}\n" for item in result["evidence_gaps"]]])
    lines.extend(["## 使用边界\n", *[f"- {item}\n" for item in result["scope_limits"]]])
    return "\n".join(line.rstrip("\n") for line in lines if line != "") + "\n"


def apply_result(
    workspace: Path,
    recommendation_id: str,
    result_path: Path,
    *,
    now: str | None = None,
) -> dict[str, Any]:
    workspace = ensure_workspace(workspace)
    directory = task_dir(workspace, recommendation_id)
    job = load_json(directory / "job.json")
    packet = load_json(directory / "recommendation-input.json")
    try:
        validate_input_fresh(directory, workspace, packet)
        result = load_json(result_path.resolve())
        validate_result(result, packet)
        rendered = render_result(result, packet)
    except (RecommendationFailure, ReadForMeFailure) as exc:
        record_failure(directory, job, str(exc), now=now)
        raise
    target_result = directory / "result.json"
    target_report = directory / "report.md"
    result_content = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if target_result.exists() and target_report.exists():
        if target_result.read_text(encoding="utf-8") == result_content and target_report.read_text(encoding="utf-8") == rendered:
            return {"result": "unchanged", "recommendation_id": recommendation_id, "status": job.get("status"), "next_action": next_action(job)}
        previous = int(job.get("report_version") or 1)
        version_dir = directory / "versions" / f"v{previous:03d}"
        if not version_dir.exists():
            version_dir.mkdir(parents=True)
            atomic_write(version_dir / "result.json", target_result.read_text(encoding="utf-8"))
            atomic_write(version_dir / "report.md", target_report.read_text(encoding="utf-8"))
    atomic_write(target_result, result_content)
    atomic_write(target_report, rendered)
    job["report_version"] = int(job.get("report_version") or 0) + 1
    job["last_error"] = None
    save_job(directory, job, status="report-ready", now=now)
    append_event(
        directory,
        "book-recommendation-result-applied",
        file_sha256(target_result),
        {"result_sha256": file_sha256(target_result), "report_sha256": file_sha256(target_report), "version": job["report_version"]},
        now or utc_now(),
    )
    return {
        "result": "applied",
        "recommendation_id": recommendation_id,
        "status": "report-ready",
        "report_version": job["report_version"],
        "result_path": relative_path(target_result, workspace),
        "report_path": relative_path(target_report, workspace),
        "next_action": next_action(job),
    }


def audit_task(
    workspace: Path,
    recommendation_id: str,
    *,
    complete: bool = True,
    now: str | None = None,
) -> dict[str, Any]:
    workspace = ensure_workspace(workspace)
    directory = task_dir(workspace, recommendation_id)
    checks: dict[str, bool] = {}
    failures: list[str] = []

    def check(name: str, value: bool) -> None:
        checks[name] = bool(value)
        if not value:
            failures.append(name)

    try:
        events = load_jsonl(directory / "events.jsonl")
        ids = [str(item.get("event_id")) for item in events]
        check("event_ids_unique", len(ids) == len(set(ids)) and all(ids))
        job = load_json(directory / "job.json")
        request = load_json(directory / "request.json")
        context = load_json(directory / "context.json")
        snapshot = load_json(directory / "reading-snapshot.json")
        packet = load_json(directory / "recommendation-input.json")
        identities = {
            job.get("recommendation_id"), request.get("recommendation_id"), context.get("recommendation_id"),
            snapshot.get("recommendation_id"), packet.get("recommendation_id"),
        }
        check("identity", identities == {recommendation_id})
        check("request_hash", request.get("request_sha256") == request_hash(request) == packet.get("request_sha256"))
        check("context_hash", context.get("context_sha256") == context_hash(context) == packet.get("context_sha256"))
        check("snapshot_hash", snapshot.get("snapshot_sha256") == object_hash(snapshot, "snapshot_sha256") == packet.get("reading_snapshot_sha256"))
        validate_input_fresh(directory, workspace, packet)
        check("prepared_sources", True)
        boundary_ok = context.get("review_status") == "confirmed"
        for item in context.get("selected_sources") or []:
            if item.get("requires_explicit_selection"):
                boundary_ok = boundary_ok and item.get("selection_basis") == "explicit-user-context-confirmation"
        check("context_boundary", boundary_ok)
        plan = load_json(directory / "discovery-plan.json")
        validate_discovery_plan(plan, request, context, snapshot)
        check("privacy_plan", True)
        result = load_json(directory / "result.json")
        validate_result(result, packet)
        check("three_routes", True)
        rendered = render_result(result, packet)
        check("report_render", (directory / "report.md").is_file() and (directory / "report.md").read_text(encoding="utf-8") == rendered)
        check("no_belief_promotion", all(not str(item.get("event") or "").startswith("belief-") for item in events))
    except (RecommendationFailure, ReadForMeFailure, OSError, KeyError, TypeError, ValueError) as exc:
        check("audit_exception", False)
        safe_error = str(exc)
    else:
        safe_error = None
    passed = not failures
    if passed and complete:
        job = load_json(directory / "job.json")
        save_job(directory, job, status="completed", now=now)
        append_event(
            directory,
            "book-recommendation-audit-passed",
            file_sha256(directory / "result.json"),
            {"checks": sorted(checks)},
            now or utc_now(),
        )
    return {
        "result": "passed" if passed else "failed",
        "recommendation_id": recommendation_id,
        "checks": checks,
        "failed_checks": failures,
        "safe_error": safe_error,
        "status": load_json(directory / "job.json").get("status") if (directory / "job.json").exists() else None,
    }


def status_packet(workspace: Path, recommendation_id: str) -> dict[str, Any]:
    directory = task_dir(workspace, recommendation_id)
    job = load_json(directory / "job.json")
    return {
        "result": "status",
        "recommendation_id": recommendation_id,
        "status": job.get("status"),
        "report_version": job.get("report_version"),
        "last_error": job.get("last_error"),
        "next_action": next_action(job),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    start = subparsers.add_parser("start")
    start.add_argument("--workspace", required=True, type=Path)
    start.add_argument("--goal", required=True)
    start.add_argument("--constraint", action="append", default=[])
    start.add_argument("--per-route", type=int, default=2)
    context = subparsers.add_parser("context")
    context.add_argument("--workspace", required=True, type=Path)
    context.add_argument("--recommendation-id", required=True)
    context.add_argument("--include", action="append", default=[])
    context.add_argument("--exclude", action="append", default=[])
    context.add_argument("--correct", action="append", default=[])
    context.add_argument("--confirm", action="store_true")
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--workspace", required=True, type=Path)
    prepare.add_argument("--recommendation-id", required=True)
    prepare.add_argument("--plan", required=True, type=Path)
    prepare.add_argument("--api-fixture", type=Path)
    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--workspace", required=True, type=Path)
    apply_parser.add_argument("--recommendation-id", required=True)
    apply_parser.add_argument("--result", required=True, type=Path)
    audit = subparsers.add_parser("audit")
    audit.add_argument("--workspace", required=True, type=Path)
    audit.add_argument("--recommendation-id", required=True)
    audit.add_argument("--no-complete", action="store_true")
    status = subparsers.add_parser("status")
    status.add_argument("--workspace", required=True, type=Path)
    status.add_argument("--recommendation-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_console()
    args = build_parser().parse_args(argv)
    try:
        if args.command == "start":
            result = start_recommendation(args.workspace, goal=args.goal, constraints=args.constraint, per_route=args.per_route)
        elif args.command == "context":
            result = review_context(
                args.workspace,
                args.recommendation_id,
                include=args.include,
                exclude=args.exclude,
                corrections=args.correct,
                confirm=args.confirm,
            )
        elif args.command == "prepare":
            result = prepare_candidates(
                args.workspace,
                args.recommendation_id,
                args.plan,
                api_fixture=args.api_fixture,
            )
        elif args.command == "apply":
            result = apply_result(args.workspace, args.recommendation_id, args.result)
        elif args.command == "audit":
            result = audit_task(args.workspace, args.recommendation_id, complete=not args.no_complete)
        elif args.command == "status":
            result = status_packet(args.workspace, args.recommendation_id)
        else:
            raise RecommendationFailure("unsupported command")
    except (RecommendationFailure, ReadForMeFailure) as exc:
        print(json.dumps({"result": "failed", "reason": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("result") != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
