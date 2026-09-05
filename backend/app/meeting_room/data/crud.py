import asyncio
import copy
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple
import json
from pathlib import Path

from loguru import logger

from app.core.config import settings
from app.meeting_room.data.crud_interfaces import STATUS_ACTIVE
from app.meeting_room.resume_analysis_pipeline.completeness_status import (
    merge_status_preserving_terminal,
)
from app.meeting_room.resume_analysis_pipeline.config_jsons_definitions.resume_schema import empty_resume
from app.meeting_room.resume_analysis_pipeline.merge import (
    apply_resolved_conflicts,
    is_redundant_with_accepted_update,
    merge_unresolved,
    merge_updates,
    remove_unresolved,
)

JSON_EXPORT_DIR = Path(__file__).resolve().parents[3]/"json"

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_session_duration(started_at: Optional[str]) -> int:
    if not started_at:
        return 0
    try:
        started_dt = datetime.fromisoformat(started_at)
        if started_dt.tzinfo is None:
            started_dt = started_dt.replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - started_dt).total_seconds()))
    except (ValueError, TypeError):
        return 0


class InMemoryResumeRoomCRUD:
    def __init__(self) -> None:
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        # Per-session lock guarding only the debug-export write itself (never
        # the state lock above) -- see _schedule_write.
        self._write_locks: Dict[str, asyncio.Lock] = {}
        # Keeps background write tasks referenced so they aren't GC'd mid-flight.
        self._write_tasks: set = set()


    def _get_write_lock(self, session_id: str) -> asyncio.Lock:
        lock = self._write_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._write_locks[session_id] = lock
        return lock


    def _write_resume_json_sync(self, session_id: str, row: Dict[str, Any]) -> None:
        JSON_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        path = JSON_EXPORT_DIR / f"{session_id}.json"

        final_pass_completed = row.get("final_pass_completed", False)
        ended_at = row.get("ended_at")
        session_duration = (
            row.get("session_duration", 0)
            if ended_at
            else compute_session_duration(row.get("started_at"))
        )

        export = {
            "session_id": row["session_id"],
            "status": "completed" if final_pass_completed else "pending",
            "final_pass_completed": final_pass_completed,
            "session_status": row["status"],
            "start_time": row.get("started_at"),
            "end_time": ended_at,
            "session_duration": session_duration,
            "llm_calls": row["llm_cost"]["calls"],
            "llm_cost": row["llm_cost"],
            "resume_data": row["resume_data"],
            "questions": row.get("questions", {}),
        }

        with path.open("w", encoding="utf-8") as f:
            json.dump(export, f, indent=2, ensure_ascii=False)

        status_path = JSON_EXPORT_DIR / f"{session_id}_status.json"
        status_export = {
            "field_completeness": row.get("field_completeness", {}),
        }
        with status_path.open("w", encoding="utf-8") as f:
            json.dump(status_export, f, indent=2, ensure_ascii=False)


    async def _write_resume_json(self, session_id: str, row: Dict[str, Any]) -> None:
        # Serialized per-session (not by the state lock) so two snapshots
        # taken back-to-back can't race and have the older one clobber the
        # newer one on disk.
        async with self._get_write_lock(session_id):
            try:
                await asyncio.to_thread(self._write_resume_json_sync, session_id, row)
            except Exception:
                logger.exception(f"RESUME ROOM: Failed to write debug JSON export for session {session_id}")


    def _schedule_write(self, session_id: str, row: Dict[str, Any]) -> None:
        """Snapshot `row` now, while the caller still holds `self._lock`, and
        write the debug JSON export in the background.

        This export is fail-soft and purely diagnostic (see
        _write_resume_json's swallowed exceptions) -- no CRUD caller should
        ever have to wait on its disk I/O. Previously every mutating method
        awaited the write while still holding `self._lock`, so one session's
        (growing, over a long interview) JSON write blocked every other
        in-memory mutation across ALL sessions and workers sharing that one
        lock -- including the interview director's own back-to-back verdict
        writes racing the periodic completeness worker's. Deep-copying here,
        before the lock is released, keeps the snapshot consistent with the
        mutation that was just applied.
        """
        snapshot = copy.deepcopy(row)
        task = asyncio.create_task(self._write_resume_json(session_id, snapshot))
        self._write_tasks.add(task)
        task.add_done_callback(self._write_tasks.discard)



    async def create_session(self, *, room_name: str, room_url: str) -> Dict[str, Any]:
        session_id = str(uuid.uuid4())
        row = {
            "session_id": session_id,
            "room_name": room_name,
            "room_url": room_url,
            "status": STATUS_ACTIVE,
            "transcript": [],
            "resume_data": empty_resume(),
            "field_completeness": {},
            "questions": {
                "current_round_id": None,
                "awaiting_answer": False,
                "round_order": [],
                "rounds": {},
            },
            "final_pass_completed": False,
            "llm_cost": {
                "calls": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "cost_usd": 0.0,
            },
            "session_duration": 0,
            "started_at": now_iso(),
            "ended_at": None,
            "created_at": now_iso(),
        }

        async with self._lock:
            self._sessions[session_id] = row
            self._schedule_write(session_id, row)
        return row


    async def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        return self._sessions.get(session_id)


    async def append_transcript_line(self, session_id: str, role: str, text: str) -> None:
        async with self._lock:
            row = self._sessions.get(session_id)
            if row is None:
                return
            row["transcript"].append({"role": role, "text": text, "ts": now_iso()})


    def _fold_llm_usage(self, row: Dict[str, Any], llm_usage: Optional[Dict[str, Any]]) -> None:
        if not llm_usage:
            return
        cost = row["llm_cost"]
        cost["calls"] += 1
        cost["prompt_tokens"] += llm_usage.get("prompt_tokens") or 0
        cost["completion_tokens"] += llm_usage.get("completion_tokens") or 0
        cost["total_tokens"] += llm_usage.get("total_tokens") or 0
        cost["cost_usd"] += llm_usage.get("cost") or 0.0


    async def apply_resume_update(
        self,
        session_id: str,
        updates: Optional[Dict[str, Any]],
        *,
        unresolved: Optional[List[Dict[str, Any]]] = None,
        resolved_conflicts: Optional[List[Dict[str, Any]]] = None,
        resolved_unresolved_ids: Optional[List[str]] = None,
        llm_usage: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[str], List[str]]:
        async with self._lock:
            row = self._sessions.get(session_id)
            if row is None:
                return [], []

            resume = row["resume_data"]
            accepted: List[str] = []
            rejected: List[str] = []
            if updates:
                _, accepted, rejected = merge_updates(resume, updates)

            if resolved_conflicts:
                apply_resolved_conflicts(resume, resolved_conflicts)
            if resolved_unresolved_ids:
                remove_unresolved(resume, resolved_unresolved_ids)
            if unresolved and accepted:
                kept = []
                for item in unresolved:
                    text = item.get("text") if isinstance(item, dict) else None
                    if text and is_redundant_with_accepted_update(
                        text, updates, accepted,
                        min_shared_tokens=settings.resume_room_min_evidence_tokens,
                    ):
                        logger.info(
                            "RESUME ROOM: dropped redundant unresolved entry for session {} "
                            "(block={}) -- same-response updates already confidently covered it: {!r}",
                            session_id, item.get("block"), text,
                        )
                        continue
                    kept.append(item)
                unresolved = kept
            if unresolved:
                merge_unresolved(resume, unresolved)

            self._fold_llm_usage(row, llm_usage)
            self._schedule_write(session_id, row)
            return accepted, rejected


    async def apply_final_resolution(
        self,
        session_id: str,
        updates: Optional[Dict[str, Any]],
        *,
        llm_usage: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[str], List[str]]:
        async with self._lock:
            row = self._sessions.get(session_id)
            if row is None:
                return [], []

            resume = row["resume_data"]
            accepted: List[str] = []
            rejected: List[str] = []
            if updates:
                _, accepted, rejected = merge_updates(resume, updates, force_overwrite=True)

            resume["conflicts"] = []
            resume["unresolved"] = []

            row["final_pass_completed"] = True
            self._fold_llm_usage(row, llm_usage)
            self._schedule_write(session_id, row)
            return accepted, rejected


    async def apply_field_completeness(
        self,
        session_id: str,
        completeness_status: Dict[str, Any],
        *,
        llm_usage: Optional[Dict[str, Any]] = None,
    ) -> None:
        async with self._lock:
            row = self._sessions.get(session_id)
            if row is None:
                return

            # Fold rather than assign: the batched grader reads resume_data
            # only, so it cannot see a verdict that came from the conversation
            # (a decline, or an answer the director already graded SUFFICIENT).
            # Assigning wholesale used to wipe those out every silence cycle.
            row["field_completeness"] = merge_status_preserving_terminal(
                row.get("field_completeness") or {}, completeness_status or {},
            )
            self._fold_llm_usage(row, llm_usage)
            self._schedule_write(session_id, row)


    async def start_round(
        self,
        session_id: str,
        *,
        question_text: str,
        forced_topic: Optional[str] = None,
        max_questions: Optional[int] = None,
        target: Optional[Dict[str, Any]] = None,
        turn_latency_seconds: Optional[float] = None,
    ) -> Optional[str]:
        """Opens a brand-new round for `question_text` and returns its id.

        `forced_topic` stamps why this round exists when it wasn't picked by
        Task B ("conflict:<id>" / "unresolved:<id>" / "gap:<block>") -- None
        for an ordinary organically-chosen question. `max_questions`, when
        given, overrides the settings default for this one round (not
        currently used by any caller, but the round itself, not a global,
        is where the budget belongs). `target` is `{"block", "item_id",
        "field"}` (the latter two optional/nullable) describing what this
        round's question is about -- stored verbatim so a later
        UNABLE_TO_ANSWER grade can be committed back into
        field_completeness precisely. `turn_latency_seconds`, when given, is
        the elapsed `time.monotonic()` seconds between the PREVIOUS answer
        being recorded and this question being queued for TTS -- the caller
        (`InterviewDirector`) computes this directly rather than leaving it
        to be derived from `asked_at`/`answered_at` timestamp arithmetic;
        `None` for the opening question / idle recovery, which have no
        preceding graded answer."""
        async with self._lock:
            row = self._sessions.get(session_id)
            if row is None:
                return None

            questions = row["questions"]
            round_id = uuid.uuid4().hex[:8]
            now = now_iso()
            questions["rounds"][round_id] = {
                "round_id": round_id,
                "status": "open",
                "grade": None,
                "forced_topic": forced_topic,
                "target": target,
                "max_questions": (
                    max_questions
                    if max_questions is not None
                    else settings.resume_room_max_questions_per_round
                ),
                "exchanges": [
                    {
                        "question": question_text, "answer": None, "asked_at": now, "answered_at": None,
                        "latency_seconds": turn_latency_seconds,
                    },
                ],
                "opened_at": now,
                "closed_at": None,
            }
            questions["round_order"].append(round_id)
            questions["current_round_id"] = round_id
            questions["awaiting_answer"] = True

            self._schedule_write(session_id, row)
            return round_id


    async def append_round_question(
        self,
        session_id: str,
        round_id: str,
        question_text: str,
        *,
        turn_latency_seconds: Optional[float] = None,
    ) -> None:
        """Appends a probe exchange to an already-open round -- same round,
        same forced_topic/max_questions, one more question/answer pair.
        `turn_latency_seconds` -- see `start_round`'s docstring; same
        measurement, landing on this probe exchange instead of a round's
        first one."""
        async with self._lock:
            row = self._sessions.get(session_id)
            if row is None:
                return

            questions = row["questions"]
            round_row = questions["rounds"].get(round_id)
            if round_row is None:
                return

            round_row["exchanges"].append({
                "question": question_text, "answer": None,
                "asked_at": now_iso(), "answered_at": None,
                "latency_seconds": turn_latency_seconds,
            })
            questions["current_round_id"] = round_id
            questions["awaiting_answer"] = True
            self._schedule_write(session_id, row)


    async def record_round_answer(
        self, session_id: str, round_id: str, answer_text: str, *, answered_at: Optional[str] = None,
    ) -> None:
        """Fills in the answer/answered_at on the round's most recent
        unanswered exchange and clears awaiting_answer. `answered_at`, when
        given, should be the moment the answer was finalized (silence
        debounce elapsed, right before grading started) -- callers that defer
        this write until after a grading LLM call has returned must pass the
        earlier timestamp explicitly, or `asked_at`/`answered_at` on the NEXT
        exchange minus this one would measure nothing but this call's own
        deferral instead of real turn latency. Falls back to now() only if no
        better timestamp is given."""
        async with self._lock:
            row = self._sessions.get(session_id)
            if row is None:
                return

            questions = row["questions"]
            round_row = questions["rounds"].get(round_id)
            if round_row is None:
                return

            for exchange in reversed(round_row["exchanges"]):
                if exchange.get("answer") is None:
                    exchange["answer"] = answer_text
                    exchange["answered_at"] = answered_at or now_iso()
                    break

            questions["awaiting_answer"] = False
            self._schedule_write(session_id, row)


    async def close_round(
        self,
        session_id: str,
        round_id: str,
        *,
        grade: str,
        llm_usage: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Terminal write for one round: stamps its grade and closed_at, and
        clears current_round_id/awaiting_answer if this was still the active
        round. Folds llm_usage the same way every other committing method
        does."""
        async with self._lock:
            row = self._sessions.get(session_id)
            if row is None:
                return

            questions = row["questions"]
            round_row = questions["rounds"].get(round_id)
            if round_row is not None:
                round_row["status"] = "closed"
                round_row["grade"] = grade
                round_row["closed_at"] = now_iso()

            if questions.get("current_round_id") == round_id:
                questions["current_round_id"] = None
                questions["awaiting_answer"] = False

            self._fold_llm_usage(row, llm_usage)
            self._schedule_write(session_id, row)


    async def mark_finished(self, session_id: str, status: str, error: Optional[str] = None) -> None:
        async with self._lock:
            row = self._sessions.get(session_id)
            if row is None or row["status"] != STATUS_ACTIVE:
                return
            row["status"] = status
            row["ended_at"] = now_iso()
            row["session_duration"] = compute_session_duration(row.get("started_at"))

    async def list_active(self) -> List[Dict[str, Any]]:
        return [row for row in self._sessions.values() if row["status"] == STATUS_ACTIVE]

    async def get_active_by_room_name(self, room_name: str) -> Optional[Dict[str, Any]]:
        for row in self._sessions.values():
            if row["room_name"] == room_name and row["status"] == STATUS_ACTIVE:
                return row
        return None

    async def count_active(self) -> int:
        return len(await self.list_active())



@lru_cache()
def get_resume_room_crud() -> InMemoryResumeRoomCRUD:
    return InMemoryResumeRoomCRUD()
