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
from app.meeting_room.resume_analysis_pipeline.analysis_orchestrator import (
    FlushRequest,
    cancel_grading_tasks,
    run_resume_analysis_worker,
)
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


    async def flush_transcript(self, session_id: str, *, wait: bool = True) -> None:
        """Force whatever's accumulated on the transcript queue through one
        extraction batch, out of turn with the char-count trigger.

        `wait=False` still enqueues the FlushRequest (so extraction runs
        promptly instead of waiting for the char trigger) but returns
        immediately instead of blocking the caller on that batch's LLM call.
        Callers that don't need a just-landed, verified resume_data read
        (see InterviewDirector._open_next_target) use this to keep a full
        extraction round trip off the interview turn's critical path.
        """
        queue = self._queues.get(session_id)
        if queue is None:
            return
        request = FlushRequest()
        try:
            queue.put_nowait(request)
        except asyncio.QueueFull:
            return
        if not wait:
            return
        try:
            await asyncio.wait_for(
                request.done.wait(), timeout=settings.resume_room_flush_timeout_seconds
            )
        except asyncio.TimeoutError:
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
        if self._daily is None:
            if self._http is None:
                self._http = aiohttp.ClientSession()
            self._daily = DailyClient(
                api_key=settings.daily_api_key,
                aiohttp_session=self._http,
            )
            try:
                self._daily.ensure_available()
            except DailyClientError as exc:
                raise HTTPException(
                    status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=f"RESUME ROOM: {exc}",
                ) from exc
        return self._daily


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

        # Post-extraction grading tasks spawned by analysis_orchestrator._run_batch
        # aren't tracked in self._tasks (they're fire-and-forget, off the
        # analysis worker's own await chain) -- cancel them explicitly so
        # none linger past teardown writing into a now-finished session.
        cancel_grading_tasks(session_id)

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
        daily = self._get_daily()

        try:
            active = await self._crud.count_active()
        except Exception as exc:
            raise HTTPException(
                status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="RESUME ROOM: Failed to check active session count.",
            ) from exc

        if active >= settings.resume_room_max_sessions:
            raise HTTPException(
                status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "RESUME ROOM: All session slots are in use right now "
                    f"({active}/{settings.resume_room_max_sessions}). Please try again shortly."
                ),
            )

        try:
            room = await daily.create_room(
                room_expiry_seconds=settings.resume_room_expiry_seconds,
                max_participants=settings.resume_room_max_participants_per_session,
            )
            bot_token = await daily.get_token(room.url, expiry_time=settings.resume_room_expiry_seconds, owner=True)
            user_token = await daily.get_token(
                room.url, expiry_time=settings.resume_room_expiry_seconds, owner=False, user_name="Candidate"
            )
            room_name = daily.get_name_from_url(room.url)
        except Exception as exc:
            raise HTTPException(
                status_code=http_status.HTTP_502_BAD_GATEWAY,
                detail="RESUME ROOM: Failed to set up the video room.",
            ) from exc

        try:
            session_row = await self._crud.create_session(room_name=room_name, room_url=room.url)
        except Exception as exc:
            raise HTTPException(
                status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="RESUME ROOM: Failed to create the session record.",
            ) from exc

        session_id = session_row["session_id"]

        self._queues[session_id] = asyncio.Queue(maxsize=1000)
        
        analysis_task = asyncio.create_task(
            run_resume_analysis_worker(session_id, self._queues[session_id], self._crud),
            name=f"resume-analysis-{session_id}"
        )

        self._tasks[f"analysis_{session_id}"] = analysis_task

        self._speaking_queues[session_id] = asyncio.Queue(maxsize=1000)

        completeness_task = asyncio.create_task(
            run_silence_completeness_worker(
                session_id, self._crud, self._speaking_queues[session_id], self
            ),
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
            cancel_grading_tasks(session_id)
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


    async def stop_session(self, room_name: str) -> StopSessionResponse:
        try:
            row = await self._crud.get_active_by_room_name(room_name)
        except Exception as exc:
            raise HTTPException(
                status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="RESUME ROOM: Failed to look up the session.",
            ) from exc

        if row:
            session_id = row["session_id"]
            task = self._tasks.get(session_id)
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            else:
                try:
                    await self._crud.mark_finished(session_id, STATUS_ENDED)
                except Exception:
                    pass
                try:
                    await self._delete_room(room_name)
                except Exception:
                    pass

        return StopSessionResponse(ok=True)


    async def shutdown(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        if self._http and not self._http.closed:
            await self._http.close()


_orchestrator: Optional[ResumeRoomOrchestrator] = None


def get_orchestrator_instance() -> ResumeRoomOrchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = ResumeRoomOrchestrator()
    return _orchestrator
