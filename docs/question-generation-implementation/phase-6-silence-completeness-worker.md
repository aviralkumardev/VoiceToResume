# Phase 6 — Wiring Target Selection Into the Worker

## What this does

Extends `_run_one_cycle` — the debounce/cancel/commit state machine from
`docs/silence-detection-processing-implementation/phase-9` — to:

1. Compute `target = select_focus_target(...)` (`phase-2`) alongside the
   existing `prune_for_judgment(...)` call.
2. **Only skip the LLM call when there's neither a verdict nor a question
   to get** (`not to_judge and target is None`) — previously it was
   implicitly skipped by `run_completeness_chain`'s own internal guard
   whenever `to_judge` was empty; now the worker's early-return has to
   account for `target` too, since a completely-empty resume produces an
   empty `to_judge` but a non-`None` target.
3. Pass `target` (as `question_target`) and `resume` into
   `run_completeness_chain` (`phase-4`).
4. After merging, decide whether the target this round was actually
   asking about **resolved** — look up its status in the freshly merged
   result via `target_status` (`phase-2`), reused against `merged` this
   time instead of `previous_status`. If it's now `SUFFICIENT`, no
   question is kept (asking about something already resolved makes no
   sense) and the sticky focus clears; otherwise, if the LLM supplied
   non-empty `question` text, that becomes `next_question` and
   `current_focus_path` stays pinned to the same target for next cycle.
5. Commit `merged` **and** `next_question`/`current_focus_path` together
   in the same `asyncio.shield`-guarded `apply_field_completeness` call
   (`phase-5`) — same cancellation semantics as before, now covering the
   question too.
6. Once the shielded commit has gone through (or the shield's
   `CancelledError` is swallowed the same way it already was), **fire the
   question at the voice bot** via `orchestrator.enqueue_next_question(...)`
   (`phase-7`) — a plain, best-effort call, matching this pipeline's
   existing fail-soft posture (`orchestrator` may be `None`, e.g. in a
   test harness that constructs the worker directly).

`run_silence_completeness_worker` itself is otherwise unchanged — it just
gains and threads through one new optional `orchestrator` param.

## File to modify: `backend/app/meeting_room/resume_analysis_pipeline/silence_completeness_worker.py`

Current file (for reference — this is what exists today, from
`docs/silence-detection-processing-implementation/phase-9`, unchanged
since):

```python
import asyncio
from typing import Optional

from app.meeting_room.data.crud_interfaces import ResumeRoomCRUD
from app.core.config import settings

from app.meeting_room.resume_analysis_pipeline.completeness_status import (
    merge_completeness,
    prune_for_judgment,
)
from app.meeting_room.resume_analysis_pipeline.config_jsons_definitions.coverage_schema import (
    COVERAGE_SCHEMA,
)
from app.meeting_room.resume_analysis_pipeline.completeness_chain import run_completeness_chain

async def run_silence_completeness_worker(
    session_id: str,
    crud: ResumeRoomCRUD,
    queue: "asyncio.Queue[Optional[bool]]"
) -> None:
    """Consumes `True`/`False`/`None` speaking-state events for one session,
    running the hardbound-wait-then-judge cycle as a single cancellable
    in-flight task. Mirrors run_resume_analysis_worker's per-session
    consume-until-None shape, but keyed off speaking state instead of
    transcript text.
    """

    pending_task: Optional[asyncio.Task] = None
    try:
        while True:
            is_speaking = await queue.get()

            if is_speaking is None:
                break

            if is_speaking:
                if pending_task is not None and not pending_task.done():
                    pending_task.cancel()
                continue

            if pending_task is None or pending_task.done():
                pending_task = asyncio.create_task(_run_one_cycle(session_id, crud))

    finally:
        if pending_task is not None and not pending_task.done():
            pending_task.cancel()


async def _run_one_cycle(session_id: str, crud: ResumeRoomCRUD) -> None:
    try:
        await asyncio.sleep(settings.resume_room_silence_hardbound_seconds)
    except asyncio.CancelledError:
        return

    row = await crud.get_session(session_id)
    if row is None:
        return

    already_decided, to_judge = prune_for_judgment(
        row["resume_data"], COVERAGE_SCHEMA, row.get("field_completeness", {})
    )

    try:
        result = await run_completeness_chain(to_judge, COVERAGE_SCHEMA)
    except asyncio.CancelledError:
        return


    merged = merge_completeness(already_decided, result.get("blocks", {}), COVERAGE_SCHEMA)
    llm_usage = result.get("_llm_usage")

    try:
        await asyncio.shield(crud.apply_field_completeness(session_id, merged, llm_usage=llm_usage))
    except asyncio.CancelledError:
        pass
```

This is a small enough file that the cleanest instruction is: **replace
the whole file** with the version below.

```python
import asyncio
from typing import Optional

from app.meeting_room.data.crud_interfaces import ResumeRoomCRUD
from app.core.config import settings

from app.meeting_room.resume_analysis_pipeline.completeness_status import (
    STATUS_SUFFICIENT,
    merge_completeness,
    prune_for_judgment,
    select_focus_target,
    target_status,
)
from app.meeting_room.resume_analysis_pipeline.config_jsons_definitions.coverage_schema import (
    COVERAGE_SCHEMA,
)
from app.meeting_room.resume_analysis_pipeline.completeness_chain import run_completeness_chain

async def run_silence_completeness_worker(
    session_id: str,
    crud: ResumeRoomCRUD,
    queue: "asyncio.Queue[Optional[bool]]",
    orchestrator=None,
) -> None:
    """Consumes `True`/`False`/`None` speaking-state events for one session,
    running the hardbound-wait-then-judge-then-question cycle as a single
    cancellable in-flight task. Mirrors run_resume_analysis_worker's
    per-session consume-until-None shape, but keyed off speaking state
    instead of transcript text. `orchestrator` (optional) delivers a freshly
    generated question into the live voice bot -- see phase-7.
    """

    pending_task: Optional[asyncio.Task] = None
    try:
        while True:
            is_speaking = await queue.get()

            if is_speaking is None:
                break

            if is_speaking:
                if pending_task is not None and not pending_task.done():
                    pending_task.cancel()
                continue

            if pending_task is None or pending_task.done():
                pending_task = asyncio.create_task(_run_one_cycle(session_id, crud, orchestrator))

    finally:
        if pending_task is not None and not pending_task.done():
            pending_task.cancel()


async def _run_one_cycle(session_id: str, crud: ResumeRoomCRUD, orchestrator=None) -> None:
    try:
        await asyncio.sleep(settings.resume_room_silence_hardbound_seconds)
    except asyncio.CancelledError:
        return

    row = await crud.get_session(session_id)
    if row is None:
        return

    resume = row["resume_data"]
    previous_status = row.get("field_completeness", {})

    already_decided, to_judge = prune_for_judgment(resume, COVERAGE_SCHEMA, previous_status)
    target = select_focus_target(resume, COVERAGE_SCHEMA, previous_status, row.get("current_focus_path"))

    if not to_judge and target is None:
        return

    try:
        result = await run_completeness_chain(to_judge, COVERAGE_SCHEMA, question_target=target, resume=resume)
    except asyncio.CancelledError:
        return

    merged = merge_completeness(already_decided, result.get("blocks", {}), COVERAGE_SCHEMA)
    llm_usage = result.get("_llm_usage")

    next_question = None
    new_focus_path = None
    if target is not None and target_status(target["target_path"], merged) != STATUS_SUFFICIENT:
        question_text = result.get("question")
        if question_text:
            next_question = {
                "target_type": target["target_type"],
                "target_path": target["target_path"],
                "question": question_text,
            }
            new_focus_path = target["target_path"]

    try:
        await asyncio.shield(crud.apply_field_completeness(
            session_id,
            merged,
            next_question=next_question,
            current_focus_path=new_focus_path,
            llm_usage=llm_usage,
        ))
    except asyncio.CancelledError:
        pass

    if next_question is not None and orchestrator is not None:
        orchestrator.enqueue_next_question(session_id, next_question["question"])
```

## Key design points, explained

- **`target` is computed from `previous_status` — the state *before* this
  round's LLM call**, exactly like `to_judge` is. This is unavoidable: we
  have to tell the LLM which target to write a question for *before* we
  know this round's fresh verdicts. The one-round lag this could imply
  (the target resolves to `SUFFICIENT` in this very round) is exactly what
  the post-merge `target_status(target["target_path"], merged) !=
  STATUS_SUFFICIENT` check guards against — if the target *did* resolve
  this round, no question is kept for it at all, and `current_focus_path`
  clears so next cycle picks something fresh via `select_focus_target`.
- **`if not to_judge and target is None: return`** is a worker-level
  early-return, separate from (but consistent with) `phase-4`'s own
  internal guard inside `run_completeness_chain`. The worker's guard
  avoids doing any of the `already_decided`/`merged` bookkeeping and the
  CRUD write at all on a true no-op round (fully `SUFFICIENT` resume,
  nothing to verdict, nothing to ask) — `run_completeness_chain`'s own
  guard is what actually skips the network call whenever *it's* called
  with nothing to do, which still matters for the "has to_judge but no
  target" case that reaches the chain but doesn't need special-casing
  here.
- **The commit still always happens whenever the LLM call actually ran**
  (i.e. we didn't hit the early return above), exactly like before this
  phase — a round that turns up nothing new for `next_question` still
  needs to persist `merged` (which might contain freshly-`MISSING`-
  populated skeleton state on the very first round) and clear a
  stale `current_focus_path`/`next_question` if the round genuinely
  produced nothing.
- **The `enqueue_next_question` call sits *after* the shielded commit,
  not inside the `try`.** If a cancellation lands in that narrow post-LLM
  window, the shield still lets the CRUD write finish in the background
  (per the original `phase-9` reasoning); execution then falls through to
  the delivery call regardless of whether the `except CancelledError:
  pass` branch was taken, since it's caught rather than re-raised. This
  is what "the result commits unconditionally, even if the candidate has
  already resumed speaking" now means for the question too, per the
  original spec's interruption-handling requirement.
- **`orchestrator=None` is a real, supported case**, not just a defensive
  default — it's what lets `phase-9`'s tests (and any future harness)
  call `_run_one_cycle`/`run_silence_completeness_worker` directly against
  a fake CRUD without needing a real orchestrator/bot pipeline running.
