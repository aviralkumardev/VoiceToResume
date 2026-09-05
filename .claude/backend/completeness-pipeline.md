# Backend: Completeness Grading & Interview Mode

## Purpose
Judges how *complete* the live-extracted resume actually is, against a
static coverage rubric, rather than just whether a field has any value —
and, together with `next_target.py`, computes the Python-authoritative
priority order for what the interview should ask about next.

There is **no separate batched completeness worker any more**. Coverage
grading is fused directly into the same combined background LLM call that
does extraction (`combined_chain.run_combined_chain` — see
[backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md)), fired
on the same ~100-char buffer trigger (plus a flush at every answer-end).
That one call also regenerates the entire upcoming-question queue in the
same response, already fully worded — there is no separate "pick a target,
then word it" step, and no silence-triggered whole-resume sweep distinct
from extraction any more.

Two independent consumers of this module's pure logic:
1. **The combined-analysis batch** (`analysis_orchestrator._run_batch`) —
   calls `prune_for_judgment` to build the completeness grading payload,
   calls `compute_candidate_queue` to build the priority-ordered candidate
   list handed to the LLM as `candidate_queue`, and calls
   `merge_completeness` to fold the LLM's `blocks` verdicts back onto the
   stored `field_completeness`. This is the **only** producer of a fresh
   `field_completeness` judgment.
2. **The interview director** (`stt_tts_pipeline/interview_director.py` —
   see [backend/stt-tts-pipeline.md](stt-tts-pipeline.md)) — writes exactly
   one thing here: `build_unable_to_answer_patch`, committed the instant a
   **targeted round** closes `UNABLE_TO_ANSWER`. This remains the only path
   for a decline made *inside* a round about that exact block/field (the
   opening round is never graded at all — `target is None` — so nothing
   said there can ever reach this path). It never computes a candidate list
   or picks a target itself any more — it only pops the already-worded head
   of `questions.queue`.

## Key files
- `.../config_jsons_definitions/coverage_schema.py` — `COVERAGE_SCHEMA`:
  per-block/per-field `importance` (`required`/`recommended`/`optional`) +
  a natural-language `complete_when` bar, a per-block `objective_priority`
  (used by `next_target.py`'s ordering), and an optional `not_applicable:
  true` flag (flagged block/field goes straight into `already_decided` as
  `NOT_APPLICABLE` by `prune_for_judgment`, never sent to the LLM).
  `personal`/`summary` are both `required` importance but ALSO
  `not_applicable: true`, so they never surface as an open required gap.
  `ASKABLE_COVERAGE_SCHEMA` / `askable_coverage_schema()` — `COVERAGE_SCHEMA`
  with every `not_applicable` block removed, the one shared filter for
  "which blocks may ever be asked about through a spoken question."
  `complete_when_for_target(coverage, target) -> Any` — the block's own
  `complete_when` for a whole-block target (`fields` falsy), or the list of
  the named fields' own `complete_when` strings for a field-scoped one;
  `None` if `target`'s block isn't in `coverage`. Used both to bake
  `complete_when` into each `compute_candidate_queue` entry and, in
  `interview_director.py`, to build `target_complete_when` for the narrow
  per-answer grading call from a round's stored `target`.
- `.../completeness_status.py` — the pure, no-I/O grading core:
  `STATUS_MISSING`/`STATUS_PARTIAL`/`STATUS_SUFFICIENT`/
  `STATUS_NOT_APPLICABLE`/`STATUS_UNABLE_TO_ANSWER`, `TERMINAL_STATUSES`,
  `prune_for_judgment()`, `merge_completeness()`,
  `merge_status_preserving_terminal()`, `build_unable_to_answer_patch()`,
  and their private helpers.
- `.../next_target.py` — `compute_next_targets` (unchanged internal
  scanner) plus `gap_key`/`compute_candidate_queue`, the Python-authoritative
  priority-ordered candidate list builder consumed by the combined call.
  See [backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md)
  for its exact algorithm and call site.
- `question_chain.py`/`question_prompts.py` (physically live in
  `resume_analysis_pipeline/`, documented in full there) — the narrow
  per-answer grading chain (`run_answer_grading_chain`) the interview
  director calls once per turn. It never sees the resume, coverage rubric,
  or a candidate list — only the conversation thread and the current
  round's own `target_complete_when` bar.

## Public surface
- `prune_for_judgment(resume, coverage, previous_status) -> (already_decided, to_judge)`
  — partitions every leaf: **CARRIED** (already `SUFFICIENT`, or a terminal
  `UNABLE_TO_ANSWER`/`NOT_APPLICABLE` verdict from a prior cycle, via
  `_carry_unavailable`) short-circuits straight into `already_decided`,
  untouched. Everything else — including a block/field with **no value at
  all yet** — goes into `to_judge`, with an empty `value`/`fields_to_judge`/
  `items_to_judge` payload when there's nothing extracted so far. This is
  deliberate: an empty-but-not-yet-terminal block must still reach JOB 2
  every cycle, because the excerpt itself (not the empty `resume_data`
  payload) may contain the candidate explicitly ruling the whole block out
  (see `build_unable_to_answer_patch`'s second caller, below). Only a block
  that's already terminal stops being sent — that's the only case that
  still avoids the token cost. Called with the **full** `COVERAGE_SCHEMA`
  (not askable-only) — `personal`/`summary` are still graded for
  completeness even though never asked about through a spoken question.
- `merge_completeness(already_decided, llm_blocks, coverage) -> dict` —
  stitches carried-forward parts back together with the combined call's
  fresh `blocks` verdicts. Safe with empty `llm_blocks`, and shape-tolerant
  of a malformed one: a block, item, or verdict leaf that isn't a dict is
  skipped, never indexed.
- `merge_status_preserving_terminal(existing, incoming) -> dict` — folds a
  freshly computed `field_completeness` onto the stored one, holding back
  any leaf whose existing verdict is terminal and whose incoming one is
  not. Called only by `crud.apply_field_completeness` — the sole writer of
  `field_completeness` anywhere in the codebase.
- `build_unable_to_answer_patch(field_completeness, target) -> dict` —
  `target` is `{"block", "item_id", "fields"}` (`item_id` optional, `fields`
  an optional list of field names — plural, since one declined question
  commonly covers several fields at once). `InterviewDirector` builds this
  from the closing round's own stored `target` whenever it grades
  `UNABLE_TO_ANSWER`, and awaits the resulting `crud.apply_field_completeness`
  call **before** the flush that triggers the next candidate-queue
  computation (see "Cancellation / commit semantics" below). This is the
  path for a decline made *inside* a targeted round. A **spontaneous**
  decline made outside any round about that block (e.g. in the ungraded
  opening answer, or a tangent during some other round) is instead caught
  directly by the combined call's own JOB 2 now that `prune_for_judgment`
  keeps still-open empty blocks in `to_judge` — see
  [backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md)'s JOB 2
  `UNABLE_TO_ANSWER` verdict. Both paths land on the exact same terminal
  status through the exact same `merge_status_preserving_terminal`/
  `apply_field_completeness` writer, so nothing downstream (exclusion,
  candidate-queue computation) needs to know which one fired.
- `gap_key(block, item_id) -> str` / `compute_candidate_queue(resume,
  coverage, field_completeness, *, excluded_keys=frozenset()) -> List[dict]`
  (`next_target.py`) — see
  [backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md) for
  the full algorithm; documented there since it's a direct input to
  `run_combined_chain`.
- `_recompute_list_object_status(node, field_specs) -> dict`
  (`completeness_status.py`, private) — for a **list-object** block only
  (experience/education/projects/certifications/courses), overrides the
  block's own top-level `completeness_status` with a Python-derived
  verdict — SUFFICIENT only once **every** existing item has no open field
  left, PARTIAL otherwise — computed the same way
  `next_target._item_level_target` decides an item has nothing left to ask
  (every one of the block's own field names has a terminal status).
  `merge_completeness` calls this for every list-object block whose merged
  node has a non-empty `items` list (an empty-items block's status stays
  JOB 2's own call — see "Empty vs. populated list-object blocks" below).
  This exists because the LLM's own holistic verdict for these blocks used
  to be trusted directly, under an "at least one item is good enough" bar —
  which both mismatched what candidates actually expect (`education`
  showing SUFFICIENT while a second item was still missing basic fields)
  and, worse, was what `next_target.py` used to gate whether the block got
  looked at *at all* (see below) — so a premature aggregate SUFFICIENT
  could silently hide every other item's open fields from ever being asked
  about, forever.

## The interview loop — Python-ordered queue, LLM-worded questions
Priority ordering is decided in Python, not by the model choosing freely —
a live session under an earlier free-choice design bounced between blocks
(`projects` → `experience` → `education` → ...) instead of exhausting one
at a time. The current design:

- **`questions.queue`** (persisted on the session row — see
  [backend/database-models.md](database-models.md)) is regenerated
  wholesale every combined-call cycle: `compute_candidate_queue` builds the
  exhaustive, priority-ordered candidate list (conflicts, then unresolved
  records, then ordinary coverage gaps by `objective_priority`), the LLM is
  told to preserve that order and word a `question` for every still-open
  entry, and `combined_chain._validate_queue` re-validates + re-sorts the
  result back to `candidates`' own order afterward — belt-and-suspenders
  against the model reordering or inventing an out-of-list entry. See
  [backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md) for
  `run_combined_chain`'s exact contract.
- **`InterviewDirector` only ever pops the queue head**
  (`crud.pop_question_queue_head`) — no LLM call at pop time, since the
  question is already fully worded. An empty queue means genuinely nothing
  is left (the queue is exhaustive by construction), so the interview ends.
- **Every item of a list-object block gets its own separate question
  thread, and the block stays candidate-worthy until every item's every
  field is individually resolved.** `next_target._block_is_open` treats a
  list-object block with existing items (experience/education/projects/
  certifications/courses) as always worth looking at, regardless of that
  block's own aggregate `completeness_status` — only `_item_level_target`
  itself (scanning items in order, returning the first with an open field)
  decides there's nothing left, once every item has no open field of its
  own remaining. "Resolved" means filled in, explicitly declined
  (`UNABLE_TO_ANSWER` on that field), or given up on after the round
  targeting it hits `resume_room_max_questions_per_round` — the last case
  leaves the field's own `field_completeness` leaf non-terminal (still
  `PARTIAL`/`MISSING`) even though `questions.given_up_targets` correctly
  stops it from ever being re-asked, so `_recompute_list_object_status`
  (above) can end up reporting a block as PARTIAL indefinitely after a cap,
  even though the interview has functionally moved on — a label-accuracy
  gap, not a re-asking bug. Blocks with no items yet, or with no item
  breakdown at all (skills, achievements, awards, languages,
  additional_information), still gate purely on the block's own aggregate
  status, exactly as before.
- **Exclusion is cross-task, persisted state.** `questions.given_up_targets`
  (a gap a round capped out on while still non-terminal) and
  `questions.forced_topics_spent` (a conflict/unresolved topic whose round
  already closed) are both written by `InterviewDirector` via
  `crud.mark_target_given_up`/`crud.mark_forced_topic_spent`, and read by
  `analysis_orchestrator._current_round_key`/`_run_batch` (a different
  asyncio task) to exclude them — plus whatever round is *currently* open —
  from the next `compute_candidate_queue` call. This replaced two in-memory
  `Set`s that used to live on `InterviewDirector` itself; they had to move
  to the session row once target-selection moved into the analysis worker's
  own task, which can't see the director's instance state.
- **The narrow per-answer grading call** (`run_answer_grading_chain`) only
  grades the current round's own `target_complete_when` bar and drafts a
  same-topic probe — it has no say over what gets asked next at all. See
  [backend/stt-tts-pipeline.md](stt-tts-pipeline.md) for the full round
  state machine.

## Cancellation / commit semantics
- **Per-answer grading** (`InterviewDirector._finish_answer`) — a
  resume-speaking event cancels the in-flight task. Before
  `run_answer_grading_chain` returns, nothing has been committed, so a
  cancel there is a pure restore (`_awaiting_answer` back on, answer text
  prepended to whatever accumulated during the call). After it returns, the
  round's `record_round_answer`/`close_round` writes are individually
  shielded and deferred (they don't affect the next candidate-queue
  computation, only bookkeeping) — but `mark_target_given_up`,
  `mark_forced_topic_spent`, and the `UNABLE_TO_ANSWER`
  `apply_field_completeness` patch are all **awaited immediately**, before
  the answer-end flush is even fired. This ordering matters: those three
  writes change what the very next `compute_candidate_queue` call will see
  (an exclusion set entry, or a leaf's terminal status), and deferring them
  past the flush would let the flush-triggered combined call regenerate the
  queue before the write lands — handing the same just-declined/just-capped
  target straight back out again, exactly what this bookkeeping exists to
  prevent.
- **The combined-analysis batch** — a cancel mid-call discards the whole
  cycle; nothing is written, and the next trigger re-derives everything
  from a fresh snapshot anyway, so a discarded cycle costs nothing.
- Relies on `asyncio.Task.cancel()` being a documented no-op on an
  already-finished task — no manual "was it too late?" bookkeeping.

## Conventions & gotchas
- `COVERAGE_SCHEMA`'s block/field names must exactly match
  `RESUME_SCHEMA`'s — `completeness_status.py` looks blocks up by name
  across both and will raise on a mismatch the moment it's hit.
- The combined call's completeness half produces `PARTIAL`/`SUFFICIENT`/
  `UNABLE_TO_ANSWER` (the third added specifically for a spontaneous,
  unprompted whole-block decline — see JOB 2 in
  [backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md)).
  `MISSING` is always code-decided (the fail-soft default when a block was
  never sent, or the model omitted it from its response). `NOT_APPLICABLE`
  is produced by `prune_for_judgment` for any block/field whose
  `COVERAGE_SCHEMA` entry carries `not_applicable: true` — terminal, never
  overwritten. The combined call is prompt-instructed to never emit either
  of these two itself.
- **`UNABLE_TO_ANSWER` is the explicit-decline verdict** and is terminal,
  reachable two ways: `InterviewDirector`'s targeted-round grade (via
  `build_unable_to_answer_patch`) and the combined call's own JOB 2 (for a
  decline stated outside any round about that block). `TERMINAL_STATUSES`
  (`SUFFICIENT | UNABLE_TO_ANSWER | NOT_APPLICABLE`) is what
  `merge_status_preserving_terminal` tests against. `_carry_unavailable()`
  is what stops `prune_for_judgment` force-writing `MISSING` back over a
  decline every cycle.
- **The combined response's `blocks` field validates almost nothing** — a
  bare `{"type": "object"}` with `strict=False`, since the rubric's shape is
  dynamic and per-session. **The merge, not the schema, is the trust
  boundary** — `merge_completeness`, `_merge_items`, `_as_leaf_map`,
  `_items_by_id`, `_leaf_status` all skip non-dict nodes instead of
  indexing them (confirmed live: a session once returned bare strings where
  objects belong).
- **`field_completeness` freshness is bounded by the combined-call cadence**
  (the ~100-char buffer trigger, plus the answer-end flush), with one
  narrow director-side immediate write on top: `build_unable_to_answer_patch`
  — not a return of the old per-answer verdict model (no SUFFICIENT/PARTIAL
  write), just the one thing the combined call structurally cannot ever
  infer on its own.
- `resume_room_silence_hardbound_seconds` and `resume_room_answer_silence_seconds`
  are the director's own two silence windows (idle-recovery vs. answer-end)
  — unrelated to the completeness cadence above now that there is no
  separate silence-triggered grading sweep.

## Last synced
2026-09-05 (later still still — fixed a second live bug on the same theme:
`education` (and, by the same mechanism, any other list-object block) could
go SUFFICIENT while a second/later item still had basic fields MISSING, and
once that happened the block was never looked at again for the rest of the
session. Root cause: `next_target.compute_next_targets`'s `open_blocks`
filter gated a list-object block's ENTIRE candidate generation on that
block's own aggregate `completeness_status` — and that aggregate status
used to reflect only a loose "at least one item is good enough" bar (the
old `coverage_schema.py` wording), so ONE fully-covered item could mark the
whole block terminal and hide every other item's open fields from
`_item_level_target` forever. Fixed two ways: (1) `next_target._block_is_open`
now always considers a list-object block with existing items worth looking
at, regardless of its own aggregate status — only `_item_level_target`
itself (unchanged) decides there's nothing left, per item; (2)
`completeness_status.merge_completeness` now overrides that same aggregate
status with a Python-derived one for these blocks (`_recompute_list_object_status`)
instead of trusting the LLM's holistic call, so the exported label stays
truthful and can never again diverge from what the candidate-queue logic
actually does. `coverage_schema.py`'s `complete_when` for `experience`/
`education`/`projects` was reworded from "at least one is enough" to "every
listed item," matching. `certifications`/`courses` were already worded
per-item ("any mentioned X has...") and needed no change. See
[backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md) for the
`next_target.py`/`completeness_status.py` particulars.)
2026-09-05 (later still — fixed a live bug: a candidate's spontaneous
"I don't have any personal projects" in the ungraded opening answer wasn't
absorbed anywhere, so the standalone `projects` block re-asked it. Root
cause was two structural gaps, not a prompt-wording gap: (1) the opening
round is never graded, so `build_unable_to_answer_patch` was unreachable for
anything said there; (2) `prune_for_judgment`'s empty-block short-circuit
(`_prune_atomic_block`/`_prune_singular_block`/`_prune_list_object_block`)
sent an empty-but-still-open block straight to `MISSING` without ever
showing it to the combined call's JOB 2, and JOB 2's prompt only allowed
`SUFFICIENT`/`PARTIAL` anyway. Fixed by changing all three pruning helpers
to only short-circuit an empty block when its **existing** verdict is
already terminal — a still-open empty block now always reaches `to_judge`
(with an empty `value`/`fields_to_judge`/`items_to_judge` payload) — and
adding `UNABLE_TO_ANSWER` as JOB 2's third legal verdict, worded to the same
strict "explicit, unambiguous negative only" bar `question_prompts.py`
already used for the per-round case. `merge_completeness`/
`merge_status_preserving_terminal`/`compute_next_targets` needed zero
changes — already fully generic on `completeness_status`. See
[backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md) for the
exact JOB 2 prompt change.)
2026-09-05 (major rewrite — collapsed the separate silence-triggered
completeness worker and the per-answer next-question-drafting call into ONE
combined background LLM call (`combined_chain.run_combined_chain`, see
[backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md)):
deleted `silence_completeness_worker.py`, `completeness_chain.py`,
`completeness_prompts.py`, `run_completeness_chain`,
`run_completeness_grading_cycle`, the whole speaking-state queue plumbing
(`room_orchestrator.enqueue_speaking_state`/`_speaking_queues`), and the
`_forced_topics_spent`/`_organic_targets_given_up` in-memory sets on
`InterviewDirector` (moved to persisted `questions.given_up_targets`/
`forced_topics_spent`, written via new CRUD methods
`mark_target_given_up`/`mark_forced_topic_spent`). Added
`next_target.gap_key`/`compute_candidate_queue` (the Python-authoritative
priority list now fed to the combined call as `candidate_queue`, replacing
`InterviewDirector._pick_forced_topic`) and `coverage_schema.complete_when_for_target`.
The interview director no longer selects a target or drafts a next
question at all — it only pops the already-worded head of a
wholesale-regenerated `questions.queue`. See
[backend/stt-tts-pipeline.md](stt-tts-pipeline.md) and
[backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md) for the
full new design. Older history predating this rewrite described the
deleted batched-worker/fused-per-answer-call design in detail and has been
removed from this file; `docs/qa-flow-redesign-understanding.md` has the
full point-by-point design trail.)
