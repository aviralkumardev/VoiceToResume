# Backend: Completeness Grading & Interview Mode

## Purpose
Judges how *complete* the live-extracted resume actually is, against a
static coverage rubric, rather than just whether a field has any value.

Two independent consumers of the same rubric:
1. **The batched grading worker** (`silence_completeness_worker.py`, an
   orchestrator-level task) — re-grades the whole resume against the rubric
   whenever the candidate goes silent, or right after any extraction batch
   that changed something (see
   [backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md)'s
   "Second trigger"). This is what catches information the candidate
   volunteers *unprompted* in free conversation, and is the **only** writer
   of `field_completeness` left in the codebase.
2. **The interview director** (`stt_tts_pipeline/interview_director.py` —
   see [backend/stt-tts-pipeline.md](stt-tts-pipeline.md)) — drives the
   actual round-based Q&A. It no longer writes any verdict itself, but it
   DOES now compute target *selection* deterministically in Python: one
   fused LLM call per answer (`question_chain.run_question_chain`, living in
   [backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md)
   despite the name of this file) grades the just-given answer AND drafts
   both a same-topic probe and a next question, but the next question's
   *subject* must be picked from a Python-computed, exhaustive,
   priority-ordered candidate list (`next_target.compute_next_targets`) —
   the model reasons over the whole resume/conversation to decide which
   candidate is still open and how to word the question, not which block
   matters most. The director reads `field_completeness` on every single
   turn now (to build that candidate list), not just once at a safety net —
   see "The interview loop" below.

This is a substantially simplified replacement for an earlier, much larger
design (per-target selection, a next-target shortlist, field-group question
batching, an async pending-BLOCK-claim verification ledger). That entire
mechanism is gone; see "Last synced" for what it used to be, if archaeology
is ever needed — the design docs under `docs/` may still have more color.

## Key files
- `.../config_jsons_definitions/coverage_schema.py` — `COVERAGE_SCHEMA`,
  unchanged: per-block/per-field `importance`
  (`required`/`recommended`/`optional`) + a natural-language `complete_when`
  bar, a per-block `objective_priority` (used by `next_target.py`'s
  ordering — see below), and an optional `not_applicable: true` flag (a
  flagged block/field is placed straight into `already_decided` as
  `NOT_APPLICABLE` by `prune_for_judgment`, never sent to the LLM). `personal`
  and `summary` are both `required` importance but ALSO `not_applicable:
  true` in this schema, so in practice they never surface as an open
  required gap — the only required blocks that ever can are `experience`,
  `education`, `skills` (verified by `objective_priority` ordering).
- `.../completeness_status.py` — trimmed to the pure, no-I/O grading core
  the batched worker still needs: `STATUS_MISSING`, `STATUS_PARTIAL`,
  `STATUS_SUFFICIENT`, `STATUS_NOT_APPLICABLE`, `STATUS_UNABLE_TO_ANSWER`,
  `TERMINAL_STATUSES`, `prune_for_judgment()`, `merge_completeness()`,
  `merge_status_preserving_terminal()`, and their private helpers
  (`_is_not_applicable`, `_scalar_value`, `_array_value`, `_is_sufficient`,
  `_leaf_verdict`, `_carry_unavailable`, `_leaf_status`, the `_prune_*` /
  `_merge_*` family). Everything that used to support target
  selection/shortlisting/field-group batching/special CONFLICT-UNRESOLVED
  targets (`select_focus_target`, `build_next_target_shortlist`,
  `open_targets`, `target_status`, `set_target_verdict`, `target_context`,
  `is_special_target`, the field-group helpers, etc.) has been deleted —
  ~800 lines removed, verified grep-clean of any remaining reference
  anywhere in the codebase. That entire responsibility no longer exists:
  the interview director trusts the LLM's own read of the resume+rubric
  directly instead of Python pre-selecting a path to ask about.
- `.../completeness_prompts.py` + `.../completeness_chain.py` —
  `run_completeness_chain()`, the batched whole-resume grading call
  (`(to_judge, coverage) -> {reasoning, blocks, _llm_usage}`), used
  exclusively by the silence worker's volunteered-info sweep. Unchanged.
- `.../silence_completeness_worker.py` — `run_silence_completeness_worker()`
  / `run_completeness_grading_cycle()`. Unchanged.

The per-answer grading chain (Task B) and the deterministic target scanner
are new modules physically living in `resume_analysis_pipeline/` —
`question_chain.py`, `question_prompts.py`, `next_target.py` — even though
conceptually they're "completeness" work; see
[backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md) for
their full documentation. The round-based state machine that calls them
(`_open_round`/`_probe_round`/`_advance_round`/`_pick_forced_topic`/
`_organic_targets_given_up`/`_forced_topics_spent`) is documented in
[backend/stt-tts-pipeline.md](stt-tts-pipeline.md).

## Public surface
- `prune_for_judgment(resume, coverage, previous_status) -> (already_decided, to_judge)`
  — partitions every leaf three ways: **MISSING** (no value right now —
  decided by code, never sent to the LLM), **CARRIED** (has a value,
  previous verdict already `SUFFICIENT` — carried forward verbatim),
  **TO_JUDGE** (has a value, previous verdict `PARTIAL` or none). A block's
  own aggregate verdict is included in `to_judge` whenever anything under it
  is `TO_JUDGE` or its own last verdict wasn't `SUFFICIENT`; a fully empty
  or fully-settled block short-circuits into `already_decided`.
- `merge_completeness(already_decided, llm_blocks, coverage) -> dict` —
  stitches carried-forward parts back together with a fresh LLM response.
  Safe to call with empty `llm_blocks`, and **shape-tolerant** of a
  malformed one (see the response-schema gotcha): a block, an item or a
  verdict leaf that isn't a dict is skipped, never indexed.
- `merge_status_preserving_terminal(existing, incoming) -> dict` — the
  *second* merge, one layer down: folds a freshly computed
  `field_completeness` onto the stored one, walking blocks/fields/item
  fields, and holding back any leaf whose existing verdict is terminal and
  whose incoming one is not. Pure, mutates neither argument. Called only by
  `crud.apply_field_completeness` — this is now the **sole** writer of
  `field_completeness` anywhere in the codebase (the old per-answer verdict
  write, `apply_answer_verdict`, is deleted along with the target model it
  served).
- `build_unable_to_answer_patch(field_completeness, target) -> dict` — the
  one write path around the batched grader's structural blind spot: a
  candidate's verbal decline ("I have no personal projects") leaves no
  `resume_data` value, so `prune_for_judgment` has nothing to ever detect,
  no matter how many grading cycles run. `target` is `{"block", "item_id",
  "fields"}` (`item_id` optional, `fields` an optional *list* of field
  names — plural, since one declined question commonly covers several
  fields of the same item at once, see "consolidate, don't drip-feed" in
  [backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md)) —
  `InterviewDirector` builds this from the round's own stored `target` (see
  [backend/stt-tts-pipeline.md](stt-tts-pipeline.md)) whenever a round
  closes `UNABLE_TO_ANSWER`. Copies whatever's already stored for `block`
  forward and overwrites every leaf `target` identifies (whole-block
  decline if `item_id`/`fields` are both absent/empty, else every named
  field written into the item's `fields` entry or the block's own `fields`
  entry) — never builds a sparse new node, since
  `merge_status_preserving_terminal`'s block-level compare reads the
  block's own top-level `completeness_status` and a patch missing that key
  would read as `MISSING`, risking clobbering an already-terminal verdict
  this call never meant to touch. Returns `{}` (a no-op) if `target` has no
  usable `block`. The caller feeds the result straight into the existing
  `crud.apply_field_completeness` (shielded) — no CRUD signature change
  needed for this.
- `run_completeness_chain(to_judge, coverage) -> dict` — one batched LLM
  call, the silence worker's whole-resume volunteered-info sweep only;
  returns `{reasoning, blocks, _llm_usage}`. Skips the network call when
  `to_judge` is empty. Fail-soft: schema/provider errors degrade to
  `blocks: {}`.
- `run_silence_completeness_worker(session_id, crud, queue)` (async, run as
  an `asyncio.Task` by the orchestrator) — consumes `True`/`False`/`None`
  speaking-state events off `queue` until `None` (session end). On `False`
  (silence), starts a debounce cycle as its own child task; on `True`,
  cancels any in-flight cycle. Each cycle is `prune_for_judgment` →
  `run_completeness_chain(to_judge, COVERAGE_SCHEMA)` → `merge_completeness`
  → one shielded `crud.apply_field_completeness(...)`.
- `run_completeness_grading_cycle(session_id, crud)` — the grading-only body
  of the above, extracted so it has a second caller: the post-extraction-
  batch trigger in `analysis_orchestrator.py` (background, unawaited). The
  interview director itself never awaits this call any more — it reads
  whatever `field_completeness` snapshot is already on the row at the start
  of `_finish_answer` (possibly a turn or more stale) rather than forcing a
  catch-up; see "The interview loop" below and
  [backend/stt-tts-pipeline.md](stt-tts-pipeline.md). Cancellation is the
  caller's concern — the silence worker's own `_run_one_cycle` still wraps
  its call in `try/except asyncio.CancelledError: return`.

## The interview loop — one trusted LLM call, deterministic target selection
The old design this section used to describe in detail — `select_focus_target`'s
priority chain (sticky focus, CONFLICT/UNRESOLVED special targets, importance
tiers, field-group batching), `build_next_target_shortlist`/`open_targets`
as the LLM's only allowed menu, `answer_evaluation_chain.run_answer_evaluation_chain`'s
fused grade-plus-shortlist-pick call, and `claim_reconciler`'s async
pending-BLOCK-claim verification ledger — is gone. A live session then
showed the next iteration of this design (grading + FREE target choice in
one call, no Python ordering at all) bouncing between blocks
(`projects` → `experience` → `education` → ...) instead of exhausting one at
a time, since the model had no enforced priority and wasn't even given the
round's own current subject. Target *selection* moved back into Python as a
result — grading and question *wording* remain one fused LLM call, exactly
as before. See [backend/stt-tts-pipeline.md](stt-tts-pipeline.md) for the
full state machine. In outline:

- **One LLM call per answer** — `question_chain.run_question_chain(resume,
  coverage, conversation_history, answer_text, field_completeness=...,
  current_target=..., next_target_candidates=...)` — grades the just-given
  answer (`answer_grade`: `PARTIAL`/`SUFFICIENT`/`UNABLE_TO_ANSWER`), drafts
  a `probe_question` grounded in `current_target` (the round's own
  already-known subject) if the grade stays open and the round isn't
  capped, and drafts a `next_question` for the first entry in
  `next_target_candidates` not already resolved by the live conversation if
  the grade resolves. `next_target_candidates` — built by Python's
  `next_target.compute_next_targets` (`resume_analysis_pipeline/`,
  touched blocks before untouched, each sorted ascending by
  `objective_priority`) — is the **complete** remaining list, not a sample:
  the model may only pick from it, never invent a target outside it, but it
  still decides which candidate is genuinely still open (using its own
  freshest context, including the very answer it's grading right now, which
  `field_completeness` hasn't caught up to yet) and how to word the
  question. See
  [backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md) for the
  chain itself and `compute_next_targets`'s exact algorithm.
- **Two small, deterministic Python guardrails sit on top of that trust**,
  both living directly in `InterviewDirector` (not in this module):
  1. **Forced conflict/unresolved priority** — before opening any new round,
     `_pick_forced_topic` checks `resume["conflicts"]` then
     `resume["unresolved"]` for a record whose key isn't already in
     `_forced_topics_spent`. If found, whatever `next_question` Task B just
     drafted is discarded outright and a small shared chain
     (`question_chain.run_topic_question_chain`) words a natural question
     for that specific conflict/unresolved record instead. Settlement of
     the record itself still happens purely through extraction (Task A) —
     the director never writes anything to resolve a conflict/unresolved
     entry.
  2. **Nothing left → end directly.** Reached only when there's no forced
     topic and Task B's own `next_question` came back `null`. Because
     `next_target_candidates` was already the exhaustive remaining list,
     a null `next_question` legitimately means every askable block is
     covered — there is no separate required-coverage safety-net call any
     more (`required_gap.py`/`find_required_gap` deleted, along with the
     `_await_task_a_settle` catch-up that used to precede it): `_advance_round`
     goes straight to `_complete_interview()`, the only site left that ends
     the session.
- **`_forced_topics_spent: Set[str]`** (director-only, in-memory) is the
  anti-infinite-loop mechanism for the forced-topic guardrail: a forced
  round's key (`"conflict:<id>"`/`"unresolved:<id>"`) is added the moment
  that round closes — terminal or capped — regardless of whether Task A has
  actually cleared the underlying record yet. Without this, a just-resolved
  conflict still sitting in `resume_data["conflicts"]` for one extra turn
  (extraction hasn't caught up) would force the identical question again
  immediately.
- **`_organic_targets_given_up: Set[Tuple[str, Optional[str]]]`**
  (director-only, in-memory) is the equivalent mechanism for the now-
  deterministic organic path: a `(block, item_id)` a round capped out on
  while still non-terminal is excluded from every later
  `compute_next_targets` call for the rest of the session — otherwise that
  same stuck subject, for which `field_completeness` never gets patched,
  would deterministically win top priority again next round.

## Cancellation / commit semantics
- **Per-answer grading** (`InterviewDirector._finish_answer`) — a
  resume-speaking event cancels the in-flight task. Before
  `run_question_chain` returns, nothing has been committed to CRUD yet, so
  a cancel there is a pure restore: `_awaiting_answer` back on, the answer
  text prepended to whatever accumulated during the call. After the call
  returns, the round's `record_round_answer`/`close_round` writes are each
  individually `asyncio.shield`-wrapped, so a cancellation landing anywhere
  in that narrow commit window can't strand a computed-but-uncommitted
  result. See [backend/stt-tts-pipeline.md](stt-tts-pipeline.md)'s
  "Cancel-safe turns".
- **Batched silence-worker cycle** — unchanged: a cancel mid-call discards
  the whole judgment; nothing is written, and the next silence re-grades
  the whole resume anyway, so a discarded judgment costs nothing.
- Relies on `asyncio.Task.cancel()` being a documented no-op on an
  already-finished task — there is no manual "was it too late?" bookkeeping.

## Data flow & dependencies
- The batched worker consumes the speaking-state queue fed by
  `room_orchestrator.enqueue_speaking_state`; the director gets the same
  signal as a direct in-process call from `pipeline.py`'s
  `on_speaking_change` — see [backend/stt-tts-pipeline.md](stt-tts-pipeline.md)
  and [backend/room-orchestration.md](room-orchestration.md).
- Both chains call into [backend/llm-providers.md](llm-providers.md) via
  `LLMProviderFactory`, `(provider_name, model)`-keyed cache,
  `LLMMessage`/`LLMRequestOptions`, `SchemaSpec.from_dict` — but on
  **different providers now**: `run_completeness_chain` (batched sweep)
  stays on `OpenRouterProvider` via `resume_room_completeness_*`;
  `question_chain.py`'s `run_question_chain`/`run_topic_question_chain` (the
  per-answer grading and forced-topic wording described below) moved to
  `OpenAIProvider` via their own `resume_room_question_*` settings — see
  [backend/app-config.md](app-config.md) and
  [backend/llm-providers.md](llm-providers.md)'s `OpenAIProvider` section.
- `field_completeness` lives in `{session_id}_status.json`;
  `row["questions"]` (now the round ledger — see
  [backend/database-models.md](database-models.md)) lives in the main
  `{session_id}.json` export alongside `resume_data`.

## Conventions & gotchas
- `COVERAGE_SCHEMA`'s block/field names must exactly match
  `RESUME_SCHEMA`'s — `completeness_status.py` looks blocks up by name
  across both and will raise on a mismatch the moment it's hit.
- The batched grader still produces only `PARTIAL`/`SUFFICIENT`. `MISSING`
  is always code-decided. `NOT_APPLICABLE` is produced by
  `prune_for_judgment` for any block/field whose `COVERAGE_SCHEMA` entry
  carries `not_applicable: true` — terminal, never overwritten.
- **`UNABLE_TO_ANSWER` is the explicit-decline verdict** and is terminal —
  an assertion that there's nothing to capture cannot be contradicted by an
  empty resume. `TERMINAL_STATUSES` (`SUFFICIENT | UNABLE_TO_ANSWER |
  NOT_APPLICABLE`) is what `merge_status_preserving_terminal` tests against.
  `_carry_unavailable()` is what stops `prune_for_judgment` force-writing
  `MISSING` back over a decline every cycle.
- **`COMPLETENESS_RESPONSE_SCHEMA` validates almost nothing.** `blocks` is a
  bare `{"type": "object"}` with `strict=False`, so every level below it is
  whatever the model felt like emitting. That looseness is deliberate (the
  rubric's shape is dynamic and per-session), which means **the merge, not
  the schema, is the trust boundary** — `merge_completeness`, `_merge_items`,
  `_as_leaf_map`, `_items_by_id` and `_leaf_status` all skip non-dict nodes
  instead of indexing them. Confirmed live: a session once returned
  `"items": ["p1", "p2"]` (bare strings where objects belong) and would have
  raised `TypeError` without this guard. Assume the same for any new reader
  of `field_completeness` — go through `_leaf_status`, never subscript raw.
- **Nothing awaits the silence worker's cycle task.** Fired with
  `asyncio.create_task` and only ever cancelled; `_safe_run_one_cycle` wraps
  it (re-raising `CancelledError`, logging everything else) — same fail-soft
  shape as `_safe_run_batch` in the extraction pipeline.
- **`field_completeness` freshness is bounded mostly by the batched
  sweep**, with one narrow director-side immediate write on top:
  `build_unable_to_answer_patch` commits an `UNABLE_TO_ANSWER` grade back
  onto the one leaf the closing round's `target` identifies (see above) —
  this is not a return of the old per-answer verdict model (no
  SUFFICIENT/PARTIAL write), just the one thing the batched grader
  structurally cannot ever infer on its own. Everything else can still lag
  further behind the live conversation for stretches of the interview;
  accepted trade-off, since `compute_next_targets` reads whatever snapshot
  is already on the row (no forced catch-up any more — there's no
  safety-net tier left to force one ahead of) and the fused call is
  designed to tolerate that staleness by using its own freshest context (the
  answer it's grading right now) to skip a candidate `field_completeness`
  hasn't caught up to yet.
- The two silence windows still mean different things even though both
  currently sit at 2.0s: `resume_room_silence_hardbound_seconds` gates the
  batched worker's own debounce; `resume_room_answer_silence_seconds` gates
  the director's answer-end debounce. Keep them separate even if their
  values coincide.
- Neither loop fires on a session where the candidate never speaks: both
  arm only on a stop-speaking event.

## Last synced
2026-09-05 (yet later still — deterministic block-priority target selection:
a live session showed the fully-free-choice fused call bouncing between
blocks instead of exhausting one at a time. Target *selection* moved to a
new Python scanner, `next_target.compute_next_targets` — grading and
question *wording* remain exactly one fused LLM call, same as before this
change, just no longer trusted to pick which block matters most on its own.
The old required-coverage safety net (`required_gap.py`/`find_required_gap`,
`InterviewDirector._await_task_a_settle`) is gone entirely, superseded by
`compute_next_targets` being exhaustive by construction — see "The
interview loop" above and
[backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md)/[backend/stt-tts-pipeline.md](stt-tts-pipeline.md)
for the full change.)
2026-09-05 (later still — `question_chain.py`'s `run_question_chain`/
`run_topic_question_chain` moved off `resume_room_completeness_*` onto their
own `resume_room_question_*` settings, and onto a new `OpenAIProvider`
(Responses API) instead of `OpenRouterProvider`. `run_completeness_chain`
(the batched silence-worker sweep) is unaffected — still OpenRouter. See
[backend/llm-providers.md](llm-providers.md) and
[backend/app-config.md](app-config.md).)
2026-09-05 (later still — `build_unable_to_answer_patch`'s `target["field"]`
pluralized to `target["fields"]` (`Optional[List[str]]`): a live run showed
one experience item split across four separate rounds, one per remaining
open field, instead of one consolidated question — see
[backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md)'s
"consolidate, don't drip-feed" rule. The patch now loops over every field
in `fields` so a single declined multi-field question commits every one of
them `UNABLE_TO_ANSWER` in one patch, not just the first.)
2026-09-05 (later same day — added `build_unable_to_answer_patch` to
`completeness_status.py`: a live session showed several declined optional
blocks/fields (personal projects, certifications, courses, an experience
item's own projects/achievements/awards) permanently reading `MISSING` in
the debug export despite an explicit spoken decline, because
`prune_for_judgment` only ever reads `resume_data` and a decline produces no
value there at all -- a structural blind spot, not a staleness lag. Wired
into `InterviewDirector._finish_answer`: when a round closes
`UNABLE_TO_ANSWER`, the round's own stored `target` (see
[backend/database-models.md](database-models.md) and
[backend/stt-tts-pipeline.md](stt-tts-pipeline.md)) is used to build and
shielded-commit a precise patch via the existing
`crud.apply_field_completeness`. See
[backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md) for the
paired `next_question_target` self-report that supplies `target`.)
2026-09-05 (major rewrite: replaced the entire target-selection/shortlist/
pending-BLOCK-claim design — `select_focus_target`, `build_next_target_shortlist`,
`open_targets`, field-group batching, special CONFLICT/UNRESOLVED targets,
`claim_reconciler.py`, `answer_evaluation_chain.py`/`answer_evaluation_prompts.py`
— with a small round-based Q&A trusted directly to one fused per-answer LLM
call (`question_chain.run_question_chain`, now living in
`resume_analysis_pipeline/`) plus two deterministic Python guardrails: forced
conflict/unresolved priority (`InterviewDirector._pick_forced_topic`) and a
required-coverage safety net before ending
(`required_gap.find_required_gap`). `completeness_status.py` trimmed from
~1200 to ~400 lines, keeping only `prune_for_judgment`/`merge_completeness`/
`merge_status_preserving_terminal` and their helpers — every
selection/shortlist/special-target/field-group helper deleted, grep-verified
unreferenced elsewhere. `field_completeness` now has exactly one writer
(`crud.apply_field_completeness`, fed only by the batched sweep) and the
director reads it in exactly one place (the required-coverage safety net).
See [backend/stt-tts-pipeline.md](stt-tts-pipeline.md) and
[backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md) for the
new design. Older history predating this rewrite described the deleted
selection/shortlist/claim machinery in detail and has been removed from this
file; `docs/` at the repo root may still hold narrative color on why that
design was originally chosen.)
