import asyncio
from typing import Any, Optional

from loguru import logger

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
    queue: "asyncio.Queue[Optional[bool]]",
    orchestrator: Any = None,
) -> None:
    """Consumes `True`/`False`/`None` speaking-state events for one session,
    running the hardbound-wait-then-judge cycle as a single cancellable
    in-flight task. Mirrors run_resume_analysis_worker's per-session
    consume-until-None shape, but keyed off speaking state instead of
    transcript text.

    This grades the whole resume against the coverage rubric and nothing
    else -- picking a target and asking about it belongs to
    InterviewDirector (stt_tts_pipeline/interview_director.py), which runs
    inside the bot and commits its own per-answer verdicts. Both write
    `field_completeness`; this one keeps it in sync with whatever the
    candidate volunteers in free conversation, the director keeps it in
    sync with what it explicitly asked about.

    `orchestrator` (untyped here to avoid a circular import with
    room_orchestrator.py, which constructs this worker) is only used for
    `flush_transcript` -- optional so a caller without one just skips it and
    grades whatever resume_data currently holds, as before.
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
                pending_task = asyncio.create_task(
                    _safe_run_one_cycle(session_id, crud, orchestrator)
                )

    finally:
        if pending_task is not None and not pending_task.done():
            pending_task.cancel()


async def _safe_run_one_cycle(
    session_id: str, crud: ResumeRoomCRUD, orchestrator: Any = None
) -> None:
    """Fail-soft wrapper, matching _safe_run_batch in the extraction pipeline.

    Nothing ever awaits this task, so an escaping exception surfaces as a bare
    "Task exception was never retrieved" traceback and silently ends that
    grading cycle. One malformed LLM response must not do that: the next
    silence starts a fresh cycle, and field_completeness is simply a cycle
    staler in the meantime.
    """
    try:
        await _run_one_cycle(session_id, crud, orchestrator)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("silence completeness cycle failed for session {}", session_id)


async def _run_one_cycle(
    session_id: str, crud: ResumeRoomCRUD, orchestrator: Any = None
) -> None:
    try:
        await asyncio.sleep(settings.resume_room_silence_hardbound_seconds)
    except asyncio.CancelledError:
        return

    if orchestrator is not None:
        await orchestrator.flush_transcript(session_id)

    try:
        await run_completeness_grading_cycle(session_id, crud)
    except asyncio.CancelledError:
        return


async def run_completeness_grading_cycle(session_id: str, crud: ResumeRoomCRUD) -> None:
    """One grading pass: prune -> grade what changed -> merge -> commit.

    Shared by the silence-triggered cycle above (which wraps this in its own
    hardbound-wait + flush + cancellation handling) and by the
    extraction-pipeline's post-batch trigger (analysis_orchestrator.py),
    which calls this directly, uncancellable, right after a batch commits.
    Cancellation is deliberately the caller's concern, not this function's --
    the two callers want different behavior on cancel.
    """
    row = await crud.get_session(session_id)
    if row is None:
        return

    resume = row["resume_data"]
    previous_status = row.get("field_completeness", {})

    already_decided, to_judge = prune_for_judgment(resume, COVERAGE_SCHEMA, previous_status)

    if not to_judge:
        return

    result = await run_completeness_chain(to_judge, COVERAGE_SCHEMA)

    merged = merge_completeness(already_decided, result.get("blocks", {}), COVERAGE_SCHEMA)

    await asyncio.shield(
        crud.apply_field_completeness(session_id, merged, llm_usage=result.get("_llm_usage"))
    )
