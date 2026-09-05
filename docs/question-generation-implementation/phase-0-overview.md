# Interview Follow-Up Question Generation — Implementation Overview

## What this is

Today the silence-triggered completeness pipeline
(`silence_completeness_worker.py` + friends, see
`docs/silence-detection-processing-implementation/`) grades how complete
the live-extracted resume is against `COVERAGE_SCHEMA`, producing
`MISSING`/`PARTIAL`/`SUFFICIENT` verdicts per block and field. The voice
bot's persona LLM has no access to that signal at all — it just free-talks
based on whatever the candidate says, with no notion of what's still
missing.

This feature extends the *existing* completeness LLM call so it also
writes **one** targeted follow-up question per silence cycle, and pipes
that question into the running voice bot so the persona LLM naturally
works it into its next turn. **No second LLM call is introduced anywhere**
— the question rides in the same batched call that already produces the
completeness verdicts.

## Confirmed scope and design decisions

- **Sticky focus.** Once a target (a specific block or a specific field
  inside a block) is picked, the interview keeps probing that same target
  every cycle until its verdict is `SUFFICIENT`. Only then does the next
  cycle pick a new target. This is one new piece of session state —
  `current_focus_path` — not a general interview state machine.
- **Two-tier priority, driven by a new `objective_priority` field.** Every
  top-level block in `COVERAGE_SCHEMA` gets an `objective_priority` int
  (1 = highest). When there's no sticky focus (or it just resolved),
  target selection walks blocks in that order, but **all blocks that
  already have some content are exhausted (each one's remaining
  field-level gaps) before any completely-empty block is targeted at
  all** — regardless of the empty block's own priority number. Only once
  every started block is fully `SUFFICIENT` does a completely-empty block
  become eligible, in priority order, producing a whole-block question.
- **The backend picks the target path; the LLM only writes the wording.**
  A small new pure function, `select_focus_target` (alongside the existing
  `prune_for_judgment`/`merge_completeness` in `completeness_status.py`),
  deterministically decides `target_type`/`target_path`/`complete_when`
  *before* the LLM call. The prompt tells the LLM exactly which target to
  write a question for; the LLM's only job is producing one concise,
  voice-appropriate `question` string (or `null`). This avoids ever having
  to validate an LLM-invented path, and guarantees "exactly one question,
  priority-ordered" by code rather than by hoping the LLM follows the
  rules across a whole batch.
- **Fixes a real bug in today's guard**: a **completely empty** resume
  block never reaches the LLM at all (`prune_for_judgment` short-circuits
  it straight into `already_decided`), and `run_completeness_chain`
  currently skips the network call entirely when `to_judge` is empty. That
  means on session start (everything `MISSING`), no question could ever be
  generated under today's code. The guard becomes: skip the call only when
  there's *nothing to verdict AND nothing to ask about*
  (`not to_judge and question_target is None`).
- **Question delivery is new plumbing** (the one genuinely new piece,
  beyond extending the existing call): no channel today reaches from a
  background worker into the *live* pipecat pipeline — the two existing
  workers (`run_resume_analysis_worker`, `run_silence_completeness_worker`)
  only ever call CRUD methods. A third per-session queue
  (`_question_queues`, mirroring `_queues`/`_speaking_queues` in
  `room_orchestrator.py` exactly) carries the question text into `run_bot`,
  where a small internal consumer task steers the persona LLM's own
  context (`context_llm.add_message(...)` + `LLMRunFrame()`) — the exact
  same mechanism `BotSession.greet()` already uses to seed the greeting.
  The persona LLM phrases it in its own voice; nothing bypasses
  `PERSONA_PROMPT`'s tone.
- **Cancellation/commit semantics are untouched.** The question rides in
  the *same* `asyncio.shield`-guarded `apply_field_completeness` write the
  verdict already uses — "discard if interrupted mid-LLM-call, commit
  unconditionally once the LLM has returned" now covers the question too,
  for free, with no new bookkeeping.
- **No test suite exists anywhere in this repo today** (checked — only
  third-party `.venv` tests exist). Given the new logic is pure and easy to
  misjudge (priority ordering, sticky persistence), this feature adds a
  minimal `backend/tests/` + `pytest` covering just the new pure
  functions — not a general test framework, not integration tests.

## `next_question` / `field_completeness` shape

Stored on the session row, sibling to `field_completeness`:

```json
{
  "next_question": {
    "target_type": "BLOCK",
    "target_path": "experience",
    "question": "Can you tell me about your work or internship experience — where, your role, and what you did?"
  },
  "current_focus_path": "experience"
}
```

or, once a block has some content and a specific field is the gap:

```json
{
  "next_question": {
    "target_type": "FIELD",
    "target_path": "experience.exp_1.responsibilities",
    "question": "What were your main responsibilities in that role?"
  },
  "current_focus_path": "experience.exp_1.responsibilities"
}
```

Both are `null` once nothing is left to ask about (a fully `SUFFICIENT`
resume) or whenever the LLM declines to produce wording for the chosen
target.

## Files touched

**Modified** (all under `backend/app/meeting_room/`, except tests/deps):
1. `resume_analysis_pipeline/config_jsons_definitions/coverage_schema.py`
   — `objective_priority` per block.
2. `resume_analysis_pipeline/completeness_status.py` — new
   `select_focus_target`/`target_status` + private helpers.
3. `resume_analysis_pipeline/completeness_prompts.py` — `question_target`
   param, question-writing instructions.
4. `resume_analysis_pipeline/completeness_chain.py` — `question_target`
   param, guard fix, `question` in the response schema.
5. `data/crud_interfaces.py` — `apply_field_completeness` Protocol
   signature gains `next_question`/`current_focus_path`.
6. `data/crud.py` — new row keys, extended method, status-export shape.
7. `resume_analysis_pipeline/silence_completeness_worker.py` — target
   selection + extended commit + delivery call, `orchestrator` param.
8. `room_orchestrator.py` — `_question_queues`, `enqueue_next_question`,
   teardown wiring, passes the queue into `run_bot`.
9. `stt_tts_pipeline/pipeline.py` — consumes `question_queue`, steers
   `context_llm`, triggers `LLMRunFrame`.
10. `backend/requirements.txt` — adds `pytest`.

**New**:
11. `backend/tests/test_completeness_question_targeting.py`

## Build order

| Phase file | What it adds | Depends on |
| --- | --- | --- |
| `phase-1-coverage-schema.md` | `objective_priority` | — |
| `phase-2-target-selection.md` | `select_focus_target`, `target_status` | Phase 1 |
| `phase-3-completeness-prompts.md` | `question_target` prompt support | Phase 1, 2 (shape) |
| `phase-4-completeness-chain.md` | `question` in the LLM call/response | Phase 3 |
| `phase-5-crud-changes.md` | `next_question`/`current_focus_path` storage | — |
| `phase-6-silence-completeness-worker.md` | Wires selection + chain + commit + delivery together | Phases 2, 4, 5 |
| `phase-7-room-orchestrator-wiring.md` | `_question_queues` + spawns/tracks/tears down | Phase 6 |
| `phase-8-pipeline-wiring.md` | Consumes the queue inside the live bot | Phase 7 |
| `phase-9-tests-and-verification.md` | Unit tests + manual verification + doc-sync reminder | Phases 2, 6, 7, 8 |

Phases 1, 5 have no dependencies on each other and can be done first, in
either order. Phases 2 → 3 → 4 → 6 → 7 → 8 form the critical path — don't
wire phase 7 into `room_orchestrator.py` until phase 6's worker actually
calls `orchestrator.enqueue_next_question`, and don't wire phase 8 into
`pipeline.py` until phase 7's queue actually exists to be passed in (same
"don't wire too early" warning the reference implementation docs give).
