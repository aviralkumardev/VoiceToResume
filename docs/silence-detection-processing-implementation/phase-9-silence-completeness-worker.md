# Phase 9 — Silence-Triggered Completeness Worker

## What this does

The debounce/cancel/commit state machine itself: consumes speaking-state
events (`True`=started, `False`=stopped, `None`=session-end sentinel) off a
per-session queue, and for every stretch of silence, runs one full
hardbound-wait → judge → commit cycle as its own cancellable task.

**Cancellation semantics** (per `phase-0`): cancelling an `asyncio.Task`
that has already finished is a documented no-op — so `on_user_started_speaking`-
style handling here never has to ask "did it already finish?" before
calling `.cancel()`. Two distinct cancellation points matter:

1. **During the 2-second hardbound wait** — cancelling here means "the user
   was just thinking, not done talking." Nothing was sent to the LLM yet;
   this is a pure no-op, not even a discarded result.
2. **During the LLM call itself** — cancelling here discards the whole
   judgment; nothing gets written.

Once the LLM call has *returned*, the result is committed **unconditionally**,
even if a cancellation lands in the narrow window right after — this is
what `asyncio.shield(...)` around the final `crud.apply_field_completeness(...)`
call guards: the write always finishes once started, regardless of the
wrapping task's own cancellation state.

## New file: `backend/app/meeting_room/resume_analysis_pipeline/silence_completeness_worker.py`

```python
from __future__ import annotations

import asyncio
from typing import Optional

from loguru import logger

from app.core.config import settings
from app.meeting_room.data.crud_interfaces import ResumeRoomCRUD
from app.meeting_room.resume_analysis_pipeline.completeness_chain import run_completeness_chain
from app.meeting_room.resume_analysis_pipeline.completeness_status import (
    merge_completeness,
    prune_for_judgment,
)
from app.meeting_room.resume_analysis_pipeline.config_jsons_definitions.coverage_schema import (
    COVERAGE_SCHEMA,
)


async def run_silence_completeness_worker(
    session_id: str,
    crud: ResumeRoomCRUD,
    queue: "asyncio.Queue[Optional[bool]]",
) -> None:
    """Consumes `True` (user started speaking) / `False` (user stopped
    speaking) / `None` (session ending) events for one session, running the
    hardbound-wait-then-judge cycle as a single cancellable in-flight task.
    Mirrors run_resume_analysis_worker's per-session consume-until-None
    shape, but keyed off speaking state instead of transcript text.
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

            # is_speaking is False -- the user just went silent.
            if pending_task is None or pending_task.done():
                pending_task = asyncio.create_task(_run_one_cycle(session_id, crud))
    finally:
        if pending_task is not None and not pending_task.done():
            pending_task.cancel()


async def _run_one_cycle(session_id: str, crud: ResumeRoomCRUD) -> None:
    try:
        await asyncio.sleep(settings.resume_room_silence_hardbound_seconds)
    except asyncio.CancelledError:
        return  # the user resumed speaking before the hardbound elapsed -- just thinking

    row = await crud.get_session(session_id)
    if row is None:
        return

    already_decided, to_judge = prune_for_judgment(
        row["resume_data"], COVERAGE_SCHEMA, row.get("field_completeness", {})
    )

    try:
        result = await run_completeness_chain(to_judge, COVERAGE_SCHEMA)
    except asyncio.CancelledError:
        return  # interrupted mid-LLM-call -- discard entirely, nothing is committed

    merged = merge_completeness(already_decided, result.get("blocks", {}), COVERAGE_SCHEMA)
    llm_usage = result.get("_llm_usage")

    try:
        await asyncio.shield(crud.apply_field_completeness(session_id, merged, llm_usage=llm_usage))
    except asyncio.CancelledError:
        # A cancellation landed in the narrow window after the LLM call
        # returned but before this write finished -- the shield already let
        # the write complete in the background; there's nothing left to do,
        # so this is swallowed rather than re-raised.
        logger.debug(f"RESUME ROOM: completeness commit for session {session_id} raced a cancellation, write still landed")
```

## Key design points, explained

- **The hardbound sleep and the LLM call are two separate `try`/`except
  CancelledError` blocks**, not one — this is what distinguishes "still
  thinking" (silent discard, no logging needed, totally routine) from
  "interrupted mid-judgment" (also a discard, but conceptually a different
  event) even though both currently just `return`. Keeping them separate
  makes it a one-line change later if either branch ever needs different
  handling (e.g. logging only the second case).
- **`run_completeness_chain` is called even when `to_judge` is empty.** It
  returns immediately in that case (`phase-4`'s own internal guard) with
  `blocks: {}`, so `merge_completeness` just carries `already_decided`
  straight through with no network cost. This keeps `_run_one_cycle`'s
  control flow linear — no separate "nothing to judge" early-return branch
  duplicating what `merge_completeness` already does correctly with an
  empty `llm_blocks`.
- **The commit always happens, even on a no-op round** (nothing was
  `PARTIAL`, nothing changed). This is deliberate, not an oversight: on the
  very first silence event, this is what turns `field_completeness` from
  `{}` into a real `MISSING`-populated skeleton for every covered block,
  which matters for the debug export and any future question-generation
  phase reading this state — not just for rounds that produced a fresh
  verdict.
- **Why the queue's own consumer loop (`run_silence_completeness_worker`)
  never itself awaits `_run_one_cycle`** — it fires the cycle as its own
  task and immediately goes back to `queue.get()`, so a `True` (resume-
  speaking) event arriving mid-cycle is seen right away and can cancel it.
  If the loop instead did `await _run_one_cycle(...)` directly, there'd be
  no way to observe a same-session speaking-resume event until the cycle
  already finished — defeating the entire point of the feature.
- **`queue.get()` on `True` events just `continue`s** without creating
  anything — a start-speaking event when there's no in-flight cycle (e.g.
  two `True` events in a row, or a `True` before the first-ever `False`)
  is a harmless no-op.
