# Phase 8 — Wiring: spawn the worker, feed it candidate speech

## What this does

Connects everything built in phases 1–6 to the live session lifecycle:
creates a queue and worker task when a session starts, feeds the worker from
the candidate's transcript bridge, and tears the worker down cleanly (with a
final flush) when the session ends.

Do not wire this in until phases 5 and 6 both exist — `room_orchestrator.py`
will import `run_resume_analysis_worker`, which doesn't exist until phase 5,
and calls `apply_resume_update`, which doesn't exist until phase 6.

## File to modify: `app/meeting_room/room_orchestrator.py`

Current file in full (for reference — this is what exists today):

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


class ResumeRoomOrchestrator:

    def __init__(self, *, crud: Optional[ResumeRoomCRUD] = None) -> None:
        self._crud: ResumeRoomCRUD = crud if crud is not None else get_resume_room_crud()
        self._http: Optional[aiohttp.ClientSession] = None
        self._daily: Optional[DailyClient] = None
        self._tasks: Dict[str, asyncio.Task] = {}

    # ... _get_daily, _delete_room, _run_guarded_bot unchanged ...

    def _on_bot_done(self, session_id: str, room_name: str, task: asyncio.Task) -> None:
        self._tasks.pop(session_id, None)

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

    async def start_session(self) -> StartSessionResponse:
        # ... daily room creation, active-count check, session_row creation unchanged ...

        session_id = session_row["session_id"]

        try:
            ensure_daily_runtime()
            bot_task = asyncio.create_task(
                self._run_guarded_bot(session_id, room_name, room.url, bot_token),
                name=f"resume-room-{room_name}",
            )
            self._tasks[session_id] = bot_task
            bot_task.add_done_callback(lambda t: self._on_bot_done(session_id, room_name, t))
        except Exception as exc:
            # ... unchanged error handling ...

        return StartSessionResponse(roomUrl=room.url, token=user_token, roomName=room_name)

    # ... stop_session, shutdown unchanged ...
```

### Change 1 — `__init__`: add a queues dict

```python
    def __init__(self, *, crud: Optional[ResumeRoomCRUD] = None) -> None:
        self._crud: ResumeRoomCRUD = crud if crud is not None else get_resume_room_crud()
        self._http: Optional[aiohttp.ClientSession] = None
        self._daily: Optional[DailyClient] = None
        self._tasks: Dict[str, asyncio.Task] = {}
        self._queues: Dict[str, "asyncio.Queue[Optional[str]]"] = {}
```

### Change 2 — new method: `enqueue_transcript`

Add this method anywhere in the class (e.g. right after `__init__`):

```python
    def enqueue_transcript(self, session_id: str, text: str) -> None:
        queue = self._queues.get(session_id)
        if queue is None:
            return
        try:
            queue.put_nowait(text)
        except asyncio.QueueFull:
            pass  # logged via loguru if you want visibility here
```

### Change 3 — import for the new worker, added at the top of the file

```python
from app.meeting_room.resume_analysis_pipeline.analysis_orchestrator import run_resume_analysis_worker
```

### Change 4 — `start_session`: spawn the analysis worker alongside the bot

Right after `session_id = session_row["session_id"]` and **before** the
existing bot-task `try` block:

```python
        session_id = session_row["session_id"]

        self._queues[session_id] = asyncio.Queue(maxsize=1000)
        analysis_task = asyncio.create_task(
            run_resume_analysis_worker(session_id, self._queues[session_id], self._crud),
            name=f"resume-analysis-{session_id}",
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
            # ... unchanged ...
```

Note: the analysis task is stored under a **different key**
(`f"analysis_{session_id}"`) than the bot task (`session_id`) in the same
`self._tasks` dict — they don't collide, and `shutdown()`'s existing
"cancel everything in `self._tasks`" loop transparently covers both without
any change to `shutdown()` itself.

### Change 5 — `_on_bot_done`: push the final-flush sentinel

```python
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
```

### Change 6 — `_close_out`: await/cancel the analysis task and drop the queue

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
```

### `stop_session` and `shutdown`: no changes needed

- `stop_session`'s existing "task found, cancel it" branch cancels the *bot*
  task, whose `add_done_callback`-registered `_on_bot_done` still fires
  normally on cancellation — so the sentinel push and analysis-task teardown
  above happen exactly the same way as a natural session end. No change
  needed there.
- `shutdown()` already iterates `self._tasks.values()` and cancels/gathers
  everything in the dict — since the analysis task is now also stored there
  (under its own key), it's already covered without any edit.

## File to modify: `app/meeting_room/stt_tts_pipeline/pipeline.py`

Current relevant section (for reference — this is what exists today):

```python
    crud = orchestrator._crud if orchestrator is not None else None

    def persist(role: str, text: str):
        if crud is not None:
            asyncio.create_task(crud.append_transcript_line(session_id, role, text))
```

Change `persist` to also feed the new queue, only for the candidate's own
speech:

```python
    crud = orchestrator._crud if orchestrator is not None else None

    def persist(role: str, text: str):
        if crud is not None:
            asyncio.create_task(crud.append_transcript_line(session_id, role, text))
        if role == "user" and orchestrator is not None:
            orchestrator.enqueue_transcript(session_id, text)
```

Nothing else in `pipeline.py` changes — `persist("user", text)` is already
wired to `UserTranscriptBridge(..., on_final_transcription=lambda text: persist("user", text))`
at the existing pipeline construction site, and that callback already only
fires on a final (non-interim) `TranscriptionFrame`, which is exactly the
trigger needed here.

## Why no new bridge/tap processor

pitch_room has a dedicated `analysis_tap.py` `FrameProcessor` for this job.
That exists there because pitch_room's transcript-queue feed needed a
separate site distinct from its own `persist`-equivalent. Here, `persist()`
already receives `role` directly from the existing bridge wiring — gating on
`role == "user"` inline is sufficient and strictly simpler than adding a new
pipeline stage.
