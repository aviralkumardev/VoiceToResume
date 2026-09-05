# Phase 10 — Room Orchestrator Wiring

## What this does

Adds a second per-session queue (`_speaking_queues`) and a second per-session
worker task (tracked as `_tasks[f"completeness_{session_id}"]`), fully
mirroring how `_queues`/`analysis_task` are already spawned, tracked, fed a
`None` sentinel, and torn down — touched in every one of the four places
that pattern already appears: `start_session`, `_on_bot_done`, `_close_out`,
and the spawn-failure unwind path inside `start_session`.

**Do not wire this in before `phase-9`'s module exists** — `start_session`
imports `run_silence_completeness_worker` the same way it already imports
`run_resume_analysis_worker` at module level, so this phase has a hard
dependency on `phase-9` and `phase-8`/`phase-7` (the callback that actually
feeds this queue) landing first.

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



class ResumeRoomOrchestrator:

    def __init__(self, *, crud: Optional[ResumeRoomCRUD] = None) -> None:
        self._crud: ResumeRoomCRUD = crud if crud is not None else get_resume_room_crud()
        self._http: Optional[aiohttp.ClientSession] = None
        self._daily: Optional[DailyClient] = None
        self._tasks: Dict[str, asyncio.Task] = {}
        self._queues: Dict[str, "asyncio.Queue[Optional[str]]"] = {}


    def enqueue_transcript(self, session_id: str, text: str) -> None:
        queue = self._queues.get(session_id)
        if queue is None:
            return
        try:
            queue.put_nowait(text)
        except asyncio.QueueFull:
            pass


    def _get_daily(self) -> DailyClient:
        # ... unchanged, omitted here ...


    async def _delete_room(self, room_name: str) -> None:
        await self._get_daily().delete_room(room_name)


    async def _run_guarded_bot(self, session_id: str, room_name: str, room_url: str, bot_token: str) -> None:
        # ... unchanged, omitted here ...


    def _on_bot_done(self, session_id: str, room_name: str, task: asyncio.Task) -> None:
        self._tasks.pop(session_id, None)
        queue = self._queues.get(session_id)
        if queue is not None:
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

        status = STATUS_ENDED
        error: Optional[str] = None

        if task.cancelled():
            status = STATUS_ENDED
        else:
            exc = task.exception()
            if isinstance(exc, asyncio.TimeoutError):
                status = STATUS_TIMED_OUT
                error = f"Session exceeded resume_room_max_session_seconds={settings.resume_room_max_session_seconds}"
            elif exc is not None:
                status = STATUS_FAILED
                error = f"{type(exc).__name__}: {exc}"

        asyncio.create_task(
            self._close_out(session_id, room_name, status=status, error=error),
            name=f"resume-room-closeout-{room_name}",
        )


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


    async def start_session(self) -> StartSessionResponse:
        # ... daily room creation / session_row creation unchanged, omitted here ...

        session_id = session_row["session_id"]

        self._queues[session_id] = asyncio.Queue(maxsize=1000)
        
        analysis_task = asyncio.create_task(
            run_resume_analysis_worker(session_id, self._queues[session_id], self._crud),
            name=f"resume-analysis-{session_id}"
        )

        self._tasks[f"analysis_{session_id}"] = analysis_task

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

            raise HTTPException(
                status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"RESUME ROOM: Failed to spawn the bot: {exc}",
            ) from exc

        return StartSessionResponse(roomUrl=room.url, token=user_token, roomName=room_name)


    # ... stop_session, shutdown unchanged, omitted here ...
```

**Change 1** — new import, alongside the existing analysis-worker import:

```python
from app.meeting_room.resume_analysis_pipeline.analysis_orchestrator import run_resume_analysis_worker
from app.meeting_room.resume_analysis_pipeline.silence_completeness_worker import run_silence_completeness_worker
```

**Change 2** — new dict in `__init__`, alongside `_queues`:

```python
        self._tasks: Dict[str, asyncio.Task] = {}
        self._queues: Dict[str, "asyncio.Queue[Optional[str]]"] = {}
        self._speaking_queues: Dict[str, "asyncio.Queue[Optional[bool]]"] = {}
```

**Change 3** — new method, right after `enqueue_transcript`:

```python
    def enqueue_speaking_state(self, session_id: str, is_speaking: bool) -> None:
        queue = self._speaking_queues.get(session_id)
        if queue is None:
            return
        try:
            queue.put_nowait(is_speaking)
        except asyncio.QueueFull:
            pass
```

**Change 4** — `_on_bot_done` also pushes the sentinel into the speaking queue:

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

        status = STATUS_ENDED
        error: Optional[str] = None
        # ... rest unchanged ...
```

**Change 5** — `_close_out` also bounded-waits and tears down the completeness task/queue:

```python
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
```

**Change 6** — `start_session` creates the queue and spawns the worker, right after the existing analysis-task spawn:

```python
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
```

**Change 7** — the spawn-failure unwind path in `start_session` also cleans up the completeness task/queue, mirroring the existing analysis-task unwind block exactly:

```python
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
```

`stop_session` and `shutdown` need no changes — both already operate purely
off `self._tasks` (cancelling the bot task, or gathering every tracked
task on shutdown), and `_tasks[f"completeness_{session_id}"]` is already a
member of that same dict by Change 6, so it's swept up by both without any
special-casing.

## Key design points, explained

- **Change 6 is placed as a fully separate block, not merged into the
  existing analysis-task spawn** — the two workers are independent by
  design (`phase-0`), so keeping their spawn code visually distinct (own
  queue line, own `create_task` call, own `_tasks[...]` assignment) makes
  it obvious at a glance that touching one doesn't require touching the
  other.
- **Change 7 duplicates Change 5's teardown shape rather than factoring
  out a shared helper** — this matches the existing code's own choice not
  to factor out the analysis-task teardown between `_close_out` and the
  spawn-failure path, despite them being nearly identical; consistency
  with the surrounding style wins over introducing a new abstraction this
  phase doesn't need.
- **`run_silence_completeness_worker` is given `self._crud` directly**,
  not the row or a bound method — same as `run_resume_analysis_worker`
  already receiving `self._crud` as its own third argument, keeping the
  worker's dependencies fully explicit rather than reaching back into the
  orchestrator.
