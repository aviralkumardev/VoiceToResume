# Phase 7 — Room Orchestrator Wiring

## What this does

Adds a **third** per-session queue, `_question_queues`, mirroring
`_queues`/`_speaking_queues` exactly (same creation spot, same
`enqueue_*` shape, same `None`-sentinel teardown in both `_on_bot_done`
and the `start_session` spawn-failure unwind path). Unlike the other two
queues, **nothing in `room_orchestrator.py` itself consumes this one** —
it's created here and handed straight into `run_bot(...)` at bot-spawn
time, to be consumed by a task living *inside* the pipeline
(`phase-8`), because that's the only place with a handle on the live
`PipelineWorker` needed to actually inject a turn.

Also updates the existing `run_silence_completeness_worker(...)` call to
pass `orchestrator=self`, matching `phase-6`'s new param — this is what
lets the worker call `self.enqueue_next_question(...)` once it produces a
question.

**Do not wire this in before `phase-6`'s worker actually calls
`orchestrator.enqueue_next_question(...)`, and don't wire `phase-8` in
before this phase's queue exists to hand to `run_bot`** — same
don't-wire-too-early warning the reference implementation docs give.

## File to modify: `backend/app/meeting_room/room_orchestrator.py`

Current file (for reference — this is what exists today):

```python
from __future__ import annotations

import asyncio
from typing import Dict, Optional

import aiohttp
from fastapi import HTTPException
from fastapi import status as http_status

from app.core.config import settings
from app.meeting_room.data.crud import get_resume_room_crud
from app.meeting_room.data.crud_interfaces import (
    ResumeRoomCRUD,
    STATUS_ENDED,
    STATUS_FAILED,
    STATUS_TIMED_OUT,
)
from app.meeting_room.daily.client import DailyClient, DailyClientError
from app.meeting_room.daily.runtime import ensure_daily_runtime
from app.meeting_room.models import StartSessionResponse, StopSessionResponse
from app.meeting_room.resume_analysis_pipeline.analysis_orchestrator import run_resume_analysis_worker
from app.meeting_room.resume_analysis_pipeline.silence_completeness_worker import run_silence_completeness_worker


class ResumeRoomOrchestrator:

    def __init__(self, *, crud: Optional[ResumeRoomCRUD] = None) -> None:
        self._crud: ResumeRoomCRUD = crud if crud is not None else get_resume_room_crud()
        self._http: Optional[aiohttp.ClientSession] = None
        self._daily: Optional[DailyClient] = None
        self._tasks: Dict[str, asyncio.Task] = {}
        self._queues: Dict[str, "asyncio.Queue[Optional[str]]"] = {}
        self._speaking_queues: Dict[str, "asyncio.Queue[Optional[bool]]"] = {}


    def enqueue_transcript(self, session_id: str, text: str) -> None:
        queue = self._queues.get(session_id)
        if queue is None:
            return
        try:
            queue.put_nowait(text)
        except asyncio.QueueFull:
            pass


    def enqueue_speaking_state(self, session_id: str, is_speaking: bool) -> None:
        queue = self._speaking_queues.get(session_id)
        if queue is None:
            return
        try:
            queue.put_nowait(is_speaking)
        except asyncio.QueueFull:
            pass
    

    def _get_daily(self) -> DailyClient:
        # ... unchanged, omitted here ...


    async def _delete_room(self, room_name: str) -> None:
        await self._get_daily().delete_room(room_name)


    async def _run_guarded_bot(self, session_id: str, room_name: str, room_url: str, bot_token: str) -> None:
        from app.meeting_room.stt_tts_pipeline.pipeline import run_bot
        await asyncio.wait_for(
            run_bot(
                room_url,
                bot_token,
                room_name=room_name,
                session_id=session_id,
                orchestrator=self,
            ),
            timeout=settings.resume_room_max_session_seconds,
        )


    def _on_bot_done(self, session_id: str, room_name: str, task: asyncio.Task) -> None:
        self._tasks.pop(session_id, None)
        queue = self._queues.get(session_id)
        if queue is not None:
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

        speaking_queue = self._speaking_queues.get(session_id)
        if speaking_queue is not None:
            try:
                speaking_queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

        status = STATUS_ENDED
        error: Optional[str] = None
        # ... rest unchanged ...


    async def _close_out(self, session_id: str, room_name: str, *, status: str, error: Optional[str]) -> None:
        try:
            await self._crud.mark_finished(session_id, status, error=error)
        except Exception:
            pass

        try:
            await self._delete_room(room_name)
        except Exception:
            pass

        self._tasks.pop(session_id, None)

        analysis_task = self._tasks.pop(f"analysis_{session_id}", None)
        if analysis_task is not None:
            try:
                await asyncio.wait_for(analysis_task, timeout=30)
            except asyncio.TimeoutError:
                analysis_task.cancel()
            except Exception:
                pass
        self._queues.pop(session_id, None)

        completeness_task = self._tasks.pop(f"completeness_{session_id}", None)
        if completeness_task is not None:
            try:
                await asyncio.wait_for(completeness_task, timeout=30)
            except asyncio.TimeoutError:
                completeness_task.cancel()
            except Exception:
                pass
        self._speaking_queues.pop(session_id, None)


    async def start_session(self) -> StartSessionResponse:
        # ... daily room creation / session_row creation unchanged, omitted here ...

        session_id = session_row["session_id"]

        self._queues[session_id] = asyncio.Queue(maxsize=1000)
        
        analysis_task = asyncio.create_task(
            run_resume_analysis_worker(session_id, self._queues[session_id], self._crud),
            name=f"resume-analysis-{session_id}"
        )

        self._tasks[f"analysis_{session_id}"] = analysis_task

        self._speaking_queues[session_id] = asyncio.Queue(maxsize=1000)

        completeness_task = asyncio.create_task(
            run_silence_completeness_worker(session_id, self._crud, self._speaking_queues[session_id]),
            name=f"resume-completeness-{session_id}"
        )

        self._tasks[f"completeness_{session_id}"] = completeness_task

        try:
            ensure_daily_runtime()
            bot_task = asyncio.create_task(
                self._run_guarded_bot(session_id, room_name, room.url, bot_token),
                name=f"resume-room-{room_name}",
            )
            self._tasks[session_id] = bot_task
            bot_task.add_done_callback(lambda t: self._on_bot_done(session_id, room_name, t))
        except Exception as exc:
            self._tasks.pop(session_id, None)
            try:
                await self._crud.mark_finished(session_id, STATUS_FAILED, error=f"spawn failed: {exc}")
            except Exception:
                pass
            try:
                await self._delete_room(room_name)
            except Exception:
                pass

            queue = self._queues.get(session_id)
            if queue is not None:
                try:
                    queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass
            leaked_analysis_task = self._tasks.pop(f"analysis_{session_id}", None)
            if leaked_analysis_task is not None:
                try:
                    await asyncio.wait_for(leaked_analysis_task, timeout=30)
                except asyncio.TimeoutError:
                    leaked_analysis_task.cancel()
                except Exception:
                    pass
            self._queues.pop(session_id, None)

            speaking_queue = self._speaking_queues.get(session_id)
            if speaking_queue is not None:
                try:
                    speaking_queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass
            leaked_completeness_task = self._tasks.pop(f"completeness_{session_id}", None)
            if leaked_completeness_task is not None:
                try:
                    await asyncio.wait_for(leaked_completeness_task, timeout=30)
                except asyncio.TimeoutError:
                    leaked_completeness_task.cancel()
                except Exception:
                    pass
            self._speaking_queues.pop(session_id, None)

            raise HTTPException(
                status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"RESUME ROOM: Failed to spawn the bot: {exc}",
            ) from exc

        return StartSessionResponse(roomUrl=room.url, token=user_token, roomName=room_name)


    # ... stop_session, shutdown unchanged, omitted here ...
```

**Change 1** — new dict in `__init__`, alongside `_speaking_queues`:

```python
        self._tasks: Dict[str, asyncio.Task] = {}
        self._queues: Dict[str, "asyncio.Queue[Optional[str]]"] = {}
        self._speaking_queues: Dict[str, "asyncio.Queue[Optional[bool]]"] = {}
        self._question_queues: Dict[str, "asyncio.Queue[Optional[str]]"] = {}
```

**Change 2** — new method, right after `enqueue_speaking_state`:

```python
    def enqueue_next_question(self, session_id: str, question: str) -> None:
        queue = self._question_queues.get(session_id)
        if queue is None:
            return
        try:
            queue.put_nowait(question)
        except asyncio.QueueFull:
            pass
```

**Change 3** — `_run_guarded_bot` passes the queue into `run_bot`:

```python
    async def _run_guarded_bot(self, session_id: str, room_name: str, room_url: str, bot_token: str) -> None:
        from app.meeting_room.stt_tts_pipeline.pipeline import run_bot
        await asyncio.wait_for(
            run_bot(
                room_url,
                bot_token,
                room_name=room_name,
                session_id=session_id,
                orchestrator=self,
                question_queue=self._question_queues.get(session_id),
            ),
            timeout=settings.resume_room_max_session_seconds,
        )
```

**Change 4** — `_on_bot_done` also pushes the sentinel into the question
queue, alongside the existing two:

```python
    def _on_bot_done(self, session_id: str, room_name: str, task: asyncio.Task) -> None:
        self._tasks.pop(session_id, None)
        queue = self._queues.get(session_id)
        if queue is not None:
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

        speaking_queue = self._speaking_queues.get(session_id)
        if speaking_queue is not None:
            try:
                speaking_queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

        question_queue = self._question_queues.get(session_id)
        if question_queue is not None:
            try:
                question_queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

        status = STATUS_ENDED
        error: Optional[str] = None
        # ... rest unchanged ...
```

**Change 5** — `_close_out` also pops the question queue (no task to
await/cancel here — see "Key design points" below):

```python
        completeness_task = self._tasks.pop(f"completeness_{session_id}", None)
        if completeness_task is not None:
            try:
                await asyncio.wait_for(completeness_task, timeout=30)
            except asyncio.TimeoutError:
                completeness_task.cancel()
            except Exception:
                pass
        self._speaking_queues.pop(session_id, None)

        self._question_queues.pop(session_id, None)
```

**Change 6** — `start_session` creates the queue (right after the
speaking queue) and passes `orchestrator=self` into the completeness
worker call:

```python
        self._speaking_queues[session_id] = asyncio.Queue(maxsize=1000)
        self._question_queues[session_id] = asyncio.Queue(maxsize=50)

        completeness_task = asyncio.create_task(
            run_silence_completeness_worker(
                session_id, self._crud, self._speaking_queues[session_id], orchestrator=self
            ),
            name=f"resume-completeness-{session_id}"
        )

        self._tasks[f"completeness_{session_id}"] = completeness_task
```

**Change 7** — the spawn-failure unwind path also cleans up the question
queue, mirroring the existing speaking-queue cleanup immediately above it:

```python
            speaking_queue = self._speaking_queues.get(session_id)
            if speaking_queue is not None:
                try:
                    speaking_queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass
            leaked_completeness_task = self._tasks.pop(f"completeness_{session_id}", None)
            if leaked_completeness_task is not None:
                try:
                    await asyncio.wait_for(leaked_completeness_task, timeout=30)
                except asyncio.TimeoutError:
                    leaked_completeness_task.cancel()
                except Exception:
                    pass
            self._speaking_queues.pop(session_id, None)

            question_queue = self._question_queues.get(session_id)
            if question_queue is not None:
                try:
                    question_queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass
            self._question_queues.pop(session_id, None)

            raise HTTPException(
                status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"RESUME ROOM: Failed to spawn the bot: {exc}",
            ) from exc
```

`stop_session` and `shutdown` need no changes — same reasoning as the
original `phase-10`: they operate purely off `self._tasks`, and the
question queue was never turned into a separately-tracked orchestrator
task in the first place (see below), so there's nothing there for either
method to reach into.

## Key design points, explained

- **`_question_queues` has no matching entry in `self._tasks`.** The other
  two queues each have a dedicated orchestrator-spawned consumer task
  (`analysis_{session_id}`, `completeness_{session_id}`) that `_close_out`
  explicitly bounded-waits (30s) then cancels. This queue's consumer
  (`phase-8`) lives *inside* `run_bot`'s own coroutine instead — it isn't
  something the orchestrator spawned or can reach a task handle for, so
  `_close_out`/the spawn-failure path only ever need to `pop()` the queue
  itself, not await/cancel a task. The consumer inside `run_bot` still
  terminates cleanly off the same `None` sentinel `_on_bot_done` pushes —
  see `phase-8` for why that's safe without an explicit await here.
- **`maxsize=50` instead of matching the other queues' `1000`/`1000`** —
  this queue only ever carries one short string per completeness cycle (at
  most one every `resume_room_silence_hardbound_seconds` plus an LLM
  round-trip), nowhere near the transcript-line or speaking-event volume
  the other two queues are sized for. Any small bound works; this just
  avoids copy-pasting `1000` for a fundamentally lower-frequency channel.
- **`self._question_queues.get(session_id)` (not `[session_id]`) when
  passed into `run_bot`** — matches `enqueue_next_question`'s own
  `.get()`-based lookup; if the queue somehow isn't there yet (shouldn't
  happen given Change 6 always creates it before `_run_guarded_bot` is
  spawned, but keeps `run_bot`'s own `question_queue` param honestly
  `Optional`), `run_bot` (`phase-8`) simply doesn't spawn a consumer at
  all rather than crashing on a `KeyError`.
- **Change 6's `orchestrator=self` argument is the other half of
  `phase-6`'s new worker param** — without it, `_run_one_cycle` would
  always see `orchestrator=None` and silently never call
  `enqueue_next_question`, meaning every other phase would be wired
  correctly but no question would ever reach the bot. Easy to miss since
  it's a one-argument diff on an existing line.
