# Phase 9 — Tests and Verification

## What this does

Two things:

1. A small, self-contained `pytest` suite for `select_focus_target`/
   `target_status` (`phase-2`) — the one piece of this feature that's pure
   Python with no `asyncio`/network/CRUD involved, so it's the only part
   worth unit-testing here (this repo has no automated test suite today;
   everything else in this feature is exercised the same way the rest of
   the codebase is — manually, end-to-end, per `CLAUDE.md`'s "Testing
   approach" convention). The suite uses a small custom 3-block coverage
   fixture rather than the real `COVERAGE_SCHEMA`, but real block names
   (`experience`, `summary`, `skills`) so `block_kind(...)` lookups inside
   `completeness_status.py` still resolve against the real
   `RESUME_SCHEMA` — this keeps each test case compact and easy to verify
   by eye, while still exercising the actual kind-dispatch logic.
2. Manual end-to-end verification steps for everything the unit tests
   can't reach — the live silence/interruption timing, the CRUD write,
   and the voice bot actually asking the question out loud — plus a
   closing reminder of which `.claude/backend/*.md` domain docs need
   updating once this feature is actually implemented in source, per
   `CLAUDE.md`'s maintenance protocol (not done in this doc set, since no
   source file has been touched — only new files under `docs/`).

## New file: `backend/requirements.txt`

Add one new line (anywhere in the file; alphabetical placement matches
the existing style):

```
pytest
```

## New file: `backend/tests/__init__.py`

Empty file — makes `backend/tests` a package so `pytest`'s default
rootdir/import-mode discovery works the same way it does for any other
`backend/app/...` package in this repo.

```python
```

## New file: `backend/tests/test_completeness_question_targeting.py`

```python
from app.meeting_room.resume_analysis_pipeline.completeness_status import (
    STATUS_MISSING,
    STATUS_PARTIAL,
    STATUS_SUFFICIENT,
    select_focus_target,
    target_status,
)

# A small, hand-built coverage fixture -- NOT the real COVERAGE_SCHEMA.
# Uses real block names (experience/summary/skills) so block_kind(...)
# inside completeness_status.py resolves against the real RESUME_SCHEMA
# (experience=list_object, summary=singular, skills=list_string), while
# keeping the fields/priorities small and easy to hand-verify per test.
COVERAGE = {
    "experience": {
        "objective_priority": 1,
        "complete_when": "At least one experience is sufficiently covered.",
        "fields": {
            "company": {"importance": "required", "complete_when": "Employer name captured."},
            "responsibilities": {"importance": "required", "complete_when": "Responsibilities described."},
        },
    },
    "summary": {
        "objective_priority": 2,
        "complete_when": "A short professional summary is captured.",
        "fields": {
            "text": {"importance": "required", "complete_when": "Summary text is a real paragraph."},
        },
    },
    "skills": {
        "objective_priority": 3,
        "complete_when": "A meaningful skills list is captured.",
    },
}


def empty_resume():
    return {"experience": [], "summary": {}, "skills": []}


def test_fully_empty_resume_targets_highest_priority_block():
    target = select_focus_target(empty_resume(), COVERAGE, {}, None)

    assert target == {
        "target_type": "BLOCK",
        "target_path": "experience",
        "complete_when": COVERAGE["experience"]["complete_when"],
    }


def test_started_block_beats_empty_block_regardless_of_priority():
    resume = empty_resume()
    resume["skills"] = ["Python"]  # priority 3 -- lower than experience's 1

    target = select_focus_target(resume, COVERAGE, {}, None)

    assert target["target_type"] == "BLOCK"
    assert target["target_path"] == "skills"


def test_field_with_no_value_wins_over_a_field_awaiting_grading():
    resume = empty_resume()
    resume["experience"] = [{"id": "exp_1", "company": {"value": "Acme"}}]
    # "company" has a value (just not yet graded this round); "responsibilities"
    # has no value at all -- the genuine gap should win regardless of the two
    # fields' equal (both "required") importance.

    target = select_focus_target(resume, COVERAGE, {}, None)

    assert target == {
        "target_type": "FIELD",
        "target_path": "experience.exp_1.responsibilities",
        "complete_when": COVERAGE["experience"]["fields"]["responsibilities"]["complete_when"],
    }


def test_sticky_focus_persists_until_sufficient_then_advances():
    resume = empty_resume()
    resume["summary"]["text"] = {"value": "A short summary."}

    previous_status = {}
    target = select_focus_target(resume, COVERAGE, previous_status, None)
    assert target["target_path"] == "summary.text"

    # Nothing else changed and it's still not graded SUFFICIENT -- sticky
    # focus keeps returning the same target rather than re-picking.
    same_target = select_focus_target(resume, COVERAGE, previous_status, "summary.text")
    assert same_target["target_path"] == "summary.text"

    # Now simulate the completeness LLM having judged it SUFFICIENT.
    previous_status = {
        "summary": {
            "completeness_status": STATUS_SUFFICIENT,
            "fields": {"text": {"completeness_status": STATUS_SUFFICIENT}},
        }
    }
    next_target = select_focus_target(resume, COVERAGE, previous_status, "summary.text")

    # summary is fully resolved and nothing else has content -- falls
    # through to the highest-priority completely empty block.
    assert next_target == {
        "target_type": "BLOCK",
        "target_path": "experience",
        "complete_when": COVERAGE["experience"]["complete_when"],
    }


def test_fully_sufficient_resume_returns_none():
    resume = {
        "experience": [{"id": "exp_1", "company": {"value": "Acme"}, "responsibilities": {"value": "Built things"}}],
        "summary": {"text": {"value": "A short summary."}},
        "skills": ["Python", "SQL"],
    }
    previous_status = {
        "experience": {
            "completeness_status": STATUS_SUFFICIENT,
            "items": [{
                "id": "exp_1",
                "fields": {
                    "company": {"completeness_status": STATUS_SUFFICIENT},
                    "responsibilities": {"completeness_status": STATUS_SUFFICIENT},
                },
            }],
        },
        "summary": {
            "completeness_status": STATUS_SUFFICIENT,
            "fields": {"text": {"completeness_status": STATUS_SUFFICIENT}},
        },
        "skills": {"completeness_status": STATUS_SUFFICIENT},
    }

    assert select_focus_target(resume, COVERAGE, previous_status, None) is None


def test_target_status_defaults_to_missing_for_unknown_paths():
    assert target_status("experience", {}) == STATUS_MISSING
    assert target_status("experience.exp_1.company", {}) == STATUS_MISSING


def test_target_status_reads_item_field_status():
    status_dict = {
        "experience": {
            "items": [{"id": "exp_1", "fields": {"company": {"completeness_status": STATUS_PARTIAL}}}],
        },
    }

    assert target_status("experience.exp_1.company", status_dict) == STATUS_PARTIAL
```

Run with:

```
cd backend
pip install -r requirements.txt
pytest tests/test_completeness_question_targeting.py -v
```

## Key design points, explained

- **A custom 3-block coverage fixture, not the real `COVERAGE_SCHEMA`.**
  `select_focus_target`/`_first_open_target` are pure functions of
  whatever `coverage` dict they're handed — the algorithm doesn't care how
  many blocks are in it. Using a small fixture keeps every test case
  hand-verifiable in a few lines (no need to enumerate all 12 real blocks'
  content/status to build a "fully sufficient" case) and decouples these
  tests from `COVERAGE_SCHEMA`'s exact field list changing independently
  of the selection algorithm itself. `block_kind(...)` still resolves
  against the real `RESUME_SCHEMA`, though, so the fixture reuses real
  block names (`experience`/`summary`/`skills`) rather than inventing
  fake ones that `block_kind` wouldn't recognize.
- **`test_field_with_no_value_wins_over_a_field_awaiting_grading`
  is the one that would have caught the bug fixed in `phase-2`** — an
  earlier draft of `_first_open_target` ordered candidate fields purely by
  `FIELD_IMPORTANCE_ORDER`, which meant a field that already had a value
  (just not yet graded) could beat a field with no value at all, simply
  because it was declared first among equally-`required` fields. This
  test pins the corrected behavior: no-value-at-all always wins.
- **No test constructs a real `CRUD`/orchestrator/asyncio worker.**
  `_run_one_cycle` (`phase-6`), `apply_field_completeness` (`phase-5`),
  and the queue/pipeline wiring (`phase-7`/`phase-8`) are exactly the kind
  of stateful, timing-sensitive, I/O-bound code this repo already avoids
  unit-testing (see `CLAUDE.md`'s "Testing approach": *"verify pipeline
  changes by running the backend and frontend locally and exercising a
  session end-to-end"*) — the manual verification steps below cover that
  surface instead.

## Manual end-to-end verification

Run the backend and frontend locally (as already documented for this
project) and join a session as the candidate. Walk through:

1. **Cold start asks about the top-priority empty block.** On session
   start (resume completely empty), stay silent for the hardbound silence
   window without saying anything. Confirm the bot's next turn steers
   toward asking about whichever block ended up `objective_priority: 1`
   in `phase-1`'s schema (`personal`, by the default ordering chosen
   there) — not some other block, and not silence/no question.
2. **Field-level gap on a partially-filled block.** Describe your name and
   contact info but nothing else about `personal`, then go quiet. Confirm
   the next question targets a *specific missing field* inside `personal`
   (e.g. location or a missing contact field), not a repeat "tell me about
   yourself" block-level question, and not a jump to an unrelated block.
3. **Started-block-beats-empty-block.** Describe your education and
   skills in detail (leaving `personal`/`experience`/everything else
   untouched), then go quiet. Confirm the question stays on
   education/skills' remaining gaps, even though other blocks may carry a
   numerically higher (i.e. more urgent) `objective_priority`.
4. **Sticky focus.** Give a deliberately vague answer to whatever question
   just came up (something that would grade `PARTIAL`, not `SUFFICIENT`)
   and go quiet again. Confirm the *next* question probes the *same*
   target rather than jumping elsewhere. Then give a genuinely complete
   answer and go quiet once more — confirm the focus now advances to a
   new target.
5. **Interruption mid-cycle.** Start speaking again *before* the silence
   hardbound window elapses, right after a previous answer. Confirm no
   stale/duplicate question fires from the cancelled cycle.
6. **Fully sufficient resume.** After covering every block adequately,
   go quiet one more time. Confirm the bot doesn't force out a new
   question (it may simply continue the conversation naturally via
   `PERSONA_PROMPT`, with no steering message injected).
7. **Debug export.** Check the session's debug JSON export (see
   `backend/app/meeting_room/data/crud.py`'s status-export path, extended
   in `phase-5`) shows `next_question`/`current_focus_path` updating
   across cycles as expected, and clearing once a target resolves.

## Doc sync reminder (once this is actually implemented in code)

This documentation set makes no source changes — everything above is a
guide for manual implementation. Per `CLAUDE.md`'s maintenance protocol,
**as soon as the corresponding source files are actually edited**, update
these domain docs in the same turn as the code change (not deferred to a
later cleanup pass):

- `.claude/backend/completeness-pipeline.md` — currently states
  question-generation is *"Explicitly out of scope"*; this needs rewriting
  to describe `select_focus_target`/`target_status`, the sticky-focus +
  two-tier priority selection, and the extended `run_completeness_chain`
  guard/response shape (`phase-2`, `phase-3`, `phase-4`, `phase-6`).
- `.claude/backend/database-models.md` — needs the new `next_question`/
  `current_focus_path` session-row fields and the extended
  `apply_field_completeness` signature (`phase-5`).
- `.claude/backend/room-orchestration.md` — needs the new
  `_question_queues`/`enqueue_next_question` plumbing (`phase-7`).
- `.claude/backend/stt-tts-pipeline.md` — needs `run_bot`'s new
  `question_queue` param and the context-injection consumer task
  (`phase-8`).
- `.claude/CLAUDE.md`'s Map table — no new domain file is being added (this
  feature extends four existing domains rather than introducing a fifth),
  so the Map table itself shouldn't need a new row; double-check that
  assumption once the code lands, in case the implementation ends up
  large enough to warrant splitting question-generation into its own
  domain doc.
