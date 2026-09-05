import asyncio
from collections import defaultdict
from typing import Dict, Optional, Set

from loguru import logger

from app.core.config import settings
from app.meeting_room.data.crud_interfaces import ResumeRoomCRUD
from app.meeting_room.resume_analysis_pipeline.analysis_chain import (run_resume_extraction_chain, run_resume_final_resolution_chain)
from app.meeting_room.resume_analysis_pipeline.silence_completeness_worker import run_completeness_grading_cycle


class FlushRequest:
    """Sentinel put on the transcript queue to force whatever's accumulated
    so far through one extraction batch, out of turn with the char-count
    trigger. `done` is set once that batch (if any) has landed in
    resume_data, so a caller can await it instead of reading a stale
    snapshot -- see ResumeRoomOrchestrator.flush_transcript."""

    __slots__ = ("done",)

    def __init__(self) -> None:
        self.done = asyncio.Event()


def _cap_carry(text: str) -> str:
    max_chars = (settings.resume_room_extraction_trigger_chars*settings.resume_room_extraction_max_carry_multiple)
    if len(text) > max_chars:
        return text[-max_chars:]
    return text


# Fire-and-forget completeness-grading tasks spawned after a batch that
# changed something, keyed by session_id so they can be cancelled at
# teardown. Strong refs here are load-bearing -- asyncio does not guarantee
# keeping an unreferenced task alive. See cancel_grading_tasks below.
_grading_tasks: Dict[str, Set[asyncio.Task]] = defaultdict(set)


def cancel_grading_tasks(session_id: str) -> None:
    """Cancels and drops any in-flight post-extraction grading tasks for a
    session. Called from room_orchestrator._close_out so a task doesn't
    keep running (and eventually write into a gone session) past teardown.
    """
    for task in _grading_tasks.pop(session_id, ()):
        if not task.done():
            task.cancel()


async def _safe_run_completeness_grading_cycle(session_id: str, crud: ResumeRoomCRUD) -> None:
    """Fail-soft wrapper, matching _safe_run_batch. Nothing awaits this task,
    so an escaping exception would otherwise surface only as a bare "Task
    exception was never retrieved" traceback."""
    try:
        await run_completeness_grading_cycle(session_id, crud)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("post-extraction completeness grading failed for session {}", session_id)


async def _safe_run_batch(
    session_id: str,
    accumulated_text: str,
    remaining_text: str,
    crud: ResumeRoomCRUD,
) -> str:
    try:
        return await _run_batch(session_id, accumulated_text, remaining_text, crud)
    except Exception:
        return _cap_carry(remaining_text + accumulated_text)


async def _run_batch(
    session_id: str,
    accumulated_text: str,
    remaining_text: str,
    crud: ResumeRoomCRUD,
) -> str:

    input_text = remaining_text + accumulated_text
    session = await crud.get_session(session_id)
    if session is None:
        return ""

    resume = session.get("resume_data", {})
    result = await run_resume_extraction_chain(resume, input_text)

    updates = result.get("updates") if result.get("status") in ("update", "extracted") else None
    accepted, rejected = await crud.apply_resume_update(
        session_id,
        updates,
        unresolved=result.get("unresolved"),
        resolved_conflicts=result.get("resolved_conflicts"),
        resolved_unresolved_ids=result.get("resolved_unresolved_ids"),
        llm_usage=result.get("_llm_usage"),
    )

    # No claim reconciliation here on purpose. The director flushes this queue
    # and awaits the batch immediately before it selects the next target, then
    # reconciles there -- so the check happens once, at the point where its
    # result is actually used, instead of on every batch.

    # Keep field_completeness continuously fresh instead of only on silence:
    # fire a background grading cycle whenever this batch actually changed
    # something. Off the critical path (create_task, not awaited) so it never
    # adds latency to extraction itself. See silence_completeness_worker.py's
    # run_completeness_grading_cycle -- same grading logic the silence-EOT
    # worker uses, just triggered here as well, not instead.
    changed = result.get("status") != "no_update" and (
        bool(accepted)
        or bool(result.get("unresolved"))
        or bool(result.get("resolved_conflicts"))
        or bool(result.get("resolved_unresolved_ids"))
    )
    if changed:
        task = asyncio.create_task(_safe_run_completeness_grading_cycle(session_id, crud))
        _grading_tasks[session_id].add(task)
        task.add_done_callback(lambda t, sid=session_id: _grading_tasks[sid].discard(t))

    return _cap_carry(result.get("remaining_text") or "")


async def _safe_run_final_pass(
    session_id: str,
    crud: ResumeRoomCRUD
) -> None:
    try:
        await _run_final_pass(session_id, crud)
    except Exception as exc:
        pass


async def _run_final_pass(
    session_id: str,
    crud: ResumeRoomCRUD
) -> None:
    session = await crud.get_session(session_id)
    if session is None:
        return

    full_transcript = "\n".join(
        line["text"] for line in session.get("transcript", []) if line.get("role") == "user"
    )

    updates = None
    llm_usage = None
    if full_transcript.strip():
        resume = session.get("resume_data", {})
        result = await run_resume_final_resolution_chain(resume, full_transcript)
        updates = result.get("updates")
        llm_usage = result.get("_llm_usage")

    accepted, rejected = await crud.apply_final_resolution(
        session_id, updates, llm_usage=llm_usage,
    )


async def run_resume_analysis_worker(
    session_id: str,
    queue: "asyncio.Queue[Optional[str]]",
    crud: ResumeRoomCRUD
) -> None:

    accumulated_text = ""
    remaining_text = ""
    trigger = settings.resume_room_extraction_trigger_chars

    try:
        while True:
            chunk = await queue.get()

            if chunk is None:
                await _safe_run_final_pass(session_id, crud)
                break

            if isinstance(chunk, FlushRequest):
                if accumulated_text:
                    remaining_text = await _safe_run_batch(
                        session_id, accumulated_text, remaining_text, crud,
                    )
                    accumulated_text = ""
                chunk.done.set()
                continue

            accumulated_text += chunk + "\n"
            if len(accumulated_text) >= trigger:
                remaining_text = await _safe_run_batch(
                    session_id, accumulated_text, remaining_text, crud,
                )

                accumulated_text = ""
    except asyncio.CancelledError:
        pass
    except Exception:
        pass
    finally:
        pass
    