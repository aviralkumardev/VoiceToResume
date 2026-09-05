# Backend: Resume Analysis Pipeline

## Purpose
Turns the candidate's live transcript into a structured resume document,
grades how complete it is against a coverage rubric, and regenerates the
interview's upcoming-question queue — all in **one combined background LLM
call** per trigger. Runs as a background worker per session: batches
incoming transcript text, periodically (or on an answer-end flush) runs one
combined-analysis batch, and runs one final "resolution" LLM pass over the
whole transcript when the session ends to clean up anything left
ambiguous.

Separately, one small, narrow LLM call handles the interview's per-answer
grading (`question_chain.run_answer_grading_chain`) — it grades only the
current round's own bar and drafts a probe; it has no say over what gets
asked next, since that's entirely the combined call's job now.

## Key files
- `analysis_orchestrator.py` — `run_resume_analysis_worker()`, the
  batching/trigger loop; `_run_batch()`, the combined-call cycle.
- `combined_chain.py` + `combined_prompts.py` — `run_combined_chain()`, the
  ONE background LLM call: extraction + completeness grading + queue
  wording, one schema, one response.
- `analysis_chain.py` + `analysis_prompts.py` — trimmed to only
  `run_resume_final_resolution_chain()`, the session-end pass over the
  whole transcript. Extraction (`run_resume_extraction_chain`) used to live
  here; it's gone, fused into `combined_chain.py`.
- `question_chain.py` + `question_prompts.py` — the interview director's
  narrow per-answer grading chain, `run_answer_grading_chain`. Physically
  lives in this directory (not `stt_tts_pipeline/`) even though it drives
  the interview loop, because it's an LLM chain alongside the pipeline's
  other ones.
- `next_target.py` — `compute_next_targets` (unchanged internal scanner)
  plus `gap_key`/`compute_candidate_queue`, the Python-authoritative
  priority-ordered candidate list handed to the combined call.
- `merge.py` — pure merge/conflict logic applied to the in-memory resume
  document.
- `config_jsons_definitions/resume_schema.py` — `RESUME_SCHEMA`, the
  canonical resume shape. `config_jsons_definitions/coverage_schema.py` —
  `COVERAGE_SCHEMA`/`ASKABLE_COVERAGE_SCHEMA`/`complete_when_for_target`,
  documented in full in
  [backend/completeness-pipeline.md](completeness-pipeline.md).

## Public surface
- `run_resume_analysis_worker(session_id, queue, crud)` (async, run as an
  `asyncio.Task` by the orchestrator) — consumes transcript chunks off
  `queue` until it receives `None` (session end sentinel), accumulating
  text until `resume_room_extraction_trigger_chars` is reached, then
  running one combined-analysis batch (`_run_batch`). On `None`, runs the
  final resolution pass instead and returns.

  A third input, `FlushRequest` (an `asyncio.Event`-carrying sentinel
  distinct from `None`), forces one batch on whatever's accumulated
  regardless of the char trigger, then sets the event and `continue`s the
  loop — a same-tick no-op if nothing has accumulated. `InterviewDirector`
  fires this unconditionally at the end of every answer (`wait=False` off
  the critical path, then `wait=True` right before it needs the queue's
  result — see [backend/stt-tts-pipeline.md](stt-tts-pipeline.md)). Stale
  `remaining_text` alone does NOT trigger a flush batch.

- **`run_combined_chain(resume, coverage, to_judge, candidate_queue, new_text) -> dict`**
  (`combined_chain.py`) — the ONE background LLM call per trigger. Fuses
  three jobs in one response:
  1. **Extraction** — pulls any resume facts `new_text` supports, same
     semantics as the old dedicated extraction chain: `updates`,
     `unresolved`, `resolved_conflicts`, `resolved_unresolved_ids`,
     `remaining_text`, `status`.
  2. **Completeness grading** — grades whatever `to_judge` (from
     `completeness_status.prune_for_judgment(resume, COVERAGE_SCHEMA,
     field_completeness)`) currently needs a fresh verdict: `blocks`, keyed
     the same way the old dedicated completeness chain's response was.
     `prune_for_judgment` only short-circuits a block away from `to_judge`
     once its verdict is already terminal — a still-open block with zero
     extracted data is still sent, with an empty `value`/`fields_to_judge`/
     `items_to_judge` payload, so the model can grade it against the raw
     excerpt rather than never seeing it at all. The legal verdict set is
     `SUFFICIENT`/`PARTIAL`/**`UNABLE_TO_ANSWER`** — the third one added
     specifically so a candidate's own spontaneous, unprompted decline of a
     block's entire subject matter ("I don't have any personal projects,"
     said outside any round about `projects`) can be caught here, the one
     place that sees every excerpt regardless of which round is open. Bar
     for using it mirrors `question_prompts.py`'s existing per-round
     `UNABLE_TO_ANSWER` wording near-verbatim: an explicit, unambiguous
     negative only — never inferred from a block simply having no items yet.
     `MISSING`/`NOT_APPLICABLE` remain explicitly out of scope, decided only
     by `prune_for_judgment` itself. See
     [backend/completeness-pipeline.md](completeness-pipeline.md) for how
     this interacts with `build_unable_to_answer_patch`'s existing in-round
     path — same terminal status, same merge, two independent producers.
  3. **Queue wording** — for every still-open entry of `candidate_queue`,
     words a spoken `question`: `queue: [{"key", "question"}, ...]`. Every
     worded question follows a fixed shape — a short, rotated, generic
     acknowledgment (never repeating the immediately preceding one, nor the
     one baked into `last_asked_question`, and never referencing what
     section just closed or how well it went), optional natural-language
     framing of the section being opened (never the raw schema key, never
     the word "section"), then the question body itself (direct/broad if
     nothing's captured yet for that gap, or a direct reference to what's
     already known plus only the still-open fields otherwise). Repeatable
     blocks (experience/education/projects/certifications/courses) with more
     than one named item must be scanned in full — not just the
     latest-mentioned one — and the least-covered item addressed first; a
     block with only one named item so far may, once per session per block,
     fold in a brief "any other X?" check, tracked via
     `more_items_checked`/`more_items_asked` (see below) so it's never asked
     twice. See `combined_prompts.SYSTEM_PROMPT`'s JOB 3 for the full rule
     set (block/field collision, name-the-specific-item,
     consolidate-don't-drip-feed, plus the above).

  `coverage` must be the **full** `COVERAGE_SCHEMA` (not askable-only) —
  `to_judge` can legitimately include `personal`/`summary`, which are
  graded for completeness even though never asked about through a spoken
  question. `candidate_queue` (built by the caller via
  `next_target.compute_candidate_queue`, always with
  `ASKABLE_COVERAGE_SCHEMA`) already carries each candidate's own
  `complete_when` baked in, so this call never needs a second coverage
  lookup for wording.

  Routed through `OpenAIProvider` (Responses API), not `OpenRouterProvider`
  — own settings `resume_room_combined_provider` (default `"openai"`),
  `resume_room_combined_model` (default `"gpt-5.6-terra"`),
  `resume_room_combined_max_tokens`, `resume_room_combined_reasoning_effort`
  (default `"none"`, passed explicitly to `OpenAIProvider`'s constructor
  rather than left to its own fallback, which otherwise defaults to
  `resume_room_question_reasoning_effort` — this chain needed its own
  independent knob). Only `run_resume_final_resolution_chain` (the
  session-end pass) still uses `OpenRouterProvider`, via
  `resume_room_final_pass_*`.

  **Fail-soft contract, distinguishing a failed cycle from a finished
  interview:** on any provider/schema error, `_empty_result()` returns
  `queue: None` (alongside the existing fail-soft extraction/completeness
  defaults) — `None` means "this cycle produced no judgment on the queue at
  all, leave the persisted queue untouched"; a genuinely successful
  response with nothing left open returns `queue: []`. The caller
  (`_run_batch`, below) only calls `crud.apply_question_queue` when
  `result["queue"] is not None`.

  `combined_chain._validate_queue(returned_queue, candidates) -> List[dict]`
  is the trust boundary for the model's self-reported `queue`: keeps only
  entries whose `key` matches a given candidate and whose `question` is a
  non-empty string, dedupes by key, and **re-sorts to `candidates`' own
  given order** — the actual enforcement of "Python decides priority, the
  LLM just words it," not merely a containment check.

  `run_combined_chain` also takes `last_asked_question: Optional[str]` and
  `more_items_checked: Optional[List[str]]` (both passed straight from
  `session["questions"]`) — pure wording-context for JOB 3, never touching
  extraction/completeness: the exact text of the most recently spoken
  question (so its acknowledgment isn't reused) and which repeatable blocks
  have already had their one-time "any other X?" side-question asked (so it
  isn't asked again). The response gains a matching `more_items_asked:
  List[str]` — block names the model appended that side-question to this
  cycle — sanitized by `combined_chain._validate_more_items_asked` (kept
  distinct non-empty strings only) the same way `_validate_queue` sanitizes
  `queue`. `_run_batch` (below) persists it via
  `crud.mark_more_items_checked` whenever non-empty.

  `combined_prompts.py`'s `SYSTEM_PROMPT` fuses the old extraction rules,
  completeness-grading rules, and queue-wording rules (block/field
  collision, name-the-specific-item, consolidate-don't-drip-feed — see
  below) generalized from one `next_question` to the whole
  `candidate_queue`. `build_combined_user_prompt(resume, coverage, to_judge,
  candidate_queue, new_text) -> str` renders all of it as one payload. See
  [backend/app-config.md](app-config.md) for its settings (listed above).

- **`run_answer_grading_chain(conversation_history, answer_text, target_complete_when) -> dict`**
  (`question_chain.py`) — the single LLM call the interview director makes
  per answer turn. Given the whole conversation so far (every round's
  exchanges flattened, oldest first) and ONLY the current round's own
  `target_complete_when` bar (a `str`, or a `List[str]` for a multi-field
  target — see `coverage_schema.complete_when_for_target`), it: detects a
  meta/process question first (`is_meta_question`/`meta_response`); grades
  the last answer (`answer_grade`: `PARTIAL`/`SUFFICIENT`/`UNABLE_TO_ANSWER`,
  `ANSWER_GRADE_*`/`TERMINAL_GRADES` constants — a distinct, narrower
  concept than `completeness_status.TERMINAL_STATUSES`, since
  `NOT_APPLICABLE` has no meaning for a single graded answer); and drafts a
  `probe_question` if the grade stays open. **No `next_question` of any
  kind** — deciding what to ask about next is not this call's job at all,
  that's entirely `run_combined_chain`'s `queue`. Fail-soft:
  `_empty_result()` on an empty `answer_text` or a provider/schema error
  defaults to `answer_grade=PARTIAL` — "nothing usable" always means "still
  open," never "done." Own settings, unchanged: `resume_room_question_provider`
  (default `"openai"`), `resume_room_question_model`,
  `resume_room_question_max_tokens`, `resume_room_question_reasoning_effort`
  — routed through `OpenAIProvider`, not `OpenRouterProvider`; see
  [backend/llm-providers.md](llm-providers.md).

  Three prompt rules still govern `probe_question`/queue wording, carried
  forward from the old fused call: (1) the **block/field collision rule** —
  `experience`'s own `projects`/`achievements`/`awards` fields (scoped to
  one job) vs. the top-level standalone blocks of the same name; (2) the
  **name-the-specific-item rule** — a repeatable block with more than one
  item must name the specific item (role/company, degree/college, item
  name) in the question; (3) the **consolidate, don't drip-feed rule** — a
  single question must cover every currently-open field of a targeted
  item/block at once, never split across separate rounds.

- **`gap_key(block, item_id) -> str`** / **`compute_candidate_queue(resume,
  coverage, field_completeness, *, excluded_keys=frozenset()) -> List[dict]`**
  (`next_target.py`) — the unified priority-candidate-list builder. Builds,
  in order: outstanding conflicts (`resume["conflicts"]`, insertion order,
  key `f"conflict:{id}"`), outstanding unresolved records
  (`resume["unresolved"]`, insertion order, key `f"unresolved:{id}"`), then
  ordinary coverage gaps via `compute_next_targets` (touched blocks before
  untouched, each by ascending `objective_priority`, key via `gap_key`).
  Each item: `{"kind": "conflict"|"unresolved"|"gap", "key", "block",
  "item_id", "fields", "complete_when"}` (plus the raw record for
  `conflict`/`unresolved`, so the combined call can word a question without
  a separate topic-wording call). `excluded_keys` filters out anything
  already given up on or already spent (see
  [backend/completeness-pipeline.md](completeness-pipeline.md)) or whose
  round is currently open. Returns the **full** list, never truncated — an
  empty list means every askable block is genuinely covered, given no
  exclusions. Called from `_run_batch` (below) with
  `ASKABLE_COVERAGE_SCHEMA`.

  `compute_next_targets`'s own `open_blocks` filter uses `_block_is_open`,
  not a bare aggregate-status check: for a list-object block with existing
  items (experience/education/projects/certifications/courses), the block
  is always considered open — its own aggregate `completeness_status` is
  never trusted to mean "nothing left in here," since that status used to
  reflect only a loose "at least one item is good enough" bar and could
  hide every OTHER item's open fields from ever surfacing again once ONE
  item satisfied it. `_item_level_target` (unchanged) is what actually
  decides per item whether there's an open target, scanning items in the
  given order and returning the first with any non-terminal field. A block
  with no items yet, or with no item breakdown at all, still gates on its
  own aggregate status exactly as before. See
  [backend/completeness-pipeline.md](completeness-pipeline.md) for
  `_recompute_list_object_status`, the matching fix on the completeness
  side that keeps the exported aggregate label truthful now that it's no
  longer what decides candidate generation.

- `run_resume_final_resolution_chain(resume, full_transcript) -> dict` —
  one LLM call over the *entire* user transcript at session end; returns
  `{reasoning, updates, _llm_usage}`. Unchanged.
- `merge_updates(resume, updates, *, force_overwrite=False) -> (resume,
  accepted, rejected)` — applies an `updates` payload to the resume dict in
  place, per-block-kind (`singular`/`singular_freeform`/`list_object`/
  `list_string`), diverting genuine conflicts into `resume["conflicts"]`
  unless `force_overwrite=True` (used by the final pass, which also
  replaces item array fields wholesale instead of appending).
- `merge_unresolved`, `apply_resolved_conflicts`, `remove_unresolved` —
  manage `resume["unresolved"]`/`resume["conflicts"]`. A record's presence
  is what keeps its candidate-queue entry alive; its removal (by the
  combined call's extraction half) is what retires it, though
  `questions.forced_topics_spent` (see
  [backend/completeness-pipeline.md](completeness-pipeline.md)) is what
  actually stops it being re-forced once its round has closed, since
  extraction clearing the record can lag a cycle behind. Record shapes: a
  conflict is `{id, block, field, item_id, existing_value, candidates[]}`,
  an unresolved item is `{id, block, text, note}`.
- `is_redundant_with_accepted_update(text, updates, accepted, *,
  min_shared_tokens=3) -> bool` (`merge.py`) — token-overlap guard against
  extraction dual-attributing the same fact to both `updates` and
  `unresolved` in one response. `crud.apply_resume_update` calls this to
  filter `unresolved` before `merge_unresolved` runs.
- `RESUME_SCHEMA`, `empty_resume()`, `block_kind()`, `render_schema_for_prompt()`
  — schema introspection used by `merge.py` and the prompt builders.

## `_run_batch` — the combined-analysis cycle
```python
async def _run_batch(session_id, accumulated_text, remaining_text, crud) -> str:
    input_text = remaining_text + accumulated_text
    session = await crud.get_session(session_id)
    resume, field_completeness, questions = session's own fields

    already_decided, to_judge = prune_for_judgment(resume, COVERAGE_SCHEMA, field_completeness)

    excluded = given_up_targets | forced_topics_spent | {current round's own key, if any}
    candidates = compute_candidate_queue(resume, ASKABLE_COVERAGE_SCHEMA, field_completeness, excluded_keys=excluded)

    result = await run_combined_chain(
        resume, COVERAGE_SCHEMA, to_judge, candidates, input_text,
        last_asked_question=questions["last_asked_question"],
        more_items_checked=questions["more_items_checked"],
    )

    apply_resume_update(...); merge + apply_field_completeness(...)
    if result["queue"] is not None:
        apply_question_queue(session_id, result["queue"])
    if result["more_items_asked"]:
        mark_more_items_checked(session_id, result["more_items_asked"])

    return remaining_text (carried forward, capped)
```
`_current_round_key(questions)` — reads `questions.current_round_id` from
the row (not the director's in-memory state, which lives in a different
asyncio task), and derives that round's own candidate key: its
`forced_topic` if set, else `gap_key(target.block, target.item_id)` if a
`target` is set, else `None` for the opening round. This is the
analysis-worker-side replacement for exclusion logic that used to live
inline in `InterviewDirector`. A round whose `close_round` write hasn't
landed yet when a cycle runs is over-excluded for one extra cycle
(conservative, never a correctness bug) — see
[backend/completeness-pipeline.md](completeness-pipeline.md)'s
cancellation section for why that's safe.

## Data flow & dependencies
- Consumes the transcript queue fed by `room_orchestrator.enqueue_transcript`
  (only user/candidate lines), carrying plain strings. See
  [backend/stt-tts-pipeline.md](stt-tts-pipeline.md) for the producer side.
- Calls into [backend/llm-providers.md](llm-providers.md) for `run_combined_chain`
  (`resume_room_combined_*`, `OpenAIProvider`), `run_answer_grading_chain`
  (`resume_room_question_*`, `OpenAIProvider`), and
  `run_resume_final_resolution_chain` (`resume_room_final_pass_*`,
  `OpenRouterProvider`) — three separate provider/model pairs, cached
  separately per `(provider, model)` key. Only the final-resolution pass
  still goes through OpenRouter.
- Writes results back through `ResumeRoomCRUD.apply_resume_update`/
  `apply_field_completeness`/`apply_question_queue`/`apply_final_resolution`
  — see [backend/database-models.md](database-models.md).
- `merge.py` is pure/synchronous, no I/O — safe to unit test in isolation
  against `RESUME_SCHEMA`.

## Conventions & gotchas
- `_safe_run_batch`/`_safe_run_final_pass` swallow **all** exceptions from
  their inner call — a single bad cycle never crashes the worker or drops
  the session. On failure, `_cap_carry` reconstructs a bounded
  `remaining_text` so partial text isn't silently discarded.
- `RESUME_SCHEMA` block kinds each have distinct merge semantics — read
  `resume_schema.py`'s top-of-file comment before adding/changing a block.
- Placeholder values ("Not specified", "N/A", etc., `_PLACEHOLDER_VALUES`
  in `merge.py`) are discarded rather than stored.
- `_set_or_conflict`: re-submitting the same value is a silent no-op; a
  differing value diverts to `resume["conflicts"]` unless
  `force_overwrite=True` (final-resolution pass only).
- **`merge_updates` monotonicity is load-bearing** for the same reasons as
  before — a block's data can only grow during a live session except the
  final pass's deliberate array-replace.
- **Item array fields must REPLACE on the final pass, never append** — the
  final pass is an ATS-style *rewrite* of the same bullets;
  `FINAL_RESOLUTION_SYSTEM_PROMPT` states this contract explicitly.
- **Extraction must not dual-attribute a fact** across blocks —
  `combined_prompts.SYSTEM_PROMPT` treats cross-block ambiguity the same as
  intra-block ambiguity, prohibits attributing one fact to both `updates`
  and `unresolved`, and warns against defaulting an ambiguous fact onto an
  already-populated block. `is_redundant_with_accepted_update` is the
  Python-side backstop.
- The combined response's schema validates the extraction/queue halves
  fairly strictly but `blocks` (completeness) almost not at all — see
  [backend/completeness-pipeline.md](completeness-pipeline.md)'s gotcha on
  why the merge, not the schema, is the trust boundary there.

## Last synced
2026-09-05 (later still — expanded JOB 3's question-wording rules per the
new question-generation-guidelines doc: acknowledgment must rotate (never
repeat the immediately preceding one or the one in `last_asked_question`)
and must never characterize or reference the section that just closed;
section framing must use natural language, never the raw schema key or the
word "section"; internal field/key names must never be spoken aloud;
question bodies branch on whether anything's captured yet for that gap
(broad opener vs. reference-and-ask-only-what's-open); repeatable blocks
with multiple named items must be scanned in full (not just the latest) and
the least-covered one addressed first; a block with only one named item may
get a one-time "any other X?" side-question. Two new pieces of session
state support this: `questions.last_asked_question` (stamped by
`start_round`/`append_round_question`) and `questions.more_items_checked`
(written via the new `crud.mark_more_items_checked`, fed by the combined
call's own new `more_items_asked` response field, sanitized by
`combined_chain._validate_more_items_asked`). Neither new input/output
touches extraction or completeness grading — wording-only. See
[backend/database-models.md](database-models.md) for the state shape.)
2026-09-05 (later still still — fixed a second live bug on the same theme:
a list-object block (education/experience/projects/certifications/courses)
could go `SUFFICIENT` while a second/later item still had basic fields
`MISSING`, and once that happened the block was never looked at again for
the rest of the session. Root cause: `next_target.compute_next_targets`'s
`open_blocks` filter gated a list-object block's ENTIRE candidate
generation on that block's own aggregate `completeness_status`, and that
aggregate used to reflect only a loose "at least one item is good enough"
bar (the old `coverage_schema.py` wording) — so ONE fully-covered item could
mark the whole block terminal and hide every other item's open fields from
`_item_level_target` forever. Fixed two ways: (1) `next_target._block_is_open`
now always treats a list-object block with existing items as open,
regardless of its own aggregate status — `_item_level_target` (unchanged)
is what actually decides per item whether anything's left; (2) JOB 2's
own aggregate verdict for these blocks is no longer trusted as exported
truth — `completeness_status.merge_completeness` now overrides it with a
Python-derived one (`_recompute_list_object_status`) so the label can
never again diverge from what candidate generation actually does. Updated
`coverage_schema.py`'s `complete_when` for `experience`/`education`/
`projects` from "at least one is enough" to "every listed item," matching;
`certifications`/`courses` were already worded per-item and needed no
change. Known residual gap, not fixed here: a field that hits the
per-round question cap (`mark_target_given_up`) stays labeled non-terminal
in `field_completeness` even though `questions.given_up_targets` already
correctly excludes it from re-asking — a label-accuracy issue only, since
behavior (asking/stopping) is unaffected. See
[backend/completeness-pipeline.md](completeness-pipeline.md) for the
`_block_is_open`/`_recompute_list_object_status` particulars.)
2026-09-05 (later still — fixed a live bug where a candidate's spontaneous
whole-block decline in an ungraded turn, e.g. "I don't have any personal
projects" in the opening answer, went unrecorded and got re-asked later.
`completeness_status`'s per-block pruning helpers
(`_prune_atomic_block`/`_prune_singular_block`/`_prune_list_object_block`)
now only short-circuit an empty block away from `to_judge` when its
existing verdict is already terminal — previously any empty block was
short-circuited to `MISSING` unconditionally, so JOB 2 never even saw it.
JOB 2's prompt gained a third legal verdict, `UNABLE_TO_ANSWER`, for exactly
this case. No changes needed to `combined_chain.py`, `merge_completeness`,
or anything in `next_target.py` — `UNABLE_TO_ANSWER` was already a
first-class terminal status throughout that plumbing, just previously only
reachable via `InterviewDirector`'s in-round grading. See
[backend/completeness-pipeline.md](completeness-pipeline.md) for the full
before/after.)
2026-09-05 (later same day — moved `run_combined_chain` off `OpenRouterProvider`
onto `OpenAIProvider`: `resume_room_combined_provider`/`_model` are now
`"openai"`/`"gpt-5.6-terra"` (were `"openrouter"`/`"openai/gpt-5.6-terra:nitro"`),
and a new `resume_room_combined_reasoning_effort` setting (default `"none"`)
is passed explicitly to `OpenAIProvider`'s constructor in
`combined_chain._get_provider()` — explicit, rather than relying on
`OpenAIProvider`'s own fallback (which otherwise defaults to
`resume_room_question_reasoning_effort`), so this chain has an independent
reasoning-effort knob instead of silently inheriting the question chain's.
`run_resume_final_resolution_chain` is the only chain left on
`OpenRouterProvider`. See [backend/app-config.md](app-config.md) and
[backend/llm-providers.md](llm-providers.md).)
2026-09-05 (major rewrite — collapsed extraction + completeness grading +
next-question wording into ONE combined background call
(`combined_chain.run_combined_chain`), triggered by the same buffer/flush
mechanism extraction already used. Deleted `run_resume_extraction_chain`
(`analysis_chain.py` now holds only the final-resolution pass),
`run_completeness_chain`/`completeness_chain.py`/`completeness_prompts.py`,
`run_topic_question_chain`, and `question_chain._validate_next_target`
(nothing left to validate — there's no self-reported `next_question_target`
any more). `question_chain.run_question_chain` renamed
`run_answer_grading_chain` and shrunk to three inputs
(`conversation_history`, `answer_text`, `target_complete_when`) — no more
`resume`/`coverage`/`field_completeness`/`current_target`/
`next_target_candidates`. Added `next_target.gap_key`/`compute_candidate_queue`
as the new unified priority-list builder (conflicts → unresolved → gaps),
consumed by `run_combined_chain` as `candidate_queue` and validated/re-sorted
back by `combined_chain._validate_queue`. `analysis_orchestrator._run_batch`
now does the priority-queue computation and persists the result via the new
`crud.apply_question_queue`, replacing the old post-extraction-batch
"maybe fire a completeness cycle" trigger entirely. See
[backend/stt-tts-pipeline.md](stt-tts-pipeline.md) and
[backend/completeness-pipeline.md](completeness-pipeline.md) for the
director- and completeness-side halves of this change. Older history
predating this rewrite described the deleted fused-per-answer-call/
free-target-selection design in detail and has been removed from this
file; `docs/qa-flow-redesign-understanding.md` has the full design trail.)
