import asyncio
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

from loguru import logger
from pipecat.frames.frames import TTSSpeakFrame

from app.core.config import settings
from app.meeting_room.data.crud_interfaces import ResumeRoomCRUD
from app.meeting_room.resume_analysis_pipeline.config_jsons_definitions.coverage_schema import (
    ASKABLE_COVERAGE_SCHEMA,
    COVERAGE_SCHEMA,
)
from app.meeting_room.resume_analysis_pipeline.completeness_status import (
    build_unable_to_answer_patch,
)
from app.meeting_room.resume_analysis_pipeline.question_chain import (
    ANSWER_GRADE_UNABLE_TO_ANSWER,
    TERMINAL_GRADES,
    run_question_chain,
    run_topic_question_chain,
)
from app.meeting_room.resume_analysis_pipeline.required_gap import find_required_gap
from app.meeting_room.resume_analysis_pipeline.silence_completeness_worker import (
    run_completeness_grading_cycle,
)

CLOSING_MESSAGE = (
    "That covers everything I wanted to ask -- thank you, and nice work "
    "practicing your resume walkthrough today!"
)


class InterviewDirector:
    """Drives the ENTIRE conversation for the session -- there is no persona
    LLM anywhere; this is the sole source of everything the bot says.

    Two states:
      * idle -- nothing pending. The candidate falling silent for
        `resume_room_silence_hardbound_seconds` triggers `_advance_round`,
        which decides what (if anything) to ask about next.
      * awaiting_answer -- a question has been spoken. The candidate's
        speech is buffered here instead of reaching the transcript live.
        Falling silent for `resume_room_answer_silence_seconds` ends the
        answer: it gets graded by the ONE fused LLM call in
        `question_chain.run_question_chain`, which grades the answer AND
        drafts both a probe (for if it stays open) and a next question (for
        if it resolves) in the same response. There is no backend target
        selection, shortlist, or menu the LLM must pick from -- it reasons
        freely over the whole resume and coverage rubric.

    Every question lives inside a ROUND (`data/crud.py`'s
    `questions.rounds`): one round per subject, holding one or more
    exchanges (the opening question plus any probes) under a per-round
    question budget (`resume_room_max_questions_per_round`). A round closes
    once the fused call's `answer_grade` is terminal (SUFFICIENT /
    UNABLE_TO_ANSWER) or the budget runs out.

    Two narrow, deterministic Python guardrails sit on top of the LLM's own
    judgment, checked only at round-open decisions (`_advance_round`), never
    mid-round:
      1. `_pick_forced_topic` -- an outstanding conflict/unresolved record in
         the resume jumps the queue ahead of whatever the LLM would have
         asked next. Settlement still happens purely through extraction
         (Task A); the director never writes anything to settle one itself.
      2. `find_required_gap` -- before ending the interview because the LLM
         said there's nothing left to ask, a required coverage block that's
         still genuinely open forces one more question instead.
      `_forced_topics_spent` bounds both to at most one forced round each,
      so a candidate who keeps dodging (or extraction that hasn't caught up
      yet) can't loop the interview forever.

    Extraction (Task A) is triggered out-of-turn every answer
    (`orchestrator.flush_transcript(..., wait=False)`, fire-and-forget) but
    is otherwise off the critical path entirely -- the turn's only
    critical-path work is the fused grading call. Task A is only ever
    AWAITED once, in `_await_task_a_settle`, immediately before the
    required-gap safety net reads `field_completeness` to decide whether the
    interview can end.

    Questions are spoken straight through TTS (TTSSpeakFrame,
    append_to_context=False). `pipeline.py` additionally suppresses
    broadcasting an `InterruptionFrame` when the candidate starts talking,
    so a question in flight is never cut off mid-utterance -- speaking-state
    detection (UserStartedSpeakingFrame/UserStoppedSpeakingFrame, which the
    silence timers above depend on) is unaffected by that suppression.
    """

    # How many times one answer may be re-graded after a call came back with
    # nothing usable (no terminal grade, no probe), before we give up and
    # move on. One retry covers the transient provider/schema failure this
    # exists for; more would just keep a broken model in a loop while the
    # candidate waits in silence.
    _MAX_REGRADE_ATTEMPTS = 1

    def __init__(
        self,
        session_id: str,
        crud: ResumeRoomCRUD,
        orchestrator,
        worker,
        *,
        on_complete: Optional[Callable[[], Awaitable[None]]] = None,
    ):
        self._session_id = session_id
        self._crud = crud
        self._orchestrator = orchestrator
        self._worker = worker
        # Called once the closing message has been queued for speech --
        # BotSession.end_session() in practice, which queues an EndFrame
        # right behind it so the pipeline (and with it the Daily room, via
        # the usual _on_bot_done/_close_out teardown) shuts down only after
        # that frame has flowed all the way through TTS/output, not before.
        self._on_complete = on_complete

        self._awaiting_answer = False
        self._current_round_id: Optional[str] = None
        # Why the currently-open round exists, when it wasn't picked
        # organically by the fused call: "conflict:<id>" / "unresolved:<id>"
        # / "gap:<block>", else None. Folded into _forced_topics_spent the
        # moment the round closes.
        self._current_round_forced: Optional[str] = None
        self._current_question_text: Optional[str] = None
        self._buffer: List[str] = []
        self._pending: Optional[asyncio.Task] = None
        self._busy = False
        self._closed = False
        # How many times THIS answer has been re-graded after a grading call
        # came back with nothing usable. Reset every time a question is
        # asked; bounded so a persistently failing call can't loop forever.
        self._regrade_attempts = 0
        # Every forced round's key, added the moment that round closes
        # (terminal or capped), regardless of whether extraction has
        # actually cleared the underlying record/gap yet -- otherwise a
        # just-resolved conflict still sitting in resume_data for one extra
        # turn, or a required block the candidate keeps dodging, would force
        # the identical question again next round-open.
        self._forced_topics_spent: Set[str] = set()


    async def ask_opening_question(self, question: str) -> None:
        """The fixed, LLM-free kickoff -- spoken exactly as given, no
        wording call. Graded like any other round from here on: the opening
        answer may be probed if it's thin, exactly like an ordinary round
        (a deliberate change from the old hardcoded never-re-probe). No
        `target` -- it's a broad multi-block opener, not about one gap."""
        await self._open_round(question, forced=None, target=None)


    def record_candidate_text(self, text: str) -> None:
        """Buffers a finalized STT chunk for the answer currently in
        progress, so _finish_answer can join the whole thing for grading.
        Purely additive -- pipeline.py's persist() also always sends this
        same text straight to the transcript/extraction queue live, so this
        no longer gates anything the way it used to."""
        if self._awaiting_answer and text.strip():
            self._buffer.append(text.strip())


    def on_speaking_change(self, is_speaking: bool) -> None:
        """Same cancel-on-resume debounce the batched completeness worker
        uses, with the wait and the action both depending on which state
        we're in."""
        if is_speaking:
            if self._pending is not None and not self._pending.done():
                self._pending.cancel()
            return

        if self._busy:
            return
        if self._pending is not None and not self._pending.done():
            return

        self._pending = asyncio.create_task(self._run_after_silence())


    def cancel(self) -> None:
        if self._pending is not None and not self._pending.done():
            self._pending.cancel()


    async def _run_after_silence(self) -> None:
        delay = (
            settings.resume_room_answer_silence_seconds
            if self._awaiting_answer
            else settings.resume_room_silence_hardbound_seconds
        )
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return

        self._busy = True
        try:
            # An ungraded answer for a still-open round takes priority over
            # advancing to a new one even if `_awaiting_answer` somehow got
            # cleared (a cycle that died between clearing it and asking the
            # next question) -- without this a lost turn silently becomes
            # "advance to the next round" with nothing to speak from.
            if self._awaiting_answer or (self._current_round_id is not None and self._buffer):
                await self._finish_answer()
            else:
                await self._advance_round()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("interview director cycle failed")
        finally:
            self._busy = False


    async def _open_round(
        self,
        question: str,
        *,
        forced: Optional[str],
        target: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Speaks `question` and opens a brand-new round for it -- the
        opening question of a fresh subject, whether picked organically by
        the fused call or forced by one of the two Python guardrails.
        `target` is `{"block", "item_id", "field"}` describing what the
        question is about (already sanitized/built by the caller), stored on
        the round so a later UNABLE_TO_ANSWER grade can be committed back
        into field_completeness precisely."""
        self._current_question_text = question
        self._buffer = []
        self._awaiting_answer = True
        self._current_round_forced = forced
        self._regrade_attempts = 0

        try:
            await self._worker.queue_frames(
                [TTSSpeakFrame(text=question, append_to_context=False)]
            )
        except Exception:
            logger.exception("failed to speak interview question")

        try:
            self._current_round_id = await asyncio.shield(
                self._crud.start_round(
                    self._session_id,
                    question_text=question,
                    forced_topic=forced,
                    target=target,
                )
            )
        except asyncio.CancelledError:
            pass


    async def _probe_round(self, question: str) -> None:
        """Speaks `question` as one more exchange on the CURRENT round --
        narrowing in on the same subject rather than opening a new one."""
        self._current_question_text = question
        self._buffer = []
        self._awaiting_answer = True
        self._regrade_attempts = 0

        try:
            await self._worker.queue_frames(
                [TTSSpeakFrame(text=question, append_to_context=False)]
            )
        except Exception:
            logger.exception("failed to speak interview question")

        if self._current_round_id is None:
            return
        try:
            await asyncio.shield(
                self._crud.append_round_question(
                    self._session_id, self._current_round_id, question
                )
            )
        except asyncio.CancelledError:
            pass


    def _pick_forced_topic(self, resume: Dict[str, Any]) -> Optional[Tuple[str, Dict[str, Any]]]:
        """The first outstanding conflict, else the first outstanding
        unresolved record, whose forced-topic key hasn't already had its one
        forced round this session. None once nothing forced is left.

        Deliberately no priority machinery beyond "conflicts before
        unresolved, insertion order within each" -- unlike the old
        `select_focus_target` chain this never has to rank these against
        ordinary coverage gaps, since it's the ONLY thing checked before an
        ordinary next_question is allowed to be spoken.
        """
        for record in resume.get("conflicts") or []:
            record_id = record.get("id")
            if not record_id:
                continue
            key = f"conflict:{record_id}"
            if key not in self._forced_topics_spent:
                return key, record

        for record in resume.get("unresolved") or []:
            record_id = record.get("id")
            if not record_id:
                continue
            key = f"unresolved:{record_id}"
            if key not in self._forced_topics_spent:
                return key, record

        return None


    @staticmethod
    def _item_label(
        resume: Dict[str, Any], block: Optional[str], item_id: Optional[str]
    ) -> Optional[str]:
        """A short human-readable label for one specific item of a
        repeatable block (e.g. "Generative AI Intern at AI Solve"), folded
        into forced-topic wording so a conflict/unresolved question about
        one of several entries names which one it means. None when `block`/
        `item_id` don't resolve to an actual existing item."""
        if not block or not item_id:
            return None
        items = resume.get(block) or []
        item = next(
            (it for it in items if isinstance(it, dict) and it.get("id") == item_id), None
        )
        if item is None:
            return None

        def _val(key: str) -> Optional[str]:
            entry = item.get(key)
            if isinstance(entry, dict):
                return entry.get("value")
            if isinstance(entry, str):
                return entry
            return None

        if block == "experience":
            role, company = _val("role"), _val("company")
            if role and company:
                return f"{role} at {company}"
            return role or company
        if block == "education":
            degree, college = _val("degree"), _val("college")
            if degree and college:
                return f"{degree} at {college}"
            return degree or college
        if block in ("projects", "certifications", "courses"):
            return _val("name")
        return None


    @staticmethod
    def _sanitize_target(
        target: Optional[Dict[str, Any]], coverage: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Validates the fused call's self-reported `next_question_target`
        before trusting it: `block` must be a real askable coverage block,
        else the whole target is dropped to None rather than stored
        half-trustworthy. `item_id` is kept only when it's actually a
        string; `fields` is kept only as the subset of its entries that are
        actually real field keys of that block (per `coverage`), dropping
        any hallucinated name -- an empty/invalid list collapses to None,
        same as "whole-block/first-mention question"."""
        if not isinstance(target, dict):
            return None
        block = target.get("block")
        if not isinstance(block, str) or block not in coverage:
            return None
        item_id = target.get("item_id")
        valid_fields = set((coverage[block].get("fields") or {}).keys())
        raw_fields = target.get("fields")
        fields = (
            [f for f in raw_fields if isinstance(f, str) and f in valid_fields]
            if isinstance(raw_fields, list)
            else None
        )
        return {
            "block": block,
            "item_id": item_id if isinstance(item_id, str) else None,
            "fields": fields or None,
        }


    @classmethod
    def _forced_topic_description(
        cls, key: str, record: Dict[str, Any], resume: Dict[str, Any]
    ) -> str:
        """A short, natural-language description of a forced conflict/
        unresolved topic, handed to `run_topic_question_chain` as the thing
        it needs to word a question about."""
        if key.startswith("conflict:"):
            field = record.get("field") or "a detail"
            existing = record.get("existing_value")
            candidates = record.get("candidates") or []
            alt = candidates[0] if candidates else None
            label = cls._item_label(resume, record.get("block"), record.get("item_id"))
            subject = f"the candidate's {field}" + (f" for their {label}" if label else "")
            if existing and alt:
                return (
                    f"resolving a conflict: earlier {subject} was recorded as "
                    f"{existing}, then as {alt} -- need to know which is correct"
                )
            return (
                f"resolving a conflict about {subject} -- two different values "
                "were given earlier, need to know which is correct"
            )

        text = record.get("text") or "something the candidate mentioned earlier"
        return (
            f"clarifying an ambiguous earlier statement: \"{text}\" -- need to "
            "know which part of the resume this belongs to"
        )


    @staticmethod
    def _build_conversation_history(row: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Every question asked and answer given so far this session,
        flattened across every round in order, oldest first -- the whole
        conversation, no per-round windowing or truncation. Handed to both
        LLM chains as `conversation_history`."""
        history: List[Dict[str, Any]] = []
        questions = (row or {}).get("questions") or {}
        rounds = questions.get("rounds") or {}
        for round_id in questions.get("round_order") or []:
            round_row = rounds.get(round_id) or {}
            for exchange in round_row.get("exchanges") or []:
                history.append({
                    "question": exchange.get("question"),
                    "answer": exchange.get("answer"),
                })
        return history


    async def _await_task_a_settle(self) -> None:
        """Bounded catch-up for extraction+grading, called ONLY from the
        required-gap safety net -- never on every turn. Task A is otherwise
        entirely fire-and-forget from this director's point of view; this is
        the one place `field_completeness` freshness actually matters,
        because it's the one place a decision (end the interview, or not)
        depends on it."""
        await self._orchestrator.flush_transcript(self._session_id, wait=True)
        try:
            await asyncio.wait_for(
                run_completeness_grading_cycle(self._session_id, self._crud),
                timeout=settings.resume_room_flush_timeout_seconds,
            )
        except asyncio.TimeoutError:
            pass


    async def _advance_round(
        self,
        next_question: Optional[str] = None,
        next_question_target: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Decides what to open next, in priority order: a forced conflict/
        unresolved topic, else the fused call's own already-drafted
        `next_question` (no further checks -- it had everything it should
        have needed to check), else the required-gap safety net, else the
        interview is genuinely done.

        `next_question` is None both for the idle/cold-start caller
        (`_run_after_silence`, which has no fused response to draw from) and
        whenever `_finish_answer` closed a round with nothing usable to
        carry forward. `next_question_target` is the fused call's own
        (unsanitized) `next_question_target` for that same `next_question`.
        """
        row = await self._crud.get_session(self._session_id)
        if row is None:
            return
        resume = row.get("resume_data") or {}

        forced = self._pick_forced_topic(resume)
        if forced is not None:
            key, record = forced
            topic_description = self._forced_topic_description(key, record, resume)
            history = self._build_conversation_history(row)
            worded = await run_topic_question_chain(
                resume,
                COVERAGE_SCHEMA,
                history,
                topic_description,
                field_completeness=row.get("field_completeness") or {},
            )
            record_field = record.get("field")
            target = {
                "block": record.get("block"),
                "item_id": record.get("item_id"),
                "fields": [record_field] if record_field else None,
            }
            await self._open_round(worded["question"], forced=key, target=target)
            return

        if next_question:
            target = self._sanitize_target(next_question_target, ASKABLE_COVERAGE_SCHEMA)
            await self._open_round(next_question, forced=None, target=target)
            return

        await self._await_task_a_settle()
        row = await self._crud.get_session(self._session_id)
        if row is None:
            return

        gap = find_required_gap(
            row.get("field_completeness") or {},
            COVERAGE_SCHEMA,
            exclude=frozenset(self._forced_topics_spent),
        )
        if gap is None:
            await self._complete_interview()
            return

        topic_description = f"the '{gap['block']}' section -- {gap['complete_when']}"
        history = self._build_conversation_history(row)
        worded = await run_topic_question_chain(
            row.get("resume_data") or {},
            COVERAGE_SCHEMA,
            history,
            topic_description,
            field_completeness=row.get("field_completeness") or {},
        )
        target = {"block": gap["block"], "item_id": None, "fields": None}
        await self._open_round(worded["question"], forced=gap["forced_topic"], target=target)


    async def _finish_answer(self) -> None:
        round_id = self._current_round_id
        answer_text = " ".join(self._buffer).strip()
        if round_id is None or not answer_text:
            return

        # Deliberately STAYS in awaiting_answer until the fused call has
        # actually returned -- this whole cycle runs inside the very task
        # `on_speaking_change(True)` cancels when the candidate resumes
        # speaking, so a turn killed mid-grading must leave no trace. The
        # buffer clears here so anything said DURING grading accumulates
        # cleanly on top of nothing; the CancelledError handler below puts
        # both halves back together.
        self._buffer = []
        graded = False

        row = await self._crud.get_session(self._session_id)
        resume = (row or {}).get("resume_data") or {}
        history = self._build_conversation_history(row or {})

        # Fire-and-forget: every line of this answer already reached the
        # extraction queue live as it was transcribed (see pipeline.py's
        # persist()), so there's nothing to lose by forcing that batch out
        # now instead of waiting for the char-count trigger. Never awaited
        # -- Task A is off this turn's critical path entirely; the only
        # place it's ever awaited is _await_task_a_settle, on the required-
        # gap safety net path.
        await self._orchestrator.flush_transcript(self._session_id, wait=False)

        try:
            # The ONE LLM call for this whole turn: grades this answer AND
            # drafts both possible follow-ups (a probe for if it stays open,
            # a next_question for if it resolves) in the same response.
            # ASKABLE_COVERAGE_SCHEMA (not_applicable blocks removed) so it
            # can never draft a question about personal/summary; field_completeness
            # grounds the probe/next_question in exactly which fields are
            # still open instead of a generic re-ask.
            result = await run_question_chain(
                resume,
                ASKABLE_COVERAGE_SCHEMA,
                history,
                answer_text,
                field_completeness=(row or {}).get("field_completeness") or {},
            )

            # Past this line the turn is ours to commit -- everything below
            # is shielded, and a cancellation can no longer put the answer
            # back for re-grading.
            graded = True
            self._awaiting_answer = False

            if result.get("is_meta_question") and result.get("meta_response"):
                # Deliberately NOT recorded via record_round_answer: a round's
                # exchange is a fixed one-shot {question, answer} slot, unlike
                # the old free-form per-thread message log -- filling it with
                # an off-topic aside would leave no open slot for the REAL
                # answer that follows once the pending question is re-spoken.
                try:
                    await asyncio.shield(
                        self._crud.append_transcript_line(self._session_id, "user_aside", answer_text)
                    )
                    await asyncio.shield(
                        self._crud.append_transcript_line(
                            self._session_id, "assistant_aside", result["meta_response"]
                        )
                    )
                except asyncio.CancelledError:
                    pass
                await self._handle_meta_question(result["meta_response"])
                return

            # Kick off the answer-persistence write now (asyncio.shield starts
            # it running immediately) but don't block on it until AFTER the
            # next question/probe has already been queued for TTS below --
            # this write doesn't affect what gets spoken next, only
            # bookkeeping, so it shouldn't sit on the critical path.
            record_answer_task = asyncio.shield(
                self._crud.record_round_answer(self._session_id, round_id, answer_text)
            )

            grade = result.get("answer_grade")
            llm_usage = result.get("_llm_usage")
            probe_question = result.get("probe_question")
            terminal = grade in TERMINAL_GRADES

            # round_row/budget/spent come from `row` (fetched above, before
            # this turn's answer was recorded) rather than a fresh
            # get_session -- record_round_answer only fills in an existing
            # exchange's `answer` field (crud.py), it never changes the
            # exchange count, and nothing else touches questions.rounds
            # concurrently, so this is already accurate.
            round_row = (
                ((row or {}).get("questions") or {}).get("rounds", {}).get(round_id)
            ) or {}
            budget = int(
                round_row.get("max_questions") or settings.resume_room_max_questions_per_round
            )
            spent = len(round_row.get("exchanges") or [])
            capped = spent >= budget
            if capped and not terminal:
                logger.info(
                    "interview director giving up on round {} after {} of {} questions",
                    round_id, spent, budget,
                )

            if not terminal and not capped and probe_question:
                await self._probe_round(probe_question)
                try:
                    await record_answer_task
                except asyncio.CancelledError:
                    pass
                return

            if not terminal and not capped:
                # No probe at all -- a fail-soft `_empty_result` after a
                # provider/schema error, or a response that ignored the
                # instruction to always draft one. That is a failed grading
                # round, not a finished turn -- put the answer back and
                # re-grade it on the next silence rather than closing the
                # round on nothing. Bounded, so a call that keeps failing
                # can't loop: after `_MAX_REGRADE_ATTEMPTS` we move on with
                # whatever grade the last response did give us.
                self._regrade_attempts += 1
                if self._regrade_attempts <= self._MAX_REGRADE_ATTEMPTS:
                    logger.warning(
                        "grading round {} produced no probe and no terminal verdict "
                        "(attempt {}) -- restoring the turn to re-grade on the next silence",
                        round_id, self._regrade_attempts,
                    )
                    self._awaiting_answer = True
                    self._buffer = [answer_text] + self._buffer
                    try:
                        await record_answer_task
                    except asyncio.CancelledError:
                        pass
                    return
                logger.warning(
                    "grading round {} produced nothing usable {} times -- moving on",
                    round_id, self._regrade_attempts,
                )

            # Same deferral as record_answer_task above: start these writes
            # now, but don't wait on them until after _advance_round below
            # has already queued the next question's TTS frame.
            close_round_task = asyncio.shield(
                self._crud.close_round(
                    self._session_id, round_id, grade=grade, llm_usage=llm_usage,
                )
            )

            field_completeness_task = None
            if grade == ANSWER_GRADE_UNABLE_TO_ANSWER:
                # The one verdict the batched completeness worker can never
                # infer on its own (a decline leaves no resume_data value for
                # it to ever judge) -- commit it now, precisely, using the
                # round's own stored target. SUFFICIENT/PARTIAL stay
                # exclusively Task A's call; this only closes that one gap.
                closing_target = (round_row or {}).get("target")
                if closing_target:
                    patch = build_unable_to_answer_patch(
                        (row or {}).get("field_completeness") or {}, closing_target
                    )
                    if patch:
                        field_completeness_task = asyncio.shield(
                            self._crud.apply_field_completeness(self._session_id, patch)
                        )

            if self._current_round_forced:
                self._forced_topics_spent.add(self._current_round_forced)
            self._current_round_id = None
            self._current_round_forced = None

            await self._advance_round(result.get("next_question"), result.get("next_question_target"))

            try:
                await record_answer_task
                await close_round_task
                if field_completeness_task is not None:
                    await field_completeness_task
            except asyncio.CancelledError:
                pass
        except asyncio.CancelledError:
            if not graded:
                # The candidate resumed speaking while we were grading, so
                # this was never end-of-turn. Put the turn back exactly as
                # we found it -- the answer, plus whatever they added while
                # the call was in flight -- so the next silence re-grades
                # the whole thing through the same fused call.
                self._awaiting_answer = True
                self._buffer = [answer_text] + self._buffer
                logger.info(
                    "answer grading for round {} cancelled mid-flight -- turn restored, "
                    "will re-grade on the next silence",
                    round_id,
                )
            raise


    async def _handle_meta_question(self, response_text: str) -> None:
        """The candidate went off-script to ask about the process rather than
        answer. Speak the answer, then re-speak the exact pending question
        (cached in _current_question_text when it was first asked) and wait
        again -- no grade, no probe spent, no round write at all."""
        self._awaiting_answer = True
        self._buffer = []
        try:
            await self._worker.queue_frames([
                TTSSpeakFrame(text=response_text, append_to_context=False),
                TTSSpeakFrame(text=self._current_question_text, append_to_context=False),
            ])
        except Exception:
            logger.exception("failed to speak meta-question response")


    async def _complete_interview(self) -> None:
        """Nothing is left to ask -- genuinely done. The only remaining
        caller of _leave_interview_mode: a transient LLM failure must never
        reach here, which is why _finish_answer's "no probe, not terminal,
        not capped" branch re-grades the answer instead of dropping out of
        interview mode. Speaks the closing line exactly once, even if
        further silences keep re-triggering an empty round-advance, then
        ends the session.

        Ending is queued as a *separate* frame right behind the closing
        TTSSpeakFrame rather than fired off immediately: pipecat processes
        queued frames through the pipeline in order, so an EndFrame queued
        here only reaches (and tears down) the transport after the closing
        message has actually been synthesized and played, not before. The
        rest of teardown (marking the CRUD session finished, deleting the
        Daily room, running the final resolution pass) is the same
        `_on_bot_done`/`_close_out` path every other session end already
        goes through -- see room-orchestration.md -- there is nothing
        closing-specific to duplicate here.
        """
        self._leave_interview_mode()
        if self._closed:
            return
        self._closed = True
        try:
            await self._worker.queue_frames(
                [TTSSpeakFrame(text=CLOSING_MESSAGE, append_to_context=False)]
            )
        except Exception:
            logger.exception("failed to speak closing message")

        if self._on_complete is not None:
            try:
                await self._on_complete()
            except Exception:
                logger.exception("failed to end session after closing message")


    def _leave_interview_mode(self) -> None:
        self._awaiting_answer = False
        self._current_round_id = None
        self._current_round_forced = None
        self._current_question_text = None
        self._buffer = []
