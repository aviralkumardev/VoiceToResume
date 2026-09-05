import asyncio
from typing import Any, Dict, Optional

from app.core.config import settings
from app.meeting_room.data.crud_interfaces import ResumeRoomCRUD
from app.meeting_room.resume_analysis_pipeline.analysis_chain import run_resume_final_resolution_chain
from app.meeting_room.resume_analysis_pipeline.combined_chain import run_combined_chain
from app.meeting_room.resume_analysis_pipeline.completeness_status import (
    merge_completeness,
    prune_for_judgment,
)
from app.meeting_room.resume_analysis_pipeline.config_jsons_definitions.coverage_schema import (
    ASKABLE_COVERAGE_SCHEMA,
    COVERAGE_SCHEMA,
)
from app.meeting_room.resume_analysis_pipeline.next_target import compute_candidate_queue, gap_key


class FlushRequest:
    """Sentinel put on the transcript queue to force whatever's accumulated
    so far through one combined-analysis batch, out of turn with the
    char-count trigger. `done` is set once that batch (if any) has landed,
    so a caller can await it instead of reading a stale snapshot -- see
    ResumeRoomOrchestrator.flush_transcript."""

    __slots__ = ("done",)

    def __init__(self) -> None:
        self.done = asyncio.Event()


def _cap_carry(text: str) -> str:
    max_chars = (settings.resume_room_extraction_trigger_chars*settings.resume_room_extraction_max_carry_multiple)
    if len(text) > max_chars:
        return text[-max_chars:]
    return text


def _current_round_key(questions: Dict[str, Any]) -> Optional[str]:
    """The currently-open round's own candidate key (gap:/conflict:/
    unresolved:), so it's excluded from this cycle's candidate computation
    while it's still being asked -- the same exclusion `_finish_answer` used
    to apply inline before candidate-list computation moved to this worker."""
    current_round_id = questions.get("current_round_id")
    if not current_round_id:
        return None
    round_row = (questions.get("rounds") or {}).get(current_round_id) or {}
    forced = round_row.get("forced_topic")
    if forced:
        return forced
    target = round_row.get("target")
    if not target:
        return None
    return gap_key(target.get("block"), target.get("item_id"))


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
    field_completeness = session.get("field_completeness", {})
    questions = session.get("questions", {})

    already_decided, to_judge = prune_for_judgment(resume, COVERAGE_SCHEMA, field_completeness)

    excluded = set(questions.get("given_up_targets", [])) | set(questions.get("forced_topics_spent", []))
    current_key = _current_round_key(questions)
    if current_key:
        excluded.add(current_key)

    candidates = compute_candidate_queue(
        resume, ASKABLE_COVERAGE_SCHEMA, field_completeness, excluded_keys=frozenset(excluded),
    )

    result = await run_combined_chain(
        resume, COVERAGE_SCHEMA, to_judge, candidates, input_text,
        last_asked_question=questions.get("last_asked_question"),
        more_items_checked=questions.get("more_items_checked", []),
    )

    updates = result.get("updates") if result.get("status") in ("update", "extracted") else None
    accepted, rejected = await crud.apply_resume_update(
        session_id,
        updates,
        unresolved=result.get("unresolved"),
        resolved_conflicts=result.get("resolved_conflicts"),
        resolved_unresolved_ids=result.get("resolved_unresolved_ids"),
        llm_usage=result.get("_llm_usage"),
    )

    merged_completeness = merge_completeness(already_decided, result.get("blocks") or {}, COVERAGE_SCHEMA)
    await crud.apply_field_completeness(session_id, merged_completeness)

    # None (not []) means the combined call failed outright this cycle --
    # leave the persisted queue untouched rather than reading a transient
    # provider/schema error as "genuinely nothing left to ask". See
    # combined_chain._empty_result / _validate_queue.
    if result.get("queue") is not None:
        await crud.apply_question_queue(session_id, result["queue"])

    if result.get("more_items_asked"):
        await crud.mark_more_items_checked(session_id, result["more_items_asked"])

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
