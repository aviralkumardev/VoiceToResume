# Backend: Voice Bot Pipeline (STT/TTS)

## Purpose
Runs the actual live voice bot inside a Daily room: speech-to-text →
`InterviewDirector` (round-based Q&A, no chat LLM in the loop) → text-to-
speech, plus bridging every user-facing event (live captions, speaking
indicator, agent-ready signal) to the frontend as Daily app messages. Also
the source of the candidate's raw VAD speaking state, forwarded to the
orchestrator for the silence-triggered completeness pipeline. Built on
pipecat's `Pipeline`/`PipelineWorker`.

**There is no persona/chat LLM anywhere in this pipeline.** Every word the
bot ever says is either the fixed opening/closing line or a question worded
by one of the LLM chains in
[backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md) —
`question_chain.run_question_chain`/`run_topic_question_chain` — all
delivered as literal TTS text (`TTSSpeakFrame`), never generated live by a
chat model reacting to the candidate.

## Key files
- `backend/app/meeting_room/stt_tts_pipeline/pipeline.py` — `run_bot()`, the
  pipeline assembly and the fixed `GREETING_MESSAGE`.
- `backend/app/meeting_room/stt_tts_pipeline/bot_session.py` — `BotSession`
  (bot identity, greeting kickoff, session end).
- `backend/app/meeting_room/stt_tts_pipeline/participant_tracker.py` —
  `ParticipantTracker` (who's in the room, mic capture, empty-room teardown).
- `backend/app/meeting_room/stt_tts_pipeline/stt.py`, `tts.py` — one
  provider-selection module per role.
- `backend/app/meeting_room/stt_tts_pipeline/processors/bridges.py` —
  `SpeakingBridge`, `UserTranscriptBridge`, `AgentTranscriptBridge` (pipecat
  `FrameProcessor`s that emit app messages to the frontend).
- `backend/app/meeting_room/stt_tts_pipeline/interview_director.py` —
  `InterviewDirector`, the sole conversational driver for the whole session
  (see below). Rewritten top-to-bottom (~1500 → ~460 lines) to a
  round-based design; see "InterviewDirector" below.
- `backend/app/meeting_room/stt_tts_pipeline/__init__.py` —
  `select_provider()` helper shared by `stt.py`/`tts.py`.

## Public surface
- `run_bot(room_url, token, *, room_name, session_id, orchestrator) -> None`
  (async) — the entire bot lifetime: builds the Daily transport, STT/TTS
  services, the caption/speaking bridges, the `InterviewDirector`, registers
  Daily event handlers, and runs a `WorkerRunner` until the pipeline ends
  (timeout is enforced by the *caller*, `room_orchestrator._run_guarded_bot`,
  not here). Cancels the director's pending task in a `finally` on the way
  out.
- `build_stt()`, `build_tts()` — each reads its own
  `settings.resume_room_*_provider` and dispatches through `select_provider`
  to a builder function. Currently: STT/TTS only register `"sarvam"`.
- `select_provider(chosen, builders) -> T` — generic provider dispatch;
  raises `ValueError` listing valid keys on an unknown provider name.
- `BotSession.greet()` — sends `{"type": "agent-ready", ...}`, then (if a
  director exists) calls `director.ask_opening_question(self._greeting)` —
  the fixed `GREETING_MESSAGE` spoken straight through TTS. Only fires once
  (`self.greeted` guard). `BotSession.director` is set by `run_bot` right
  after constructing the `InterviewDirector`.
- `BotSession.end_session()` — queues a single `EndFrame` on the worker for
  graceful pipeline shutdown. `run_bot` passes it as
  `InterviewDirector(..., on_complete=bot.end_session)`, so
  `_complete_interview` calls it right after queuing the closing
  `TTSSpeakFrame` (see below) — this is what actually ends the Daily room
  once the interview genuinely finishes.
- `ParticipantTracker` — tracks human participant ids, captures mic audio
  per participant, and schedules pipeline teardown
  (`resume_room_empty_room_grace_seconds` after the room goes empty).
- Bridge classes emit the exact `AppMessage` wire shapes documented in
  [frontend/state-management.md](../frontend/state-management.md) —
  `transcript` (from `UserTranscriptBridge`/`AgentTranscriptBridge`),
  `speaking` (from `SpeakingBridge`), `agent-ready` (from `BotSession.greet`).
- `UserTranscriptBridge` also takes an `on_speaking_change: Optional[Callable[[bool], None]]`
  constructor param, called `True` on `UserStartedSpeakingFrame` / `False`
  on `UserStoppedSpeakingFrame` — a same-process callback (no app message
  emitted for it), separate from the `transcript`/turn-numbering logic.

## `InterviewDirector` — round-based Q&A state machine
Constructed in `run_bot` right after the `PipelineWorker` exists (it needs
`worker` to speak). `on_complete` is wired to `bot.end_session`. Drives the
entire conversation — there is nothing else that speaks. State is
per-round rather than per-target: `_current_round_id`/
`_current_round_forced`/`_current_question_text` describe whichever round
is currently open (if any); `_awaiting_answer` gates whether
`record_candidate_text` is buffering an in-progress answer.

**A round is one subject.** It holds one or more `{question, answer}`
exchanges (the opening question plus any probes) under a per-round question
budget (`resume_room_max_questions_per_round`, default 2 — counts the
opening question too). CRUD's `questions.rounds[round_id]` shape and the
four methods that manage it (`start_round`/`append_round_question`/
`record_round_answer`/`close_round`) are documented in
[backend/database-models.md](database-models.md).

- **`ask_opening_question(question)`** — called once, from `BotSession.greet()`
  the instant a participant joins: `await self._open_round(question,
  forced=None, target=None)`. The opening answer is graded exactly like any
  other round by Task B — no special-cased never-reprobe behavior any more;
  if the candidate's opening answer is thin, Task B may naturally probe it
  just like any other round. `target=None` since it's a broad multi-block
  opener, not about one coverage gap.
- **`_open_round(question, *, forced, target=None)`** — speaks the question
  (`TTSSpeakFrame(text=..., append_to_context=False)` via
  `worker.queue_frames`), resets per-round state (`_buffer=[]`,
  `_awaiting_answer=True`, `_current_round_forced=forced`,
  `_regrade_attempts=0`), then `crud.start_round(..., target=target)`
  (shielded) sets `_current_round_id`. `target` is `{"block", "item_id",
  "fields"}` (`fields` an optional list of field names) describing what the
  question is about — already built/sanitized
  by the caller (`_advance_round`, below) — stored verbatim on the round so
  a later `UNABLE_TO_ANSWER` grade can be committed back into
  `field_completeness` precisely (see
  [backend/completeness-pipeline.md](completeness-pipeline.md)'s
  `build_unable_to_answer_patch`).
- **`_probe_round(question)`** — same speak/state-reset, but calls
  `crud.append_round_question` on the *same* `_current_round_id` instead of
  opening a new one — the round stays open, its exchange count grows.
- **`record_candidate_text(text)`** — unchanged: buffers finalized STT
  chunks into `self._buffer` while `_awaiting_answer`, purely additive; see
  "Data flow & dependencies" below for why every line also always reaches
  the transcript/extraction queue live, independent of this buffering.
- **`on_speaking_change(is_speaking)` / `cancel()`** — unchanged cancel-on-
  resume debounce logic: `on_speaking_change(True)` cancels `self._pending`
  (the task running `_finish_answer`), `_run_after_silence()` schedules the
  eventual grading/advance call after the configured silence window.
  `_run_after_silence` branches to `_finish_answer()` if there's an actual
  answer to grade (`_awaiting_answer` or a non-empty `_buffer` on an open
  round), else to `_advance_round()` with no `next_question` of its own —
  the idle/recovery path.

**`_finish_answer()`** — once the answer-silence window elapses:
1. Builds `resume`/`history` from a fresh `get_session` read
   (`_build_conversation_history` flattens every round's exchanges, in
   `round_order` order, into `[{"question", "answer"}, ...]` — the *whole*
   session, no windowing/truncation).
2. Fires `orchestrator.flush_transcript(session_id, wait=False)` —
   fire-and-forget, never awaited here. This is Task A: the unchanged
   extraction+coverage pipeline, running purely in the background. Task B
   below is the turn's only critical-path work.
3. Awaits `question_chain.run_question_chain(resume, ASKABLE_COVERAGE_SCHEMA,
   history, answer_text, field_completeness=row.get("field_completeness") or
   {})` — the ONE LLM call for the whole turn. `ASKABLE_COVERAGE_SCHEMA`
   (`coverage_schema.py`'s `COVERAGE_SCHEMA` with every `not_applicable`
   block removed) is used here instead of the raw schema so the fused call
   can never draft a question about `personal`/`summary` — that information
   is captured elsewhere in the product, never through this interview.
   `field_completeness` (the batched worker's last per-field/per-item
   verdicts, possibly a turn or more stale) grounds `probe_question`/
   `next_question` in exactly which fields are still open, so a probe
   targets precisely what's missing on the current item instead of padding
   in a generic re-ask for something already given. Whenever `next_question`
   is non-null, the same response self-reports `next_question_target`
   (block/item/field metadata about that question — see
   [backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md)),
   carried forward to step 4's `_advance_round` call below. If
   `is_meta_question` and `meta_response`: records the aside under
   `user_aside`/`assistant_aside` transcript roles (invisible to extraction,
   which only reads `role == "user"`) and diverts to
   `_handle_meta_question` — **without** ever calling
   `crud.record_round_answer`, since a round's exchange is a fixed one-shot
   `{question, answer}` slot (unlike the old free-form per-target message
   log) and filling it with the off-topic aside would leave no open slot
   for the real answer that follows once the pending question is re-spoken.
4. Otherwise records the answer (`crud.record_round_answer`, shielded),
   computes `capped` from the round's exchange count vs. its stamped
   `max_questions`, and branches on `answer_grade`:
   - **non-terminal (`PARTIAL`), under cap, `probe_question` present** →
     `_probe_round(probe_question)` — same round, same subject.
   - **non-terminal, under cap, no probe at all** (a fail-soft empty result,
     or a response that ignored the always-draft instruction) → restore the
     answer to `self._buffer` with `_awaiting_answer` re-armed, so the next
     silence re-grades the whole thing through the same call, bounded by
     `InterviewDirector._MAX_REGRADE_ATTEMPTS` (1).
   - **terminal, or capped** → `crud.close_round(..., grade=...)`; if the
     grade is `UNABLE_TO_ANSWER`, reads the closing round's own stored
     `target` and, if set, builds a patch via
     `build_unable_to_answer_patch(field_completeness, target)` and, if
     non-empty, commits it with a shielded
     `crud.apply_field_completeness` — the one write path around the
     batched grader's structural inability to ever detect a verbal decline
     on its own (see
     [backend/completeness-pipeline.md](completeness-pipeline.md)). Only for
     `UNABLE_TO_ANSWER` — `SUFFICIENT`/`PARTIAL` stay exclusively Task A's
     call. If the round was forced, its key joins `_forced_topics_spent`;
     hands off to `_advance_round(result.get("next_question"),
     result.get("next_question_target"))`.
5. `except asyncio.CancelledError` (candidate resumed speaking mid-grading):
   if the answer wasn't graded yet, restores `_awaiting_answer` and
   prepends the original `answer_text` back onto `self._buffer` (ahead of
   whatever accumulated during the cancelled call), then re-raises. See
   "Cancel-safe turns" below.

**`_advance_round(next_question=None, next_question_target=None)`** — the
shared round-open decision, called from `_finish_answer`'s terminal/capped
path (with Task B's own `next_question`/`next_question_target`) and from
`_run_after_silence`'s idle branch (with both `None`):
1. **Forced topic check, always first.** `_pick_forced_topic(resume)` scans
   `resume["conflicts"]` then `resume["unresolved"]` for a record whose key
   (`"conflict:<id>"`/`"unresolved:<id>"`) isn't already in
   `_forced_topics_spent`. If found: builds a natural-language
   `topic_description` from the record via `_forced_topic_description(key,
   record, resume)` (conflict — "resolving a conflict: earlier the
   candidate's `<field>`[ for their `<item label>`] was recorded as
   `<existing>`, then as `<alt>` — need to know which is correct"; unresolved
   — "clarifying an ambiguous earlier statement: `"<text>"` — need to know
   which part of the resume this belongs to"). The `<item label>` clause
   uses `_item_label(resume, block, item_id)` (below) to name the specific
   experience/education/etc. item a conflict concerns, when the record
   carries one — e.g. "their Generative AI Intern at AI Solve" rather than
   leaving it ambiguous which of several entries is meant. Words the topic
   via `question_chain.run_topic_question_chain` (passing
   `field_completeness=row.get("field_completeness") or {}` alongside the
   full, unfiltered `COVERAGE_SCHEMA` — the topic here is already
   pre-decided by Python, so there's no block-selection risk needing the
   askable filter), builds `target = {"block": record.get("block"),
   "item_id": record.get("item_id"), "fields": [record["field"]] if
   record.get("field") else None}` (a conflict record's single `field` is
   wrapped in a one-element list to match the target shape everywhere else;
   an unresolved record only ever carries `block`, so `item_id`/`fields`
   come back `None` — still meaningfully better than nothing), and opens a
   forced round via `_open_round(worded["question"], forced=key,
   target=target)`.
   Whatever `next_question`/`next_question_target` Task B organically
   drafted this turn is discarded outright — a conflict or unresolved fact
   outranks anything the LLM proposed on its own.
2. **Else, if `next_question` is truthy**, sanitizes Task B's own
   `next_question_target` via `_sanitize_target(next_question_target,
   ASKABLE_COVERAGE_SCHEMA)` — `block` must be a string actually present in
   `ASKABLE_COVERAGE_SCHEMA`, else the whole target is dropped to `None`;
   `item_id` is coerced to `None` unless already a string; `fields` is
   filtered down to only the entries that are real field keys of that block
   (per `coverage[block]["fields"]`), collapsing to `None` if nothing valid
   survives — then opens it directly (`_open_round(next_question,
   forced=None, target=target)`). This is the common case: Task B's own
   free choice of what to ask next, trusted as-is (only the *target
   metadata* is validated, never the question text itself or whether it
   should be asked).
3. **Else, the required-coverage safety net.** `_await_task_a_settle()`
   awaits `orchestrator.flush_transcript(wait=True)` then a
   timeout-bounded `run_completeness_grading_cycle` (swallowing
   `asyncio.TimeoutError`) — the *only* place Task A is ever awaited, and
   the only place the director ever reads `field_completeness`. Re-fetches
   the row and calls `required_gap.find_required_gap(field_completeness,
   COVERAGE_SCHEMA, exclude=frozenset(self._forced_topics_spent))` (which
   internally filters `COVERAGE_SCHEMA` through `askable_coverage_schema`
   before applying the `required`-importance check — see
   [backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md)). If a
   gap comes back: builds `topic_description = f"the '{block}' section --
   {complete_when}"`, words it via the same `run_topic_question_chain`
   (again passing `field_completeness=row.get("field_completeness") or
   {}`), builds `target = {"block": gap["block"], "item_id": None,
   "fields": None}`, and opens a forced round (`forced=gap["forced_topic"]`, i.e.
   `"gap:<block>"`, `target=target`). If `None`: `_complete_interview()` —
   the only site that ends the session.

**Doubt/meta-question handling** — the candidate can go off-script mid-
answer to ask about the process itself ("how long is this?") instead of
answering, detected as one extra field on the *same* per-answer grading
call (no added LLM round-trip). `_handle_meta_question(response_text)`
speaks it, then **re-speaks the exact pending question** (cached in
`self._current_question_text`, set every time `_open_round`/`_probe_round`
runs), re-arms `_awaiting_answer`, and leaves `_buffer` empty — no probe
spent, no round state otherwise touched.

**Session bootstrap and closing** — with no persona to fall back on, the
director owns both ends of the session:
- `ask_opening_question` — see above.
- `_complete_interview()` — speaks the fixed `CLOSING_MESSAGE` exactly once
  (guarded by `self._closed`), then calls `self._on_complete()`
  (`bot.end_session`), which queues an `EndFrame` right behind the closing
  `TTSSpeakFrame` — pipecat processes queued frames in order, so the
  `EndFrame` only tears down the transport once the closing message has
  actually been synthesized and pushed to output. `_leave_interview_mode()`
  clears `_awaiting_answer`/`_current_round_id`/`_current_round_forced`/
  `_current_question_text`/`_buffer`.

**Cancel-safe turns.** `on_speaking_change(True)` cancels `self._pending` —
the very task running `_finish_answer` — so a candidate who resumes
speaking mid-grading cancels their own turn. `_finish_answer` therefore
holds `_awaiting_answer` set until `run_question_chain` has actually
returned, and its `except asyncio.CancelledError` restores the turn
(`_awaiting_answer` back on, answer text prepended to whatever accumulated
during the call) so the next silence re-grades the whole thing through the
same single call. There is no separate `flush_task` to cancel/track in this
path any more (unlike the old design): `flush_transcript(..., wait=False)`
just enqueues a `FlushRequest` and returns almost immediately, so it's
awaited directly rather than wrapped in its own tracked task.

**TTS is never interrupted by user speech.** `pipeline.py`'s
`LLMContextAggregatorPair` construction now passes an explicit
`user_turn_strategies`:
```python
user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
    context_llm,
    user_params=LLMUserAggregatorParams(
        vad_analyzer=SileroVADAnalyzer(),
        user_turn_strategies=UserTurnStrategies(
            start=[
                VADUserTurnStartStrategy(enable_interruptions=False),
                TranscriptionUserTurnStartStrategy(),
            ],
        ),
    ),
)
```
Verified directly against the installed `pipecat` source
(`pipecat.turns.user_start`, `pipecat.turns.user_turn_strategies`):
`enable_interruptions=False` only suppresses `broadcast_interruption()` (the
call that sends the `InterruptionFrame` cancelling in-flight TTS synthesis)
— it does **not** touch `enable_user_speaking_frames` (default `True`,
unaffected), which is the separate flag that still emits
`UserStartedSpeakingFrame`/`UserStoppedSpeakingFrame`, the frames every
silence/turn-detection timer in this file depends on. `vad_analyzer=
SileroVADAnalyzer()` builds an independent `VADController` regardless of the
explicit `user_turn_strategies` list, so speaking-state detection is
unaffected end-to-end — only the "cut off the bot's own TTS" behavior is
suppressed.

**Observable side effect worth knowing**: `AgentTranscriptBridge._clear_drip`
in `bridges.py` still reacts to `InterruptionFrame` to cut the caption drip
short. With interruption broadcast suppressed, captions keep dripping for
the full bot utterance even while the candidate talks over it — consistent
with "never cut off the bot," but visually the caption may keep animating
after the user starts speaking.

Three things the director owns beyond the round machinery above:
- **`_item_label(resume, block, item_id) -> Optional[str]`** (staticmethod)
  — a short human-readable label for one specific item of a repeatable
  block (`experience` → `"{role} at {company}"`, `education` →
  `"{degree} at {college}"`, `projects`/`certifications`/`courses` → the
  item's own `name`), `None` if `block`/`item_id` don't resolve to an actual
  existing item. Folded into `_forced_topic_description`'s conflict wording
  so a forced question about a specific job/degree/project names it rather
  than leaving it ambiguous which of several entries is meant — the same
  problem `next_question_target`'s `item_id` solves for organic questions,
  solved here on the deterministic forced-topic path.
- **`_sanitize_target(target, coverage) -> Optional[dict]`** (staticmethod)
  — the trust boundary for Task B's self-reported `next_question_target`:
  drops the whole target to `None` unless `block` is a string present in
  `coverage`; coerces `item_id` to `None` unless already a string; filters
  `fields` down to only the entries that are real field keys of that block
  (`coverage[block]["fields"]`'s own keys), dropping any hallucinated name,
  and collapses to `None` if nothing valid survives. Never validates the
  *question text* itself or whether it should be asked — Task B is still
  trusted fully on that; this only protects `field_completeness` from ever
  being patched under a bogus/hallucinated block or field key.
- **`_forced_topics_spent: Set[str]`** — see
  [backend/completeness-pipeline.md](completeness-pipeline.md)'s "The
  interview loop". New, director-only, in-memory anti-infinite-loop state;
  replaces the old session-lifetime `_abandoned`/`abandoned_paths`/
  `mark_target_abandoned` machinery entirely — there is no equivalent
  cross-round bookkeeping for organic (non-forced) topics any more, since
  Task B is trusted to not loop on its own.
- Questions are spoken by queueing a `TTSSpeakFrame(text=...,
  append_to_context=False)` on the worker — same injection point used for
  the opening question, doubt responses, probes, and the closing line.
  `append_to_context=False` is deliberate: there's no chat context
  downstream to pollute.
- `on_speaking_change(is_speaking)` drives the whole state machine with the
  same cancel-on-resume debounce the batched completeness worker uses —
  only the wait duration and the action differ. `cancel()` kills any
  pending task at teardown.

## Data flow & dependencies
- Reads config from `app.core.config.settings`
  ([backend/app-config.md](app-config.md)) — provider choices, grace/idle
  timeouts, `resume_room_max_questions_per_round`.
- `UserTranscriptBridge`'s `on_final_transcription` callback is wired in
  `pipeline.py`'s `persist()` closure. For `role == "user"`, `persist()`
  always offers the line to `director.record_candidate_text(text)` (a
  buffering side effect only) *and* always does `crud.append_transcript_line`
  + (for `user`) `orchestrator.enqueue_transcript` — the handoff into
  [backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md) — the
  same path used outside interview mode, unconditionally, mid-answer
  included, so extraction can fire on character count alone however long
  the candidate keeps talking, not just once they go silent.
  `persist("assistant", ...)` is unaffected, so a directly-spoken interview
  question still lands in the transcript as an assistant line — keeping
  question-then-answer ordering intact for extraction.
- `UserTranscriptBridge`'s `on_speaking_change` callback fans out to *two*
  consumers: `orchestrator.enqueue_speaking_state` (the batched grading
  worker, cross-process via a queue) and `director.on_speaking_change` (a
  direct in-process call, since the director lives inside `run_bot` and
  holds `worker` directly).
- **VAD is kept even though the chat LLM is gone.** `pipeline.py` still
  builds `context_llm = LLMContext()` and the `LLMContextAggregatorPair`
  above, keeping both aggregators in the `Pipeline([...])` list. This is
  pipecat's *only* configured source of `UserStartedSpeakingFrame`/
  `UserStoppedSpeakingFrame` — remove this pair and *all* silence/turn
  detection breaks, not just chat. Pipecat's standalone `VADProcessor` is
  not a drop-in alternative: it emits a different frame type
  (`VADUserStartedSpeakingFrame`), which `UserTranscriptBridge`'s
  `isinstance` checks would not match.
- Depends on external services: Sarvam STT/TTS and Daily transport/VAD
  (Silero) — all via pipecat service classes, not called directly.
- Only ever invoked from `room_orchestrator._run_guarded_bot` (lazy import,
  see [backend/room-orchestration.md](room-orchestration.md)).

## Conventions & gotchas
- `AGENT_CAPTION_WORDS_PER_SECOND` (2.5) in `bridges.py` is a deliberate
  approximation: Sarvam's TTS has no word-boundary timing events, so agent
  captions are dripped out word-by-word at a fixed pace to *look* like
  they're keeping pace with audio, rather than jumping to the full sentence
  instantly. Don't remove the drip queue without understanding this — see
  the long comment at the top of `bridges.py`.
- `AgentTranscriptBridge`'s drip queue must be cleared on
  `InterruptionFrame` (`_clear_drip`) or a stale sentence keeps dripping
  words after the bot has actually been interrupted — see the TTS-
  interruption-suppression note above for why this frame is now rarer.
- `GREETING_MESSAGE` (`pipeline.py`) and `CLOSING_MESSAGE`
  (`interview_director.py`) are literal spoken text, not LLM instructions —
  changing tone here changes the product's core interaction style directly,
  with no LLM paraphrasing in between.
- New STT/TTS providers are added by writing a new builder function and
  adding it to that module's `BUILDERS`/`builders` dict, then pointing the
  matching `settings.resume_room_*_provider` at its key — never by adding
  conditional branches elsewhere.
- `ParticipantTracker.schedule_teardown` cancels the pipeline worker
  (`worker.cancel()`) after `resume_room_empty_room_grace_seconds` of an
  empty room — a pending teardown task is cancelled again if a participant
  rejoins in that window.

## Last synced
2026-09-05 (later still — round 3 (part 1): a live run showed one
experience item getting four separate rounds, one per remaining open field
(`location`, then `projects`, then `achievements`, then `awards`), because
`target`'s `field` slot could only ever name one. Renamed `field` → `fields`
(`Optional[List[str]]`) everywhere a round `target` is built or read:
`_sanitize_target` now filters `fields` against the block's real field
keys instead of coercing a single string; the forced conflict/unresolved
branch wraps the record's single `field` into a one-element list; the
required-gap branch's `target` is unchanged apart from the rename.
`build_unable_to_answer_patch` (see
[backend/completeness-pipeline.md](completeness-pipeline.md)) now commits
every field in the list on a decline, not just one. Paired with a new
"consolidate, don't drip-feed" prompt rule (see
[backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md)) so
Task B folds every currently-open field of a targeted item/block into ONE
question instead of asking them one at a time across separate rounds.
Priority/ordering across different blocks remains a known, separate,
explicitly-deferred follow-up.)
2026-09-05 (later same day — round 2 of live-session bug fixes, target
bookkeeping: `_open_round`/`crud.start_round` gained a `target` param
(`{"block", "item_id", "field"}`, stored on the round) built at all three
`_advance_round` round-open sites — from the conflict/unresolved record
directly, from Task B's own sanitized `next_question_target`
(`_sanitize_target`, new), or `{"block": gap_block}` for a required-gap
round. `_finish_answer` now reads the closing round's `target` and, on an
`UNABLE_TO_ANSWER` grade, commits `build_unable_to_answer_patch`'s result
into `field_completeness` — closing the one gap the batched grader can
never fill on its own (a verbal decline produces no `resume_data` value to
ever judge). Added `_item_label` to name a specific experience/education/
etc. item in forced-topic wording, fixing an ambiguity that surfaces once a
candidate has more than one entry in a repeatable block. See
[backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md) (prompt
rules + `next_question_target`) and
[backend/completeness-pipeline.md](completeness-pipeline.md)
(`build_unable_to_answer_patch`) and
[backend/database-models.md](database-models.md) (`target` on the round
row).)
2026-09-05 (bug fixes from live-session testing: the fused call's `coverage`
argument at the `_finish_answer` call site switched from `COVERAGE_SCHEMA`
to `ASKABLE_COVERAGE_SCHEMA` — a live session had the bot ask for the
candidate's full name/email/phone, a `not_applicable` block it should never
surface as a question. Also threaded `field_completeness` into all three
`run_question_chain`/`run_topic_question_chain` call sites — another live
session showed a probe padding in an irrelevant re-ask for responsibilities
the candidate had already given, because the chain had no ground truth for
exactly which fields of the current item were still open. See
[backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md) for the
chain-level detail.)
2026-09-05 (`interview_director.py` rewritten top-to-bottom, ~1500 → ~460
lines: replaced per-target selection/shortlist/field-group-batching/pending-
BLOCK-claim verification with a round-based state machine
(`_open_round`/`_probe_round`/`_advance_round`/`_pick_forced_topic`/
`_await_task_a_settle`/`_forced_topics_spent`) trusting one fused per-answer
LLM call (`question_chain.run_question_chain`) directly, with two small
deterministic guardrails (forced conflict/unresolved priority, a
required-coverage safety net before ending). Also fixed `pipeline.py` so the
bot's own TTS is never interrupted by resumed user speech
(`VADUserTurnStartStrategy(enable_interruptions=False)`), while leaving
speaking-state detection (VAD/silence timers) fully intact. Settings
renamed `resume_room_max_probes_per_target` →
`resume_room_max_questions_per_round`; `resume_room_dedup_candidate_targets`/
`resume_room_next_target_shortlist_size`/
`resume_room_next_question_transcript_lines` deleted (fed only the removed
shortlist machinery). See
[backend/completeness-pipeline.md](completeness-pipeline.md) and
[backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md). Older
history predating this rewrite described the deleted selection/shortlist/
claim machinery in detail and has been removed from this file.)
