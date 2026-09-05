# Silence-Triggered Completeness Grading — Implementation Overview

## What this is

Today, `resume_data` gets extracted incrementally by a 700-char-buffer
trigger (`analysis_orchestrator.py`), and a separate debug-only module,
`field_status.py`, computes a naive "field completeness" export purely by
presence: a field is `SUFFICIENT` the instant it has *any* value, `MISSING`
otherwise. It never checks whether an answer is actually good enough —
just whether something was said at all.

This feature replaces that naive signal with a real one. A static
**coverage rubric** (`COVERAGE_SCHEMA`) states, per resume block and field,
an `importance` and a natural-language `complete_when` bar. On every
**2-second silence** from the candidate, a single batched LLM call compares
the currently-extracted resume against that rubric and produces real
`MISSING` / `PARTIAL` / `SUFFICIENT` verdicts — both **per field** and
**per block** (a block's own aggregate bar, e.g. "at least one relevant
experience is sufficiently covered") — stored as real session state. If the
candidate starts speaking again before the LLM call finishes, the whole
pass is cancelled and discarded; if it already finished, the result stands
regardless of what happens after.

This is the completeness-grading half of a larger plan. **Explicitly out of
scope**: generating interview follow-up questions from `PARTIAL`/`MISSING`
verdicts. That's a separate, not-yet-designed phase that will consume this
phase's output (`field_completeness`) — nothing here should anticipate its
schema or storage.

## Confirmed scope and design decisions

- **Coverage rubric is a Python module**, not a literal `.json` file —
  `COVERAGE_SCHEMA` in `coverage_schema.py`, matching `resume_schema.py`'s
  own convention (the `config_jsons_definitions` folder's only existing
  occupant is already a `.py` file despite the folder name).
- **One single batched LLM call per silence event** — never one call per
  field, never one call per block. The call is skipped entirely if there's
  nothing to judge.
- **Only fields with a value are ever sent to the LLM.** A field with no
  extracted value at all is decided `MISSING` by plain code — no LLM call,
  no tokens spent judging emptiness.
- **Already-`SUFFICIENT` fields are also skipped on later runs.** The first
  silence event that judges a field as `SUFFICIENT` is the last one that
  spends tokens on it — later runs carry that verdict forward untouched.
  Only fields still `PARTIAL` (or never yet judged) get re-sent. A block's
  own aggregate verdict is skipped the same way once it's `SUFFICIENT` and
  nothing new has appeared under it; otherwise it's re-asked every time
  something under it changes, since the aggregate bar can only be judged
  correctly by seeing the block's current full picture.
  **Known limitation** (documented, not silently swept under): if a
  candidate later *edits* an already-`SUFFICIENT` field's value (the
  extraction pipeline's conflict-resolution flow in `merge.py` does allow
  this), the new value won't be re-validated until something else in the
  same block also needs judging. Accepted tradeoff for this phase.
- **Block-level verdicts are in scope now**, not deferred — every block
  covered by the rubric gets both its own aggregate verdict and (where
  applicable) per-field/per-item verdicts, from the same LLM call.
- **`NOT_APPLICABLE` is reserved but not produced** by this phase — the enum
  value exists for later, but nothing here ever emits it. The LLM's own
  output is only ever `PARTIAL` or `SUFFICIENT`; `MISSING` is always
  code-decided.
- **Storage**: a new `field_completeness` field on the session row,
  mutated through a new `ResumeRoomCRUD.apply_field_completeness` method —
  real session state, not a debug-only side effect like `field_status.py`
  was. The old naive `field_status.py` is retired outright (deleted): its
  only caller stops using it, and its output was the thing being replaced,
  not supplemented.
- **Speaking-state signal reuses `UserTranscriptBridge`** (it already
  imports `UserStartedSpeakingFrame` and sits at the right pipeline
  position) rather than a new dedicated bridge class.
- **A fully separate worker/task/queue** (`silence_completeness_worker.py`)
  from the existing transcript-triggered `run_resume_analysis_worker` —
  different trigger signal (speaking state, not transcript text), own
  single-in-flight-task-plus-cancel state machine.
- **Cancellation mechanism**: cancelling an `asyncio.Task` that has already
  finished is a documented no-op in Python's asyncio — this is what gives
  "discard if interrupted mid-flight, keep if already done" for free, with
  no manual "was it too late" bookkeeping. The only place needing explicit
  protection is the narrow window between "LLM call returned" and "CRUD
  write finished," guarded with `asyncio.shield(...)` around just that call.

## Files touched

**New files** (all under `backend/app/meeting_room/`):
1. `resume_analysis_pipeline/config_jsons_definitions/coverage_schema.py`
2. `resume_analysis_pipeline/completeness_status.py`
3. `resume_analysis_pipeline/completeness_prompts.py`
4. `resume_analysis_pipeline/completeness_chain.py`
5. `resume_analysis_pipeline/silence_completeness_worker.py`

**Modified files**:
6. `data/crud_interfaces.py` — new `apply_field_completeness` Protocol method
7. `data/crud.py` — new `field_completeness` row field, new mutation method,
   debug-export switched to dump real state instead of recomputing the
   naive one, `field_status` import dropped
8. `core/config.py` — new `resume_room_completeness_*` and
   `resume_room_silence_hardbound_seconds` settings
9. `stt_tts_pipeline/processors/bridges.py` — `UserStoppedSpeakingFrame`
   handling + `on_speaking_change` callback on `UserTranscriptBridge`
10. `stt_tts_pipeline/pipeline.py` — wires the callback to
    `orchestrator.enqueue_speaking_state`
11. `room_orchestrator.py` — new speaking-state queue, spawn/track/tear
    down the new worker, mirroring the existing transcript-queue handling

**Deleted file**:
12. `resume_analysis_pipeline/field_status.py` — fully superseded (see
    `phase-11-cleanup-and-verification.md`)

## Build order

| Phase file | What it adds | Depends on |
| --- | --- | --- |
| `phase-1-coverage-schema.md` | `COVERAGE_SCHEMA` | — |
| `phase-2-completeness-status.md` | `prune_for_judgment`, `merge_completeness` | Phase 1 |
| `phase-3-completeness-prompts.md` | System prompt + user-prompt builder | Phase 1 |
| `phase-4-completeness-chain.md` | The LLM call itself | Phase 3 |
| `phase-5-crud-changes.md` | `field_completeness` storage + `apply_field_completeness` | Phase 2 |
| `phase-6-config.md` | New settings | — |
| `phase-7-speaking-state-bridge.md` | `UserStoppedSpeakingFrame` + callback | — |
| `phase-8-speaking-state-pipeline-wiring.md` | Wires the callback into `run_bot` | Phase 7 |
| `phase-9-silence-completeness-worker.md` | Debounce/cancel/shielded-commit worker | Phases 2, 4, 5, 6 |
| `phase-10-room-orchestrator-wiring.md` | Spawns/tracks/tears down the worker | Phases 8, 9 |
| `phase-11-cleanup-and-verification.md` | Deletes `field_status.py`; manual verification | Phases 5, 10 |

Phases 1, 3, 6, 7 have no dependencies on each other and can be typed in any
order first. Phases 2 → 4 → 9 → 10 form the critical path — don't wire
phase 10 into `room_orchestrator.py` until phase 9's module actually exists,
same warning the extraction-implementation docs give about wiring too
early.
