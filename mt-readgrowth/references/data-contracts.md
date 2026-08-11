# MT-readgrowth runtime data contracts

## Contents

0. Project shell binding
1. Workspace configuration
2. Book catalog event
3. Book metadata
4. Chapter metadata
5. Paragraph record
6. WeRead shelf snapshot
7. Reading-system overview
8. WeRead sync state
9. Unified manual import result
10. Version confirmation queue
11. Current-book delta report
12. Evidence guide packet
13. Session metadata
14. Manual weekly report
15. User profile settings, event stream, and current views
16. Belief event stream and durable view
17. Shared personal task context
18. Read-for-me task and report
19. Personalized book recommendation
20. State rules

## Project shell binding

The optional project shell lives outside `reading-workspace/` and contains only a project-root `AGENTS.md` plus the child workspace. Its managed binding is distributed as `assets/project-shell/AGENTS.md` and created or audited only through `scripts/project_shell.py`.

The binding fixes the unique workspace to `./reading-workspace`, requires the host's user-level installed `mt-readgrowth` Skill for every new project task, and requires a binding audit before the first workspace read or write. It stores no user statement, book, session, profile item, belief, report, credential, cache, absolute author-machine path, or installed Skill copy.

The managed block may coexist with user-authored instructions outside the block. Initialization must refuse an existing unmanaged `AGENTS.md` or a modified managed block instead of overwriting it. Audit validates the exact block, fixed `workspace.yaml`, and standard non-escaping directories without enumerating or reading personal workspace content.

Automatic project association does not alter `memory_mode` and is never a profile or belief decision. Hosts that do not automatically load project-root `AGENTS.md` require a host-specific thin bridge or explicit per-conversation Skill selection.

## Workspace configuration

Store current configuration in `reading-workspace/workspace.yaml`:

```yaml
schema_version: 1
workspace_id: personal-reading
skill_name: mt-readgrowth
catalog: catalog/books.jsonl
books_dir: books
sessions_dir: sessions
weread_dir: integrations/weread
```

All paths are relative to the workspace root. Structured workspace data must not store secrets. The sole exception is the Git-ignored root file `.weread-api-key`, which contains exactly one non-empty UTF-8 line and is consumed only by live WeRead credential resolution.

Initialize a missing, empty, or not-yet-configured workspace with:

```text
<python-3.10+> <installed-skill-dir>/scripts/daily_reading.py init --workspace <reading-workspace>
```

Resolve `<installed-skill-dir>` from the directory containing `SKILL.md`. Use `python` on a configured Windows installation and commonly `python3` on macOS/Linux; do not assume the repository checkout path after user-level installation.

The initializer writes the fixed configuration above, creates these empty paths: `inbox/`, `integrations/weread/`, `catalog/`, `books/`, `sessions/`, `knowledge/beliefs/`, `knowledge/weekly/`, `knowledge/read-for-me/`, `knowledge/recommendations/`, `knowledge/user-profile/`, `cache/`, `logs/`, and `trash/`, and writes the `proactive-review` initialization setting defined below. It is idempotent, preserves an existing valid mode setting, creates no reading records, profile items, profile candidates, beliefs, or credentials, and must refuse conflicting configuration.

## Book catalog event

Append one JSON object per state-changing event to `catalog/books.jsonl`:

```json
{"event":"book-imported","at":"RFC3339","stable_book_id":"sha256-prefix","book_dir":"relative/path","source_sha256":"full-sha256","status":"source-imported"}
```

Use later events to update status; do not rewrite history to hide a previous state.

Keep provider-only shelf registration out of `catalog/books.jsonl`, because it has no source-backed `stable_book_id`. Its current source of truth is `integrations/weread/shelf.json`. If the user later imports a real EPUB/TXT, append the normal source-backed `book-imported` event.

## Book metadata

Store current book state in `books/<book-dir>/book.yaml` with:

- `schema_version`
- `stable_book_id`
- `title`, `authors`, `language`, `identifiers`
- `status`
- `quality_warning`: `embedded-watermark` for a registered warning, otherwise null
- `source.file_name`, `source.relative_path`, `source.media_type`, `source.size_bytes`, `source.sha256`, `source.imported_at`
- `parser.name`, `parser.version`, `parser.parsed_at`
- `weread.book_id`, `weread.mapping_status`, `weread.edition_status`, `weread.confirmed_at`, `weread.confirmation_basis`

Allow `weread` fields to be null before mapping.

For TXT, use `source/original.txt` and `text/plain`. Also record `parser.source_format: txt`, strict-decoding `encoding`, `encoding_confidence`, `chapter_strategy`, `streaming`, `max_chapter_chars`, and `max_chapter_paragraphs`. For EPUB, retain `parser.opf_path`. Never store a lossy replacement-decoded TXT as display text.

Integrity audits resolve the preserved source only from `source.relative_path`. The allowed pairs are `source/original.epub` with `application/epub+zip`, and `source/original.txt` with `text/plain` plus `parser.source_format: txt`. Reject absolute paths, `..`, alternate filenames, media-type mismatches, and any symlink or Junction whose resolved target leaves the book directory before reading or hashing the source.

Use `schema_version: 2` for newly imported books. `status: text-ready` means only that the preserved source and parsed display/search text passed import integrity checks. Store thought-layer readiness separately in `derived/analysis-state.json`; see `analysis-contracts.md`.

When `quality_warning: embedded-watermark`, preserve source and parsed records unchanged and require `derived/source-quality.json`. That registry stores a project-registered deterministic literal-deletion rule with its registration basis, every affected locator, original and cleaned hashes, usable/skipped status, and skip reasons. It must not inherit or claim another user's approval. Broad tokens such as `EPUB...` are ordinary content and do not create a warning. The registry is rebuildable analysis metadata, not a replacement source.

## Chapter metadata

Store `parsed/toc.json` as an object containing `schema_version`, `stable_book_id`, and ordered `chapters`. Each chapter includes:

- `chapter_id`: `ch-001`, `ch-002`, and so on for readable citation
- `source_href`: canonical EPUB manifest path, or a deterministic TXT source-line/segment reference retained under this compatibility field
- `source_href_sha256`: protects identity across reparses
- `spine_index`
- `title`
- `paragraph_count`
- `content_sha256`

For EPUB, the spine controls reading order and the ZIP member order does not. For TXT, detected headings or explicitly confirmed fixed segments control reading order; record source line bounds in each chapter.

## Paragraph record

Store one JSON object per display paragraph in `parsed/paragraphs.jsonl`:

```json
{"schema_version":1,"stable_book_id":"f03c...","locator":"f03c...#ch-001:p-0001","chapter_id":"ch-001","chapter_title":"...","spine_index":0,"paragraph_index":1,"text":"display text","text_sha256":"...","source_href_sha256":"..."}
```

The readable locator is not the sole identity. Verify `text_sha256`, `source_href_sha256`, source file hash, and parser version when exact stability matters.

Store the search copy separately in `search/normalized.jsonl` with `locator`, `normalized_text`, and the same hashes. Never quote normalized text as original text.

## WeRead shelf snapshot

Store the last valid normalized shelf in `integrations/weread/shelf.json`:

- `schema_version`, `provider`, `provider_skill_version`, `synced_at`
- `scope.api_calls`: exactly `[/shelf/sync]` for V1 node 1
- `scope.full_text_fetched: false`, `scope.notes_fetched: false`
- `counts.visible_entries`, `counts.books`, `counts.albums`, `counts.mp`
- `entries[]` in provider shelf order

Every entry has an immutable provider identity independent of local source hashes:

- electronic book: `weread:book:<bookId>`
- album/audiobook: `weread:album:<albumId>`
- article collection entry: `weread:mp:collection`

Also retain the provider item type, ID, title, author, safe deep link when returned, top/private flags, completion flag, and provider timestamps needed for the overview. Reject a response with missing or duplicate provider identities instead of registering ambiguous rows.

Do not store raw authorization data. Do not create a `books/<book-dir>` directory or synthesize `stable_book_id`/`source_sha256` for a shelf-only entry.

## Reading-system overview

Store the current combined machine view in `catalog/overview.json` and render the user-facing view to `catalog/coverage.md`. The overview references the exact SHA-256 of the normalized shelf snapshot and includes both provider entries and local-only books.

Keep these dimensions separate per entry:

- `registration_status`: `registered` or `local-only`
- `text_status`: the allowed book text state used for routing
- `local_text_status`: nullable; the actual local state when a candidate or confirmed local book exists
- `analysis_status`: nullable for non-book entries; otherwise the independent thought-layer state
- `link_status`: `confirmed`, `needs-confirmation`, `unlinked`, `mismatch`, or `not-applicable`
- `stable_book_id`: nullable; a real local source identity only
- `link_basis`: `book-yaml-weread-id`, `exact-title-author`, `base-title-author`, or null
- `discussion_available`: true only for a confirmed link whose local text is `text-ready` and thought layer is `discussion-ready`
- `next_action`: deterministic routing text; never an assertion that a pending edition is confirmed

A confirmed WeRead bookId already stored in local `book.yaml` may produce `link_status: confirmed`. A unique normalized title/author match may attach the real local `stable_book_id` as a candidate, but must produce `text_status: needs-confirmation` and `link_status: needs-confirmation`. This allows the overview to show that local full text exists without allowing that text to stand in for an unconfirmed WeRead edition.

Summary counts must include the WeRead visible-entry formula, status distributions, total unique local books, unique local books actually linked to a provider entry, unique local full-text books, thought-layer-ready books, and currently discussion-available books. A local-only book must not inflate `linked_local_books`. Repeated sync replaces current snapshots atomically and must not create duplicate provider identities or append duplicate registrations.

## WeRead sync state

Store current synchronization state in `integrations/weread/sync-state.yaml`. V1 node 1 upgrades it to `schema_version: 2` while preserving existing per-book V0 note/progress fields:

- `schema_version`
- `provider`
- `provider_skill_version`
- `last_shelf_sync_at`
- `last_shelf_snapshot_sha256`
- `shelf_entry_count`
- `books.<stable_book_id>.weread_book_id`
- `books.<stable_book_id>.last_notes_sync_at`
- `books.<stable_book_id>.last_progress_sync_at`
- `books.<stable_book_id>.cursor`
- `last_error.code`, `last_error.at`, `last_error.safe_message`

Never store API keys, authorization headers, or raw secret-bearing errors in synchronization state. The optional root `.weread-api-key` remains separate from this contract and must never be copied into state or reports.

On a shelf-interface failure, leave `shelf.json`, `overview.json`, and `coverage.md` unchanged. Update only `last_error` with a controlled code, timestamp, and safe message. A successful later shelf sync clears `last_error`.

## Unified manual import result

Use `scripts/daily_reading.py import` as the V1 node 2 entry for both EPUB and TXT. It delegates to the existing format-specific importers and does not weaken their safety gates.

The result adds `flow_version`, `source_format`, the underlying importer result, the rebuilt `version_queue`, and a deterministic `next_action`. For ambiguous TXT encoding or missing chapter headings, return `result: needs-confirmation`, `stage: txt-inspection`, and the original inspection packet without creating a book directory. Never turn `needs-confirmation` into an implicit import.

## Version confirmation queue

Store the current materialized queue in `integrations/weread/version-confirmations.json`. Include the exact saved overview source metadata, `pending[]`, and `resolved[]`.

Each candidate uses a stable `candidate_id` derived from provider identity plus the real local `stable_book_id`. Retain provider identity, WeRead bookId/title/author, local book path, link basis, and explicit evidence that title/author agreement does not prove edition identity.

Append every user decision to `integrations/weread/version-decisions.jsonl` as `weread-version-decision`. Record `confirm` or `reject`, timestamp, both identities, and `basis: explicit-user-decision`. Never rewrite prior decisions. Confirmation updates `book.yaml` to `mapping_status: confirmed`, `edition_status: confirmed`, and `confirmation_basis: user-confirmed-version-queue`; rejection does not mutate source identity or pretend an edition mismatch has been technically proven.

Do not synchronize notes for a title/author-only candidate. `/book/info` may establish an unambiguous exact ISBN match; otherwise stop before progress, chapter, highlight, or review calls and require an explicit queue decision.

## Current-book delta report

Store the latest report for each confirmed current book at `integrations/weread/reports/<stable-book-id>.json`. Raw notes and match decisions remain append-only in their existing JSONL files; the report is a replaceable derived view.

The report contains the exact current-book API scope, previous/current progress, fetched/new/unchanged note counts, new highlight/review counts, match statuses for newly appended events, unique discussion routes, ambiguous or not-found items, and `idempotent: true` when a repeat fetch appends nothing.

Every discussion route includes the local locator, chapter, whether `prepare-chapter` is required, and the required sequence before `build_context.py`. Do not silently select one candidate from an ambiguous match. Preserve the node 1 `schema_version: 2` shelf fields and other books in `sync-state.yaml`; update only the selected book block.

## Evidence guide packet

`scripts/daily_reading.py guide` returns a replaceable response packet; it does not write a new truth source. It requires `discussion-ready`, `overview-result.json`, `book-map.md`, and `author-lens.md`.

The packet contains an introduction route, author lenses, ranked chapter recommendations with reasons and resolvable citations, `generated` or `overview-only` chapter status, existing validated chapter claims when available, and the overview scope limits. A topic score is only AI routing assistance. It is not an author statement, user belief, or proof that the recommended chapter is best. Overview sampling must not be described as full-chapter evidence.

## Session metadata

Store `sessions/<year>/<session-id>/session.yaml` with:

- `schema_version`
- `session_id`
- `started_at`, `ended_at`
- `stable_book_ids`
- `focus_locators`
- `weread_highlight_ids`
- `evidence_mode`: `full`, `restricted`, or `none`
- `summary_status`: `pending`, `generated`, `confirmed`, or `failed`

Append raw messages and tool events to `transcript.jsonl`. Generate `summary.md` from raw records without modifying them.

The session-summary JSON keeps the existing required fields and accepts optional `profile_candidates`. A missing field means an empty list, including for historical summaries. Accept at most three candidates. Each candidate contains `category`, `content`, `usage_scopes`, optional `sensitivity`/`expires_at`, and `source_event_id`; the source event must be a live `role=user`, `event_type=message` event in that same session. Assistant, tool, and system events cannot be profile sources.

`scripts/record_session.py summarize` returns `summary_path` plus a `profile_review` packet. Under `proactive-review`, validate all candidates before replacing the summary, materialize them through `profile_store.py`, and return stable profile item IDs for a batch save/revise/ignore review. The packet itself sets `authorization_granted: false`; it is not a save decision. Its decision map uses save → `confirm`, revise → `revise`, and an explicitly targeted ignore → `reject`. Apply a user response only to the stable IDs it identifies: “only save item 2” confirms item 2 and leaves every unmentioned item in `candidate`; it does not reject item 1. Under `explicit-only` or `off`, do not validate or materialize supplied automatic candidates and return the deterministic suppression basis. Candidate materialization never confirms a profile item and never writes `belief-events.jsonl`.

Every session write must be bound to an explicit resolved workspace. `append` and `summarize` require both `--workspace <path>` and `--session-dir <path>`. Before opening or replacing any file, resolve and validate that the session is exactly under `workspace/sessions/<year>/<session-id>`, that its metadata identity and year agree with the directory, and that no `..`, symbolic link, Junction, or linked output file escapes the workspace boundary. Validate the complete `session.yaml` and all summary output paths before creating `summary.md` or changing session status.

For a discussion, run `scripts/record_session.py context` first as a read-only preview. A later call with `--confirm` atomically writes `sessions/<year>/<session-id>/context.json`. The frozen object records the session ID, effective memory mode, profile/belief/history source snapshots, source-bundle hash, available/selected/excluded sources, exact include/exclude decisions, a content hash, and the immutable evidence policy. Historical session statements, sensitive profiles, and candidate/provisional beliefs remain unselected unless their stable IDs are explicitly included. Profile candidates are never available.

Validate both `context.json` and `context.json.tmp` before writing. Reject an external or `..` session path, a session symlink/Junction, and any linked or escaping output path without leaving a partial context file. Run `scripts/build_context.py --workspace <workspace> --book-dir <book-dir> --session-dir <session-dir> --locator <locator>`; it must validate the frozen context and source-bundle hashes, then copy only `selected_sources` into the discussion generation packet. Unselected `available_sources` remain review material and are not generation input.

## Manual weekly report

Run `scripts/weekly_review.py report --workspace <path> --week YYYY-Www` only for a week the user requests. Interpret the ISO week in the supplied fixed timezone, defaulting to `+08:00`. Do not schedule or trigger reports automatically in V1.

Write the replaceable machine report to `knowledge/weekly/<week>/report.json` and the user-facing rendering to `report.md`. Include:

- exact local and UTC-exclusive period bounds;
- book-import and WeRead note events whose own timestamps fall inside the period;
- completed saved discussions and separately counted `discussion-skipped-by-user` sessions;
- `作者明确表达` as content facts, with session-summary sources;
- `用户明确确认` separately from product acceptance and from durable beliefs;
- `作者立场推断` and `AI延伸应用` under an explicit AI-inference tier;
- stable candidate IDs, unresolved questions, and next-reading proposals;
- explicit evidence gaps when any category has no data;
- a source manifest containing relative paths and SHA-256 hashes.

The report may materialize missing `belief-candidate-recorded` events idempotently, but it must not create a durable belief or make any user decision. A skipped session must not count as a completed discussion. A missing progress-event history must remain an evidence gap rather than being reconstructed from a current snapshot.

A session with `ended_at: null`, or with `summary_status` outside `generated / confirmed`, is still in progress. Preserve its metadata and transcript unchanged, but exclude it from weekly behavior facts, summary evidence, source manifests, and candidate-belief materialization until it is finalized.

### Current V1 limits for future V3 visualization

The V1 schema is the accepted source for the existing manual weekly report, but it does not yet prove a complete list of books read during the period and does not expose user reflections as a separate semantic tier.

- A `book-imported` event proves import, not reading.
- A current shelf/progress snapshot cannot reconstruct historical progress for the report period.
- WeRead notes and user-authored session content must not be merged into author content, AI inference, or durable beliefs.
- Future V3 work may add a rebuildable `user_reflections` tier and period-activity evidence by versioning this existing report schema. Preserve all V1 fields, the source manifest, and audit guarantees; do not create a parallel report truth source.
- Until that extension is implemented and audited, a visualizer must label incomplete book coverage as “本周期有证据涉及” and must not claim that every user thought is a confirmed belief.
- The planned durable visual view is `knowledge/weekly/<week>/visual-report.html`. It is derived from the versioned machine report, remains local by default, and must not embed credentials or long book quotations.

## User profile settings, event stream, and current views

Store the user-profile capability under `knowledge/user-profile/`. Workspace initialization creates the directory and a non-user-authored `settings.json` default. It must not create a profile item, candidate, event stream, current profile view, or belief.

For a new workspace, `settings.json` is:

```json
{
  "schema_version": 1,
  "memory_mode": "proactive-review",
  "configured_at": "RFC3339",
  "decision_basis": "workspace-initialization-default"
}
```

This setting contains no `user_statement` because it is not a user decision. A later explicit mode choice replaces the file with:

```json
{
  "schema_version": 1,
  "memory_mode": "explicit-only",
  "configured_at": "RFC3339",
  "decision_basis": "explicit-user-decision",
  "user_statement": "the user's exact decision wording"
}
```

Allow only `proactive-review`, `explicit-only`, and `off`. Initialization may write only `proactive-review` with `decision_basis: workspace-initialization-default`. Every later mode choice requires a non-empty current user statement and uses `decision_basis: explicit-user-decision`. Explicitly choosing `proactive-review` while the initialization default is active must replace the initialization basis and retain the user's wording; repeating an already explicit selection is idempotent. A missing file in a legacy workspace still has the effective behavior `explicit-only` but remains unconfigured, and read-only consumers must not materialize it.

Store the immutable lifecycle source at `knowledge/user-profile/profile-events.jsonl`. A candidate identity is deterministic from the exact category, candidate content, and source session-user event. Its registration event records:

- a retry-stable `event_id` and `profile_item_id`;
- `from_status: null` and `to_status: candidate`;
- one allowed category: `background`, `occupation`, `goals`, `interests`, `reading_preferences`, `communication_preferences`, `constraints`, or `traits`;
- exact candidate content and `content_sha256`;
- `evidence_type: explicit-user-statement`;
- one or more scopes from `discussion`, `read-for-me`, and `recommendation`;
- `sensitivity: normal|sensitive` and an optional RFC3339 `expires_at`;
- a validated `sessions/<year>/<session-id>/transcript.jsonl` source whose event exists with `role=user`;
- `decision_basis: session-summary-profile-candidate` for a reviewed extraction, or `explicit-user-request` when an explicit remember request creates the candidate half of a combined save.

Every profile decision requires the user's exact non-empty wording and uses `decision_basis: explicit-user-decision`. Apply these transitions:

- `candidate -> confirmed` for `confirm`;
- `candidate -> revised` for an explicitly corrected candidate;
- `candidate -> rejected`, terminal;
- `confirmed|revised -> revised` for later corrections;
- `confirmed|revised -> retired`, terminal.

An explicit “remember this” operation may append the deterministic candidate and confirmation events in one command. Retrying the same source and decision must not append duplicates. Revisions never overwrite old content. Reject secret-bearing content or user-decision wording before writing and return only a controlled error that does not echo the secret.

Rebuild `profile.json` and `profile.md` atomically from the event stream. Both views contain only current `confirmed` and `revised` items; `candidate`, `rejected`, and `retired` remain only in the event stream and management output. `profile.json` records the exact event-stream SHA-256 and each active item's current event, content hash, source, scopes, sensitivity, expiry, and update time. `profile.md` is a deterministic readable rendering of the same membership.

Run `scripts/profile_store.py audit --workspace <path>` after decisions. Require valid settings when present, unique deterministic event IDs, valid hashes and transitions, live user-message sources, no stored secret-bearing text, exact active-view membership, and byte-current JSON/Markdown renderings. `rebuild` repairs only the replaceable views; it never rewrites `profile-events.jsonl`.

Phase A provides this storage and audit layer. Phase B makes eligible confirmed profile items available to read-for-me and recommendation previews through the shared assembler; task context confirmation remains required. Phase C adds the same assembler to discussion previews/frozen contexts and applies the three memory modes to optional end-of-session profile candidates. Phase D keeps profile and belief stores parallel: profile decisions stay in `profile_store.py`, while long-term belief events, state transitions, durable views, and belief audit are owned by `belief_store.py`.

## Belief event stream and durable view

Store the immutable lifecycle source at `knowledge/belief-events.jsonl`. A candidate discovered in a session summary receives a deterministic `candidate_id` from the session ID, candidate index, and exact candidate text. Its registration event records:

- `event_id`, `event`, `at`, `candidate_id`, `belief_id`;
- `from_status: null`, `to_status: candidate`;
- exact content and `content_sha256`;
- source session, summary path, candidate index, and optional locator;
- `decision_basis: session-summary-candidate`.

Require `--user-statement` for every decision event and store the user's exact explicit wording. Use `decision_basis: explicit-user-decision`. Apply these transitions:

- first `confirm`: `candidate -> provisional`;
- second `confirm`: `provisional -> confirmed`;
- `revise` from `candidate`: `candidate -> provisional` using the user-supplied revision;
- `revise` from `provisional`, `confirmed`, or `revised`: move to or remain `revised` and append the new wording;
- `reject`: `candidate -> rejected`, terminal;
- `retire`: `provisional`, `confirmed`, or `revised` to `retired`, terminal.

Create or rebuild `knowledge/beliefs/<belief-id>.md` only for `provisional`, `confirmed`, `revised`, or `retired`. The Markdown file is a current, replaceable view of the append-only events and must include status, current content hash, candidate source, user statements, transitions, and event IDs. Never create one for `candidate` or `rejected`.

`scripts/belief_store.py` owns candidate-event validation, state reduction, explicit decisions, durable-view rendering, and the belief-only audit. `scripts/weekly_review.py candidates / decide / audit` remains the compatible user-facing CLI: it discovers candidates from finalized summaries, delegates belief operations, and combines the belief audit with weekly-report checks. Run `scripts/weekly_review.py audit` after decisions. Require unique deterministic event IDs, valid content hashes and transitions, exact durable-view membership, current rendered content, and proof that unconfirmed/rejected candidates were not promoted.

A repeated retry of the same belief decision wording is idempotent and cannot stand in for a second independent confirmation. Profile confirmation, recommendation feedback, read-for-me feedback, task-context confirmation, report acceptance, and other workflow acceptance remain in their own event streams; none may append a belief decision or advance a belief state.

## Shared personal task context

Use the read-only `scripts/personal_context.py` assembler for every personalized task route. It must not create a missing setting, event stream, profile view, session, or task file. It reads profile state from `profile-events.jsonl`, never from `profile.json` as the sole truth; it reuses the belief-state reducer in `belief_store.py` for `belief-events.jsonl` and keeps historical user statements disabled by default. Consumers must not implement their own profile or belief lifecycle decisions.

The assembler accepts exactly `discussion`, `read-for-me`, or `recommendation` as its route. A source packet contains:

- the effective memory mode and `memory_mode_configured`, which reports whether a persisted settings file exists; this boolean is true for the initialization default and does not by itself mean the user explicitly selected the mode—the authoritative basis remains `settings.json.decision_basis`;
- the exact SHA-256 snapshots for profile settings, profile events, belief events, and any historical transcripts read;
- `available_sources`, including current-task statements, eligible profile items, usable belief states, and safe historical user statements;
- `excluded_sources`, with deterministic reasons such as inactive profile status, scope mismatch, expiry, unusable belief status, or explicit-selection requirement;
- a semantic `source_bundle_sha256` that excludes the assembly timestamp.

When memory mode is `off`, do not open or validate `profile-events.jsonl`; report that profile events were not loaded and return no profile sources. New workspaces load the persisted `proactive-review` initialization default as configured. Missing settings in a legacy workspace still behave as unconfigured `explicit-only` without a read-time write. Profile events must still pass their event/hash/schema checks and point to a live `role=user`, `event_type=message` transcript event. Only active `confirmed` or `revised`, in-scope, unexpired profile items are eligible. Normal items are defaults after task context confirmation; sensitive items require explicit per-task selection. Candidate, rejected, retired, expired, or scope-mismatched profile items cannot be selected.

For beliefs, `confirmed` and `revised` are defaults after task context confirmation; `candidate` and `provisional` require explicit per-task selection; `rejected` and `retired` are excluded. Historical session user statements require explicit selection. Assistant/tool/system text, workflow acceptance, process status, and secret-bearing text are never personal context.

Each consumer freezes the packet's snapshots, available sources, selected sources, excluded sources, and exact selection/exclusion bases in its own `context.json`. Before `prepare`, read-for-me and recommendation compare the persistent profile settings, profile events and belief events with the frozen hashes. If they changed, stop and require a new context preview and confirmation. Discussion has no prepare state: `build_context.py --session-dir` validates the frozen context and passes only its selected sources, so later profile changes do not silently alter that saved session. Downstream generation never reads a mutable profile view in place of the frozen packet.

For all discussion packets, preserve `required_labels: [作者明确表达, 作者立场推断, AI延伸应用]` and `personal_context_cannot_change_evidence_classification: true`. Selected profile sources may affect only explanation angle, examples, and application mapping. They cannot turn an inference or AI application into an author-explicit claim.

## Read-for-me task and report

Store each V2 node-1 task under `knowledge/read-for-me/<report-id>/`. The directory contains:

- append-only `events.jsonl` as the lifecycle source;
- rebuildable `job.json`, `reading-brief.json`, `context.json`, and `report-input.json`;
- `source-manifest.json` with workspace-relative source paths and SHA-256 hashes;
- validated `result.json` and deterministically rendered `report.md` after generation succeeds;
- prior valid report revisions under `versions/` when a completed task is explicitly regenerated.

`reading-brief.json` records the user's current reason for reading, core question, and desired outcome. Missing reason or core question keeps the task at `needs-brief`; an unknown desired outcome is an explicit evidence gap, not a model-invented goal.

`context.json` separates available sources from sources approved for this report and retains the shared source snapshots and exclusions. The current brief is directly usable. In-scope, active, normal user-profile items and `confirmed` or `revised` beliefs may be selected by default only after the preview is confirmed. Sensitive profile items, `provisional` beliefs, belief candidates, historical session statements, and AI inferences are disabled by default and require explicit per-report inclusion. Profile candidates are not eligible. Every selected source retains its status, source event or session pointer, and selection basis. Product acceptance, flow confirmation, skipped-discussion wording, credentials, and secret-bearing text are not personal reading context.

`report-input.json` is a compact evidence packet. It embeds the validated overview result, only the specifically requested existing chapter results, selected personal context, and the display paragraphs referenced by those results. It records missing requested chapters as a routing gate to the existing `derive_book.py prepare-chapter -> apply-chapter -> audit` flow; it never schedules unrelated chapters or requires 100 percent coverage. Registered cleaning rules and skipped evidence are inherited from `source-quality.json` and disclosed without modifying display text.

`result.json` uses `schema_version: 1` and includes `report_id`, `stable_book_id`, generator identity, the exact reading-brief hash, selected context sources with evidence status, `book_overview`, `book_route`, `core_ideas`, `personalized_answers`, `agreements_and_challenges`, `actions`, `reading_path`, `additional_relevant_questions`, `evidence_scope`, `scope_limits`, `evidence_gaps`, and `source_manifest_sha256`.

Every personalized answer must use `AI延伸应用`, cite allowed book locators, cite at least one selected user-context source, and explain the mapping. Core book ideas use only `作者明确表达` or `作者立场推断`. Reject stale input hashes, wrong book/report identities, unresolved or out-of-packet locators, unapproved context IDs, and claims of reading every word/chapter or complete chapter coverage.

Use lifecycle states `needs-brief -> context-review -> evidence-ready -> report-ready -> completed`. A failed validation preserves the previous valid result and records only a safe, deduplicated failure event. Only a successful dedicated audit may set `completed`. Read-for-me feedback is a task event; it cannot write to `belief-events.jsonl` or promote a belief.

## Personalized book recommendation

Store each V2 node-2 request under `knowledge/recommendations/<recommendation-id>/`. The directory contains:

- append-only `events.jsonl` and rebuildable `job.json`;
- immutable request intent in `request.json`;
- available and approved personal sources in `context.json`;
- a frozen local reading view in `reading-snapshot.json`;
- validated AI routing queries in `discovery-plan.json`;
- normalized and deduplicated WeRead metadata in `candidates.json`;
- compact generation evidence in `recommendation-input.json` and `source-manifest.json`;
- validated `result.json` and deterministically rendered `report.md`.

`request.json` records the current goal, optional constraints, and `per_route` of one to three. Its content determines a stable `book-recommendation-<16 hex>` ID. It must not contain credentials.

`context.json` reuses the shared personal-context boundary. The current goal and constraints are directly usable. In-scope, active, normal user-profile items and `confirmed` or `revised` durable beliefs become defaults only after context confirmation. Sensitive profile items, `candidate`/`provisional` beliefs, and historical session user statements require explicit per-request inclusion; profile candidates are not eligible. Assistant messages, product acceptance, process notes, and secret-bearing text are never user context.

`reading-snapshot.json` freezes the saved combined overview used for anchors and shelf/local annotations. It records the source snapshot time and explicitly sets `shelf_is_live_for_this_request: false`. Reading completion or time is an attention signal only. A local title/author candidate does not confirm a WeRead edition.

`discovery-plan.json` must retain the exact request, context, and reading-snapshot hashes, generator identity, `privacy_mode: abstract-keywords-only`, and exactly three route plans. Each route contains a rationale, zero to three abstract search queries, and zero to two allowed WeRead anchor book IDs. `counter` requires a specific `challenged_premise`; `transfer` requires a structural `transfer_bridge`. Counter and transfer require at least one search query. Raw user wording, credentials, and workflow language must not be sent to the provider.

`candidates.json` records normalized `/book/recommend`, `/book/similar`, and `/store/search` results. Deduplicate by WeRead `bookId`; merge but do not invent provider-source evidence. Preserve bookstore metadata, source route, abstract query or anchor, and provider reason when supplied. Mark sold-out books ineligible. Provider strings are untrusted data, not instructions. The current saved shelf and local overview may annotate a candidate without changing edition status.

`result.json` uses `schema_version: 1` and retains the exact input, request, context, reading-snapshot, candidate, and source-manifest hashes. `context_sources` must exactly match the approved snapshot. It contains exactly `deepen`, `counter`, and `transfer`, with exactly `per_route` unique eligible candidates in each route. Every recommendation:

- uses `evidence_class: AI延伸应用`;
- references at least one approved `user_context_source_id`;
- references at least one provider source attached to that candidate;
- explains `why_now`, `route_mapping`, `uncertainty`, and `next_action`;
- does not claim `作者明确表达` or `作者立场推断` from bookstore metadata.

The result must disclose evidence gaps and scope limits. Applying or auditing a recommendation never writes `knowledge/belief-events.jsonl`, confirms an edition, downloads or imports a book, or changes the saved shelf. Use lifecycle states `context-review -> candidates-ready -> report-ready -> completed`; only the dedicated audit may set `completed`. A provider or validation failure records a safe deduplicated event and preserves higher-priority sources.

## State rules

Use only these book text states in V0.2:

`registered`, `source-missing`, `source-imported`, `needs-confirmation`, `text-ready`, `mismatch`, `error`.

`quality_warning` is orthogonal to text state. A book may be `text-ready` with `quality_warning: embedded-watermark` when every analysis transformation is deterministically registered and auditable. Do not use `error` solely because a registered removable watermark signature exists.

Migrate the former `ready` state to `text-ready` without reimporting or rewriting source/parsed data. Thought-layer state is independent: `not-started`, `partial`, `discussion-ready`, or `failed`. `discussion-ready` requires validated `book-map.md` and `author-lens.md`; it does not require 100% chapter coverage. Record gradual chapter coverage explicitly.

Use only these highlight match states:

`exact`, `high-confidence`, `ambiguous`, `not-found`, `edition-mismatch`.

When a short highlight has multiple candidates in one chapter, a uniquely located longer note abstract plus both WeRead `range` values may narrow candidates by before/after order. Do not assume WeRead numeric offsets equal local parsed character offsets.

Use this belief lifecycle:

`candidate -> provisional -> confirmed -> revised -> retired`, with `rejected` as a terminal branch. Require explicit user confirmation before leaving `candidate`.
