# Backend: Resume Analysis Pipeline

## Purpose
Turns the candidate's live transcript into a structured resume document.
Runs as a background worker per session: batches incoming transcript text,
periodically calls an LLM extraction chain to pull structured updates, and
runs one final "resolution" LLM pass over the whole transcript when the
session ends to clean up anything left ambiguous.

## Key files
- `backend/app/meeting_room/resume_analysis_pipeline/analysis_orchestrator.py`
  — `run_resume_analysis_worker()`, the batching/trigger loop.
- `backend/app/meeting_room/resume_analysis_pipeline/analysis_chain.py` —
  `run_resume_extraction_chain()`, `run_resume_final_resolution_chain()` —
  the actual LLM calls + schemas.
- `backend/app/meeting_room/resume_analysis_pipeline/analysis_prompts.py` —
  `EXTRACTION_SYSTEM_PROMPT`, `FINAL_RESOLUTION_SYSTEM_PROMPT`, and the user
  prompt builders.
- `backend/app/meeting_room/resume_analysis_pipeline/merge.py` — pure
  merge/conflict logic applied to the in-memory resume document.
- `backend/app/meeting_room/resume_analysis_pipeline/config_jsons_definitions/resume_schema.py`
  — `RESUME_SCHEMA`, the canonical resume shape (despite the directory name,
  this file and `coverage_schema.py` are the only two files that matter
  here — no literal `.json` files are loaded by this pipeline).
- `backend/app/meeting_room/resume_analysis_pipeline/question_prompts.py` +
  `.../question_chain.py` — the interview director's per-answer grading and
  question-wording chains. Physically live in this directory (not
  `stt_tts_pipeline/`) even though they drive the interview loop, because
  they're LLM chains alongside the pipeline's other ones; see "Public
  surface" below and
  [backend/stt-tts-pipeline.md](stt-tts-pipeline.md)/[backend/completeness-pipeline.md](completeness-pipeline.md)
  for how the director calls them.
- `backend/app/meeting_room/resume_analysis_pipeline/next_target.py` — the
  deterministic, priority-ordered target scanner (`compute_next_targets`)
  the interview director hands to the fused call as the exhaustive
  candidate list `next_question_target` must be picked from. Replaces the
  deleted `required_gap.py`. See "Public surface" below.

## Public surface
- `run_resume_analysis_worker(session_id, queue, crud)` (async, run as an
  `asyncio.Task` by the orchestrator) — consumes transcript chunks off
  `queue` until it receives `None` (session end sentinel), accumulating text
  until `resume_room_extraction_trigger_chars` is reached, then running one
  extraction batch. On `None`, runs the final resolution pass instead and
  returns.

  A third input, `FlushRequest` (an `asyncio.Event`-carrying sentinel
  distinct from `None`), forces one extraction batch on whatever's
  accumulated regardless of the char trigger, then sets the event and
  `continue`s the loop — if nothing has accumulated it's a same-tick
  no-op. This exists because selection and the silence grader both read
  `resume_data` as a plain snapshot with no idea a batch is pending; see
  `ResumeRoomOrchestrator.flush_transcript` below and
  [backend/room-orchestration.md](room-orchestration.md). Stale
  `remaining_text` alone does NOT trigger a flush batch — it was already
  seen and deliberately deferred by an earlier call, so re-running on it
  with nothing new added would just repeat that same deferral.

  Chunks arrive as plain `str`. They used to be `(seq, text)` tuples feeding
  a per-batch claim **watermark**; that whole mechanism, and the pending-
  BLOCK-claim ledger it fed, is gone entirely — the interview director no
  longer files or reconciles claims of any kind. `_run_batch` never
  reconciles anything, and there is no longer a session-end reconciliation
  step either: `_run_final_pass` no longer calls into any claim machinery.
- **Completeness grading is also triggered from here, not just on silence.**
  After `crud.apply_resume_update` commits, `_run_batch` computes a composite
  "did this batch actually change anything" signal
  (`result.get("status") != "no_update"` and at least one of: `accepted`
  non-empty, `unresolved`, `resolved_conflicts`, `resolved_unresolved_ids`
  present) and, if true, fires
  `silence_completeness_worker.run_completeness_grading_cycle(session_id,
  crud)` as a background `asyncio.create_task` — not awaited, so it never
  adds latency to extraction itself. This runs on **every** extraction batch
  that changed something, char-triggered or `FlushRequest`-triggered alike,
  in addition to (not instead of) the silence-EOT trigger described in
  [backend/completeness-pipeline.md](completeness-pipeline.md). Tasks are
  tracked in a module-level `_grading_tasks: Dict[str, Set[asyncio.Task]]`
  keyed by `session_id` (strong refs, matching `crud.py`'s `_write_tasks`
  GC-safety pattern) and cancelled via `cancel_grading_tasks(session_id)`,
  called from `room_orchestrator.py`'s `_close_out` (and its bot-spawn-failure
  cleanup path) so none linger past session teardown. **This does not shorten
  the interview director's own answer→next-question latency** — that turn's
  only critical-path LLM call is `question_chain.run_question_chain` (below),
  independent of this batched-grading call; this trigger exists purely to
  keep `field_completeness` fresher for the silence worker's own
  volunteered-info sweep, for the director's own required-coverage safety net
  (which reads `field_completeness` once per gap check — see
  [backend/stt-tts-pipeline.md](stt-tts-pipeline.md)), and for external/debug
  readers. See [backend/completeness-pipeline.md](completeness-pipeline.md).
- **`run_question_chain(resume, coverage, conversation_history, answer_text, field_completeness=None, *, current_target=None, next_target_candidates=None) -> dict`**
  (`question_chain.py`) — the single LLM call the interview director makes
  per answer turn. Given the whole extracted `resume`, the *askable*
  coverage rubric (see `ASKABLE_COVERAGE_SCHEMA` below — this chain applies
  no filtering of its own, so the caller must already have stripped
  `not_applicable` blocks out of whatever it passes as `coverage`), the
  entire conversation so far (every round's exchanges flattened, oldest
  first — no windowing/truncation), and optionally the last computed
  `field_completeness` verdicts, it: detects a meta/process question first
  (`is_meta_question`/`meta_response`, ported near-verbatim from the
  deleted `answer_evaluation_prompts.py`'s own meta-detection block); grades
  the last history entry's answer (`answer_grade`:
  `PARTIAL`/`SUFFICIENT`/`UNABLE_TO_ANSWER`, `ANSWER_GRADE_*`/
  `TERMINAL_GRADES` constants in `question_chain.py` — a distinct, narrower
  concept than `completeness_status.TERMINAL_STATUSES`, since
  `NOT_APPLICABLE` has no meaning for a single graded answer); and always
  drafts BOTH a `probe_question` and a `next_question` in the same response,
  regardless of grade.

  **Target *selection* is Python's job, not this chain's.** `current_target`
  (`{"block", "item_id", "fields"}|None`) is the calling round's own
  already-known subject — `probe_question` is grounded in it directly
  instead of being re-inferred from raw conversation text.
  `next_target_candidates` is the COMPLETE, priority-ordered list of
  everything else left to ask about (built by
  `next_target.compute_next_targets`, below — touched blocks before
  untouched, each in `objective_priority` order): the model may only word
  `next_question` for the first candidate not already resolved by the live
  conversation (including the very answer it's grading right now, which
  `field_completeness` can't have caught up to yet), narrowing that
  candidate's own `fields` down to whichever are genuinely still open. It
  can never invent a target outside this list. Because the list is
  exhaustive by construction, "every given candidate is already resolved"
  and "nothing is left to ask" are the same condition — `next_question`/
  `next_question_target` are `null` only when `next_target_candidates` is
  empty or every entry in it is resolved; otherwise both are required. This
  is a two-tier design with **no fallback/safety-net tier of any kind** — if
  the model determines the whole list is resolved, the interview really is
  done (see `InterviewDirector._advance_round`,
  [backend/stt-tts-pipeline.md](stt-tts-pipeline.md)).

  `field_completeness` is explicitly flagged in the prompt as possibly
  one-or-more-turns stale (written by a separate batched worker) —
  `resume`/`conversation_history` win on any disagreement. Fail-soft:
  `_empty_result()` on an empty `answer_text` or a provider/schema error
  defaults to `answer_grade=PARTIAL`, everything else `None`/`False` —
  "nothing usable" always means "still open," never "done." Uses its own
  settings, separate from `completeness_chain.py` —
  `resume_room_question_provider` (default `"openai"`),
  `resume_room_question_model`, `resume_room_question_max_tokens`,
  `resume_room_question_reasoning_effort` — routed through `OpenAIProvider`
  by default, not `OpenRouterProvider`; see
  [backend/llm-providers.md](llm-providers.md).

  Whenever `next_question` is non-null, the response also self-reports
  `next_question_target: {"block", "item_id", "fields"}` (`item_id`
  nullable, `fields` an optional *list* of field names) naming exactly
  which given candidate it used. `fields` is plural (not a single field)
  precisely so one question can name every currently-open field of the
  targeted item/block at once. Three prompt rules force the wording to be
  precise rather than blended: (1) the **block/field collision rule** —
  `experience`'s own `projects`/`achievements`/`awards` fields (scoped to
  one specific job) are a different concept from the top-level
  `projects`/`achievements`/`awards` blocks (the candidate's own standalone
  entries); a question about a specific job's projects/achievements/awards
  reports `"block": "experience"` with the matching entries in `fields`,
  never the standalone block name; (2) the **name-the-specific-item rule**
  — for a repeatable block (`experience`/`education`/`projects`/
  `certifications`/`courses`) with more than one item, a question about one
  specific existing item must name that item's own identifying detail
  (role/company, degree/college, or the item's name) rather than a bare
  generic reference; (3) the **consolidate, don't drip-feed rule** (added
  after a live run produced four separate rounds for one experience item's
  four remaining open fields, asked one at a time) — before wording a
  question for a candidate, check `field_completeness`'s per-item field
  breakdown; if more than one field is still MISSING/PARTIAL for it, the
  single question MUST cover all of them together (e.g. "where was your
  internship at AI Solve based, and did you work on any specific projects,
  achieve any notable results, or receive any awards during that role?"),
  listing every field it addresses in `fields` — never split them across
  separate rounds, even non-consecutive ones.

  `question_chain._validate_next_target(target, candidates)` is the trust
  boundary for the model's self-reported `next_question_target`: it must
  name one of the given `next_target_candidates` by `(block, item_id)` —
  a cheap containment check against a small Python-built list, not the old
  `_sanitize_target`'s full coverage-schema validation, since every
  candidate is already schema-valid by construction — with `fields`
  narrowed to a subset of the matched candidate's own fields (falling back
  to the candidate's full field list on an empty/unrecognized subset). Any
  report that doesn't match a given candidate at all falls back to
  `candidates[0]` — the single highest-priority pick — verbatim. This
  validated value is what `InterviewDirector` stores on the round it opens;
  a later `UNABLE_TO_ANSWER` grade on that round commits every field in
  `fields` back into `field_completeness` in one patch via
  `build_unable_to_answer_patch` (below) — see
  [backend/stt-tts-pipeline.md](stt-tts-pipeline.md).
- **`run_topic_question_chain(resume, coverage, conversation_history, topic_description, field_completeness=None) -> dict`**
  (`question_chain.py`) — one small shared chain used for BOTH forced
  conflict/unresolved topics and required-gap safety-net topics (not three
  separate chains). Given a short Python-built natural-language
  `topic_description` and, optionally, `field_completeness` for extra
  grounding on which fields of that topic are concretely still open, words
  ONE natural spoken question inviting the candidate to address it,
  grounded in `resume`/`conversation_history` so it doesn't re-ask
  something already covered. Returns `{"question": str}`. Fail-soft: on an
  empty `topic_description` or a provider/schema error, falls back to
  `_topic_fallback_question` — a single Python-templated sentence
  (`f"Let's cover one more thing -- could you tell me about
  {topic_description}?"`) — the **only** non-LLM question-wording fallback
  left anywhere in the codebase. See
  [backend/stt-tts-pipeline.md](stt-tts-pipeline.md) for how
  `InterviewDirector` builds `topic_description` for each of the three
  forced-topic kinds (conflict, unresolved, required gap) — these two
  call sites keep passing the **full** `COVERAGE_SCHEMA` (unfiltered),
  since the topic itself is already pre-decided by Python in both cases;
  only the free-choosing `run_question_chain` call needs the askable
  filter.
- **`askable_coverage_schema(coverage=None) -> dict`** /
  **`ASKABLE_COVERAGE_SCHEMA`** (`coverage_schema.py`) — `coverage` (or the
  module's own `COVERAGE_SCHEMA` if omitted) with every block whose
  `not_applicable` is `true` removed. This is the one shared choke-point for
  "which blocks may the candidate ever be asked about through a spoken
  question" — `personal`/`summary` are captured elsewhere in the product
  (a signup/profile form), never through this interview. Used by
  `InterviewDirector`'s fused-call site (passed instead of `COVERAGE_SCHEMA`,
  see [backend/stt-tts-pipeline.md](stt-tts-pipeline.md)) and by
  `compute_next_targets` below.
- **`compute_next_targets(resume, coverage, field_completeness, *, exclude_targets=frozenset()) -> List[dict]`**
  (`next_target.py`) — the deterministic, priority-ordered scanner that
  replaced `run_question_chain`'s old free target-selection (and,
  entirely, the narrower `required_gap.py`/`find_required_gap` it
  superseded — deleted, no remaining callers). Filters `coverage` through
  `askable_coverage_schema`, drops any block whose own top-level
  `completeness_status` in `field_completeness` is already in
  `completeness_status.TERMINAL_STATUSES` (a block with no verdict yet
  defaults to open — same convention `required_gap.py` used), then splits
  the rest into `touched` (`bool(resume.get(block))`) and `untouched`, each
  sorted ascending by `objective_priority` (1 = highest). Walks
  `touched + untouched` in that order building ONE candidate per block: a
  block-level-only block, or an untouched item-level block, becomes
  `{"block": block, "item_id": None, "fields": None}`; an item-level block
  with existing items (`experience`/`education`/`projects`/
  `certifications`/`courses`) walks `resume[block]`'s own items in resume
  order and returns the first one with any open field as `{"block":
  block, "item_id": item_id, "fields": open_fields}` — a block whose every
  item is already field-terminal (despite a stale non-terminal block-level
  status) contributes no candidate rather than one with empty `fields`.
  `exclude_targets` (a set of `(block, item_id)` pairs) is skipped entirely
  — the caller passes the round currently being graded (so it's never
  immediately reselected as its own successor) plus every
  `_organic_targets_given_up` pair (a block/item a prior round capped out
  on while still non-terminal, so it doesn't loop the interview on a stuck
  topic forever). Returns the **full** list, never truncated — an empty
  list means every askable block is genuinely covered. See
  `run_question_chain` above for how the fused call consumes this list, and
  [backend/stt-tts-pipeline.md](stt-tts-pipeline.md) for
  `InterviewDirector._finish_answer`'s call site and
  `_organic_targets_given_up`.
- `run_resume_extraction_chain(resume, new_text) -> dict` — one incremental
  LLM call; returns `{reasoning, updates, unresolved, resolved_conflicts,
  resolved_unresolved_ids, remaining_text, status, _llm_usage}`. Never
  raises — provider/schema failures degrade to `status: "no_update"` with
  `remaining_text` carrying the input forward so no text is lost.
- `run_resume_final_resolution_chain(resume, full_transcript) -> dict` —
  one LLM call over the *entire* user transcript at session end; returns
  `{reasoning, updates, _llm_usage}`.
- `merge_updates(resume, updates, *, force_overwrite=False) -> (resume,
  accepted, rejected)` — applies an `updates` payload to the resume dict
  in place, per-block-kind (`singular`/`singular_freeform`/`list_object`/
  `list_string`), diverting genuine conflicts into `resume["conflicts"]`
  unless `force_overwrite=True` (used by the final pass).
  `force_overwrite` changes behaviour for **both** value shapes: a scalar
  field is written over its existing value instead of raising a conflict,
  and an item's **array** field (`responsibilities`, `skills`, ...) is
  **replaced wholesale** instead of appended. See the gotcha below — the
  array half of that is load-bearing, not incidental.
- `merge_unresolved`, `apply_resolved_conflicts`, `remove_unresolved` —
  manage `resume["unresolved"]` / `resume["conflicts"]` side-lists.
  These two lists are no longer only an extraction-prompt concern: the
  interview director checks them directly (`_pick_forced_topic`, ahead of
  any organically-drafted next question) and settles each one through
  exactly these functions (via `crud.apply_resume_update`'s
  `resolved_conflicts` / `resolved_unresolved_ids` kwargs) — purely through
  extraction, same as always; the director itself never writes a resolution.
  A record's presence is what keeps the forced question alive and its
  removal (by Task A) is what retires it, though the director's own
  `_forced_topics_spent` bookkeeping is what actually stops it being
  re-forced once that round has closed, since Task A clearing the record can
  lag a turn or more behind. See
  [backend/stt-tts-pipeline.md](stt-tts-pipeline.md) and
  [backend/completeness-pipeline.md](completeness-pipeline.md). Record
  shapes: a conflict is `{id, block, field, item_id, existing_value,
  candidates[]}`, an unresolved item is `{id, block, text, note}`.
- `is_redundant_with_accepted_update(text, updates, accepted, *,
  min_shared_tokens=3) -> bool` (`merge.py`) — the Python-side trust-boundary
  guard for extraction's own dual-attribution rule (see "Conventions &
  gotchas" below). Reuses `answer_evidence.normalize`/`evidence_tokens` (the
  same tokenizer `answer_evaluation`'s `also_covered` verification uses) to
  token-overlap-check an about-to-be-added `unresolved` entry's `text`
  against the values this SAME extraction response already wrote into
  `updates` (restricted to the field paths `merge_updates` actually
  `accepted`, not merely proposed). `crud.apply_resume_update` calls this to
  filter `unresolved` before `merge_unresolved` runs, dropping any entry that
  overlaps — see below.
- `RESUME_SCHEMA`, `empty_resume()`, `block_kind()`, `render_schema_for_prompt()`
  — schema introspection used both by `merge.py` and by the prompt builders.

Real per-field/per-block *completeness* grading (MISSING/PARTIAL/SUFFICIENT,
judged against a coverage rubric, not just presence-of-a-value) is a
separate pipeline living in this same directory — see
[backend/completeness-pipeline.md](completeness-pipeline.md). The naive
presence-only `field_status.py` module this replaced has been deleted.

## Data flow & dependencies
- Consumes the transcript queue fed by
  `room_orchestrator.enqueue_transcript` (only user/candidate lines — the
  bot's own speech is never queued for extraction), carrying plain strings.
  See
  [backend/stt-tts-pipeline.md](stt-tts-pipeline.md) for the producer side.
- Calls into [backend/llm-providers.md](llm-providers.md)
  (`LLMProviderFactory`) for both the incremental-extraction and
  final-resolution models — these are configured as **separate** provider/
  model pairs (`resume_room_extraction_*` vs `resume_room_final_pass_*`),
  cached separately per `(provider, model)` key.
- Writes results back through
  `ResumeRoomCRUD.apply_resume_update`/`apply_final_resolution` — see
  [backend/database-models.md](database-models.md) — which is what actually
  mutates the session's `resume_data` and folds in LLM cost accounting.
- `merge.py` is pure/synchronous and has no I/O — safe to unit test in
  isolation against `RESUME_SCHEMA`.

## Conventions & gotchas
- Both `_safe_run_batch` and `_safe_run_final_pass` swallow **all**
  exceptions from their inner call — a single bad extraction never crashes
  the worker or drops the session. On extraction failure, `_cap_carry`
  reconstructs a bounded "remaining_text" from what would have been lost
  (capped at `trigger_chars * max_carry_multiple`) so partial text isn't
  silently discarded, only trimmed if it grows unbounded.
- `RESUME_SCHEMA` block kinds each have distinct merge semantics — read
  `resume_schema.py`'s top-of-file comment before adding a new block or
  changing an existing one's kind; the merge/prompt code branches on `kind`
  everywhere.
- Placeholder values ("Not specified", "N/A", "Unknown", "TBD", etc., see
  `_PLACEHOLDER_VALUES` in `merge.py`) are treated as "no value" and
  discarded rather than stored — both the LLM prompt and the merge code
  independently guard against this, since a stored placeholder would render
  as if it were real data.
- `_set_or_conflict`: re-submitting the exact same value for a field that
  already has one is a silent no-op (not a conflict, not "accepted"); a
  genuinely differing value is diverted to `resume["conflicts"]` unless
  `force_overwrite=True` — only the final-resolution pass forces overwrites.
- `_same_labeled_entry` in `merge.py` deliberately only conflates two
  `list_string` entries sharing a `"Label: ..."` prefix — unlabeled short
  strings (e.g. "Java" vs "JavaScript") are never merged this way; keep this
  distinction if extending list-string dedup.
- **`merge_updates` monotonicity is now load-bearing.** `_append_dedup` only
  ever appends and `_set_or_conflict` diverts a differing scalar into a
  conflict record rather than clearing the existing one, so a block's
  `block_fingerprint` can only grow during a live session. The claim
  reconciler relies on exactly that to tell "the extractor landed
  something" from "nothing arrived". If any merge path ever starts
  clearing values, verification silently stops detecting landings — and
  claims would then be voided and their blocks re-asked, which is the safe
  direction but still wrong. (The one legitimate shrink is
  `apply_final_resolution`'s array replace. `classify_claims` now uses the
  `> baseline` rule uniformly, so a claim still pending at the final pass
  whose block the rewrite shrank voids instead of confirming. That is a
  cosmetic `reopened` entry in the debug export, not a behavioral problem:
  the interview is already over, and the director's pre-question reconcile
  means the ledger is normally empty by then.)
- **Item array fields must REPLACE on the final pass, never append.** The
  final pass is explicitly an ATS-style *rewrite* of the same
  `responsibilities`/`description` bullets, so appending its output leaves
  the raw and the polished version of every bullet sitting side by side —
  and `_append_dedup` cannot catch that, because a reworded bullet
  ("prototype" → "proof of concept", trailing full stop) is neither an
  exact match nor a `"Label: ..."` restatement. This shipped as a real bug
  and doubled every bullet in the first session that ran to completion.
  Because replacement is destructive, `FINAL_RESOLUTION_SYSTEM_PROMPT`
  states the contract explicitly — return the *full* final list for any
  array field you include, or omit the field entirely. Changing one side of
  that contract means changing the other.
- **Extraction must not dual-attribute a fact.** `EXTRACTION_SYSTEM_PROMPT`'s
  `unresolved` rule originally only covered intra-block item ambiguity ("two
  roles at the same company"); a live session showed it also hallucinating a
  *cross-block* `unresolved` record for a fact it had, in the same response,
  already confidently written into `updates` under the correct block — e.g.
  an education start-date/location answer also got filed as an ambiguous
  `experience` fact, even though `experience`'s own start_date/location were
  already populated with unrelated values. The extractor has no access to
  the interviewer's own question text at all (`pipeline.py`'s `persist()`
  only enqueues `role == "user"` lines — "no interviewer speech is ever
  included here" is stated directly in the prompt), so it's working from a
  narrower context window than the fused answer-evaluation call and is the
  more likely source of this failure mode. The prompt now: (1) treats
  cross-block ambiguity the same as intra-block ambiguity — "unsure which
  block" reports to `unresolved` same as "unsure which item"; (2)
  explicitly prohibits attributing the same fact to both `updates` and
  `unresolved` in one response; (3) tells the model a block whose relevant
  fields are already populated is an implausible target for a *new*
  ambiguous fact unless the excerpt clearly describes a distinct new entry.
  These are also mirrored into the prompt's own QUALITY CHECK self-review
  bullets. This is a **surgical** fix, not the deeper structural change of
  also feeding extraction the interviewer's question context — deliberately
  chosen to avoid touching the "no interviewer speech" invariant.
  Belt-and-suspenders on top: `crud.apply_resume_update` (see
  [backend/database-models.md](database-models.md)) now calls
  `is_redundant_with_accepted_update` (above) on every `unresolved` entry
  before `merge_unresolved` runs, dropping any entry whose text
  token-overlaps an `updates` field this same call actually accepted, with an
  info log line ("dropped redundant unresolved entry"). This only fires when
  `updates` was non-empty in the same call — an unrelated, genuinely
  ambiguous fact elsewhere in the excerpt is untouched. Same trust-boundary
  pattern as `also_covered`/next-target-shortlist validation elsewhere in
  this codebase: the LLM proposes, Python is the boundary.
- Extraction and final-resolution use **separate** JSON schemas
  (`EXTRACTION_RESPONSE_SCHEMA`, `FINAL_RESOLUTION_RESPONSE_SCHEMA` in
  `analysis_chain.py`) and separate configured models — the final pass is
  expected to run a stronger/larger-context model since it sees the whole
  transcript at once.

## Last synced
2026-09-05 (yet later still — deterministic block-priority target selection,
paired with the [backend/stt-tts-pipeline.md](stt-tts-pipeline.md) change of
the same name: added `next_target.py`'s `compute_next_targets` (touched
blocks before untouched, each in `objective_priority` order, exhaustive and
never truncated) as the Python-authoritative replacement for
`run_question_chain`'s old free target-selection. `run_question_chain`
gained `current_target`/`next_target_candidates` keyword params —
`current_target` grounds `probe_question` in the round's own already-known
subject instead of raw-text re-inference; `next_target_candidates` is the
list the model must pick `next_question`'s subject from, never inventing
one outside it. New `question_chain._validate_next_target` replaces
`InterviewDirector._sanitize_target` (deleted) as the trust boundary for
the model's self-reported `next_question_target` — cheaper now, since it
only needs to check containment in a small Python-built list rather than
validate against the whole coverage schema. Deleted `required_gap.py`/
`find_required_gap` entirely: its narrower required-tier-only safety net
has no remaining caller now that `compute_next_targets` is exhaustive by
construction and there is no more fallback tier of any kind after the fused
call — a null `next_question` goes straight to `_complete_interview()`. See
[backend/stt-tts-pipeline.md](stt-tts-pipeline.md) for the director-side
half of this change, including the new `_organic_targets_given_up`
loop-prevention set and the deleted `_await_task_a_settle`.)
2026-09-05 (yet later still — trimmed `SYSTEM_PROMPT`/
`TOPIC_QUESTION_SYSTEM_PROMPT` in `question_prompts.py` for latency:
restructured with Markdown section headers (`# Identity`, `# Step 1:
meta-question check`, `# Inputs`, `# Step 2: grade + draft the next two
questions`) per OpenAI's reasoning-model prompting guidance (keep prompts
direct, avoid hedging/redundant prose, use delimiters for section clarity)
— every substantive behavioral rule (meta-question grounding, block/field
collision, name-the-specific-item, consolidate-don't-drip-feed, the
three-way `answer_grade` semantics, always-draft-both-follow-ups,
`next_question_target` shape) is unchanged, only wording was cut. Verified
against representative live calls (multi-field consolidation, specific-item
naming) with no behavioral regression. Also corrected this file's stale
claim that `run_question_chain` reuses `resume_room_completeness_*`
settings — it's used `resume_room_question_*` via `OpenAIProvider` since
the OpenAI-provider switch earlier the same day; see
[backend/llm-providers.md](llm-providers.md) for the accompanying
prompt-caching fix in `OpenAIProvider` itself, which is what these prompts'
now-stable Markdown-header prefix is designed to be cached by.)
2026-09-05 (later still — round 3 (part 1) of live-session bug fixes: a
session showed one experience item getting FOUR separate rounds, each
asking about exactly one of its remaining open fields (`location`,
`projects`, `achievements`, `awards`) rather than one consolidated
question. Root cause: `next_question_target`'s `field` slot could only
name one field, so the model had no way to self-report a question that
covered several. Pluralized `field` to `fields` (`Optional[List[str]]`) in
`QUESTION_RESPONSE_SCHEMA` and `run_question_chain`'s normalization, and
added the **consolidate, don't drip-feed** prompt rule (see "Public
surface" above) requiring every currently-open field of a targeted item/
block to be folded into one question. `build_unable_to_answer_patch`
(below) now loops over `fields` to commit every declined field in one
patch. Priority/ordering across different blocks is a known, separate,
explicitly-deferred follow-up — not addressed by this change.)
2026-09-05 (later same day — round 2 of live-session bug fixes: (1) a
session with multiple projects/achievements/awards concepts showed the
fused call blending an experience item's own scoped projects/achievements/
awards with the top-level standalone blocks of the same name into one
question, and failing to name which experience/education item a question
concerned once framed generically ("during your internship" rather than
"during your internship at AI Solve"). Added an explicit block/field-
collision rule and a name-the-specific-item rule to `SYSTEM_PROMPT` and
`TOPIC_QUESTION_SYSTEM_PROMPT`. (2) Added `next_question_target` to
`QUESTION_RESPONSE_SCHEMA` and `run_question_chain`'s normalization/
docstring — see "Public surface" above — so the fused call self-reports
what its own `next_question` is about, which both forces (1)'s rules to
resolve concretely and gives `InterviewDirector` what it needs to close the
`field_completeness` decline gap (see
[backend/completeness-pipeline.md](completeness-pipeline.md)'s
`build_unable_to_answer_patch` and
[backend/stt-tts-pipeline.md](stt-tts-pipeline.md)'s target bookkeeping).)
2026-09-05 (bug fixes from live-session testing, not a redesign: added
`askable_coverage_schema()`/`ASKABLE_COVERAGE_SCHEMA` to `coverage_schema.py`
as the one shared filter for "which blocks may ever be asked about through a
spoken question" — the fused `run_question_chain` call was previously handed
the raw `COVERAGE_SCHEMA` including `not_applicable` blocks, and, with no
instruction ruling them out, once asked the candidate for their full
name/email/phone (`personal` is `not_applicable`). `required_gap.py` already
filtered this inline; refactored it to share the new helper instead of
duplicating the check. Also threaded a new optional `field_completeness`
param through both `run_question_chain` and `run_topic_question_chain` (and
their prompt builders) — a live session showed the fused call's probe padding
in an irrelevant re-ask ("describe two or three things you did there") for
responsibilities the candidate had already given, because the chain had no
ground truth for exactly which fields of the current item were still
open, only raw `resume` + prose `coverage` bars to eyeball. `field_completeness`
(already computed by the unchanged batched worker) gives it that ground
truth directly, with an explicit staleness caveat in both system prompts.)
2026-09-05 (added `question_chain.py`/`question_prompts.py`
(`run_question_chain`, `run_topic_question_chain`) and `required_gap.py`
(`find_required_gap`) to this directory — the interview director's per-answer
grading/next-question chain and required-coverage safety net, replacing the
deleted `answer_evaluation_chain.py`/`answer_evaluation_prompts.py`. Deleted
`claim_reconciler.py` and its one call site in `_run_final_pass` — the
pending-BLOCK-claim ledger it verified no longer exists; the interview
director never files or reconciles claims of any kind now. See [backend/stt-tts-pipeline.md](stt-tts-pipeline.md) and
[backend/completeness-pipeline.md](completeness-pipeline.md) for the full
round-based redesign this supports.)
2026-09-04 (fixed extraction dual-attributing a fact across blocks —
`EXTRACTION_SYSTEM_PROMPT`'s `unresolved` rule broadened to cross-block
ambiguity, plus an explicit prohibition on the same fact landing in both
`updates` and `unresolved` in one response, plus guidance against defaulting
an ambiguous fact onto an already-populated block. Added
`merge.is_redundant_with_accepted_update` and wired it into
`crud.apply_resume_update` as a Python-side backstop that drops any
`unresolved` entry overlapping this same call's own accepted `updates`
before `merge_unresolved` runs. See "Public surface" and "Conventions &
gotchas" above.)
2026-09-04 (added the post-extraction-batch completeness-grading trigger:
`_run_batch` now fires `run_completeness_grading_cycle` in the background
whenever a batch changes anything, alongside the existing silence-EOT
trigger, purely for `field_completeness` freshness — does not shorten the
interview director's own critical path. Also updated for plain-string
transcript chunks and the removal of per-batch claim reconciliation)
