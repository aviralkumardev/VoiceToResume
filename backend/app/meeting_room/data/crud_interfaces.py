from typing import Any, Dict, List, Optional, Protocol, Tuple

STATUS_ACTIVE = "active"
STATUS_ENDED = "ended"
STATUS_FAILED = "failed"
STATUS_TIMED_OUT = "timed_out"


class ResumeRoomCRUD(Protocol):
    """Structural interface the orchestrator depends on — any object with these
    methods works, in-memory or otherwise."""

    async def create_session(self, *, room_name: str, room_url: str) -> Dict[str, Any]:
        """Create a new active session row and return it."""
        ...

    async def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a session row by its id, or None if it doesn't exist."""
        ...

    async def append_transcript_line(self, session_id: str, role: str, text: str) -> None:
        """Append one transcript line to the session's transcript."""
        ...

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
        """Merge `updates` into the session's resume_data (if truthy), apply
        any resolved_conflicts/resolved_unresolved_ids, append any newly
        flagged unresolved items, and fold llm_usage into the running cost
        accumulator (regardless of whether updates is truthy — a no_update
        batch still cost tokens). Returns (accepted, rejected) field-path
        lists from the merge."""
        ...

    async def apply_final_resolution(
        self,
        session_id: str,
        updates: Optional[Dict[str, Any]],
        *,
        llm_usage: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[str], List[str]]:
        """Force-applies `updates` from the session-end final resolution pass
        (bypassing conflict-diversion), then unconditionally clears
        resume_data's conflicts and unresolved lists, then folds llm_usage
        the same way as apply_resume_update. Also marks the session's
        final_pass_completed flag True, regardless of whether `updates` was
        truthy. Returns (accepted, rejected)."""
        ...


    async def apply_field_completeness(
        self,
        session_id: str,
        completeness_status: Dict[str, Any],
        *,
        llm_usage: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Fold the freshly graded result (see
        completeness_status.merge_completeness) onto the session's stored
        field_completeness via merge_status_preserving_terminal: the fresh
        result wins everywhere except a leaf whose stored verdict is already
        terminal (SUFFICIENT / UNABLE_TO_ANSWER), which the batched grader
        cannot re-derive because it only ever sees resume_data. Also folds
        llm_usage into the running cost accumulator the same way
        apply_resume_update does. Committed unconditionally the instant it's
        called — the caller (silence_completeness_worker) is responsible for
        only calling this once a result is final and should not be
        discarded."""
        ...

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
        """Opens a brand-new question round: creates
        questions.rounds[round_id] with a single {"question", "answer": None,
        ...} exchange, appends round_id to questions.round_order, sets
        questions.current_round_id=round_id and awaiting_answer=True, and
        returns round_id. `forced_topic` records why this round exists when
        it wasn't picked by the question-grading chain ("conflict:<id>" /
        "unresolved:<id>" / "gap:<block>"), else None. `max_questions`
        defaults to settings.resume_room_max_questions_per_round when not
        given, and is stamped on the round once at creation. `target` is
        `{"block", "item_id", "field"}` (the latter two optional/nullable)
        describing what this round's question is about -- self-reported by
        the fused question chain (sanitized by the caller) for an organic
        question, built directly from the record for a forced conflict/
        unresolved topic, or `{"block": gap_block}` for a required-gap
        topic; `None` for the multi-block opening question. Stored verbatim
        on the round so a later UNABLE_TO_ANSWER grade can be committed back
        into field_completeness precisely (see
        `completeness_status.build_unable_to_answer_patch`). `turn_latency_seconds`,
        when given, is stored verbatim on this exchange as `latency_seconds`
        -- the caller's own `time.monotonic()`-measured elapsed seconds since
        the previous answer was recorded, i.e. real "answer -> next question
        asked" latency, computed directly rather than left for a reader to
        derive from `asked_at`/`answered_at`. `None` for the opening question
        / idle recovery, which have no preceding graded answer."""
        ...

    async def append_round_question(
        self,
        session_id: str,
        round_id: str,
        question_text: str,
        *,
        turn_latency_seconds: Optional[float] = None,
    ) -> None:
        """Appends one more {"question", "answer": None, ...} exchange to an
        already-open round (a probe on the same topic), and sets
        current_round_id/awaiting_answer the same way start_round does.
        `turn_latency_seconds` -- see `start_round`'s docstring; same
        measurement, stored on this probe exchange instead of a round's first
        one. No-op if the round or session doesn't exist."""
        ...

    async def record_round_answer(
        self, session_id: str, round_id: str, answer_text: str, *, answered_at: Optional[str] = None,
    ) -> None:
        """Fills in answer/answered_at on the round's most recent exchange
        whose answer is still None, and clears awaiting_answer. `answered_at`
        should be the moment the candidate's answer was finalized (silence
        debounce elapsed, right before grading started) -- NOT when this
        method happens to be called, since callers commonly defer this write
        until after a grading LLM call has already returned. Defaults to
        now() only if the caller has no better timestamp. No-op if the round
        or session doesn't exist."""
        ...

    async def close_round(
        self,
        session_id: str,
        round_id: str,
        *,
        grade: str,
        llm_usage: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Terminal write for a round: sets status="closed", stamps `grade`
        and closed_at. Clears questions.current_round_id/awaiting_answer if
        this round was still the active one. Folds llm_usage into the
        running cost accumulator the same way every other committing method
        does. Committed unconditionally, same semantics as
        apply_field_completeness."""
        ...


    async def mark_finished(self, session_id: str, status: str, error: Optional[str] = None) -> None:
        """Transition an active session to a terminal status, no-op if already terminal."""
        ...

    async def list_active(self) -> List[Dict[str, Any]]:
        """Return every session row currently in the active status."""
        ...

    async def get_active_by_room_name(self, room_name: str) -> Optional[Dict[str, Any]]:
        """Fetch the active session row for a given Daily room name, if any."""
        ...

    async def count_active(self) -> int:
        """Count how many sessions are currently active."""
        ...
