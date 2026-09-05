# Backend: Voice Bot Pipeline (STT/TTS)

## Purpose
Runs the actual live voice bot inside a Daily room: speech-to-text →
`InterviewDirector` (round-based Q&A, no chat LLM in the loop) → text-to-
speech, plus bridging every user-facing event (live captions, speaking
indicator, agent-ready signal) to the frontend as Daily app messages. Built
on pipecat's `Pipeline`/`PipelineWorker`.

**There is no persona/chat LLM anywhere in this pipeline.** Every word the
bot ever says is either the fixed opening/closing line or a question worded
by one of the LLM chains in
[backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md) — the
combined analysis call words every queued question ahead of time,
`question_chain.run_answer_grading_chain` words same-round probes — all
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
  (see below).
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
  `TTSSpeakFrame` — this is what actually ends the Daily room once the
  interview genuinely finishes.
- `ParticipantTracker` — tracks human participant ids, captures mic audio
  per participant, and schedules pipeline teardown
  (`resume_room_empty_room_grace_seconds` after the room goes empty).
- Bridge classes emit the exact `AppMessage` wire shapes documented in
  [frontend/state-management.md](../frontend/state-management.md) —
  `transcript` (from `UserTranscriptBridge`/`AgentTranscriptBridge`),
  `speaking` (from `SpeakingBridge`), `agent-ready` (from `BotSession.greet`).
- `UserTranscriptBridge` also takes an `on_speaking_change: Optional[Callable[[bool], None]]`
  constructor param, called `True` on `UserStartedSpeakingFrame` / `False`
  on `UserStoppedSpeakingFrame` — a same-process callback wired straight to
  `director.on_speaking_change` in `pipeline.py`. There is no second
  consumer of this signal any more (no batched-worker speaking queue — see
  [backend/completeness-pipeline.md](completeness-pipeline.md)).

## `InterviewDirector` — round-based Q&A, queue-popping state machine
Constructed in `run_bot` right after the `PipelineWorker` exists (it needs
`worker` to speak). `on_complete` is wired to `bot.end_session`. Drives the
entire conversation — there is nothing else that speaks. State is
per-round: `_current_round_id`/`_current_round_forced`/
`_current_question_text` describe whichever round is currently open (if
any); `_awaiting_answer` gates whether `record_candidate_text` is buffering
an in-progress answer.

**Target *selection* is entirely the analysis worker's job now, not this
class's.** `questions.queue` is regenerated wholesale every combined-call
cycle (conflicts/unresolved first, then ordinary coverage gaps, in
Python-authoritative priority order — see
[backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md)'s
`next_target.compute_candidate_queue`), already fully worded. This class
only ever pops the head of that queue and speaks it — **no LLM call at pop
time**, and no in-memory bookkeeping of what's been given up on or forced
(that now lives on the session row, since the analysis worker's task needs
to see it too — `crud.mark_target_given_up`/`mark_forced_topic_spent`).

**A round is one subject.** It holds one or more `{question, answer}`
exchanges (the opening question plus any probes) under a per-round question
budget (`resume_room_max_questions_per_round`, default 2). CRUD's
`questions.rounds[round_id]` shape and the methods that manage it
(`start_round`/`append_round_question`/`record_round_answer`/`close_round`/
`apply_question_queue`/`pop_question_queue_head`/`mark_target_given_up`/
`mark_forced_topic_spent`) are documented in
[backend/database-models.md](database-models.md).

**The opening round is special.** It has no single `target`/`complete_when`
bar (a multi-block opener), so `_finish_answer` skips grading entirely for
it and goes straight to flush + advance.

Every other round's answer is graded by ONE narrow LLM call
(`question_chain.run_answer_grading_chain`) against ONLY that round's own
`target_complete_when` bar and the conversation history — it never sees the
whole resume/coverage rubric or a candidate list, since deciding what to
ask about NEXT is not its job at all.

- **`ask_opening_question(question)`** — called once, from `BotSession.greet()`
  the instant a participant joins: `await self._open_round(question,
  forced=None, target=None)`. The opening answer is graded exactly like any
  other round — no special-cased never-reprobe behavior.
- **`record_candidate_text(text)`** — buffers finalized STT chunks into
  `self._buffer` while `_awaiting_answer`, purely additive — `pipeline.py`'s
  `persist()` also always sends this same text straight to the transcript/
  analysis queue live, independent of this buffering.
- **`on_speaking_change(is_speaking)` / `cancel()`** — `is_speaking=True`
  cancels `self._pending` (the task running `_finish_answer`);
  `is_speaking=False` schedules `_run_after_silence()` after the configured
  silence window (`resume_room_answer_silence_seconds` if awaiting an
  answer, else `resume_room_silence_hardbound_seconds`). `_run_after_silence`
  branches to `_finish_answer()` if there's an actual answer to grade
  (`_awaiting_answer` or a non-empty `_buffer` on an open round), else to
  `_advance_from_queue()` — the idle/recovery path.
- **`_open_round(question, *, forced, target=None)`** — speaks the question
  (`TTSSpeakFrame`, `append_to_context=False`), resets per-round state, then
  `crud.start_round(..., forced_topic=forced, target=target)` (shielded)
  sets `_current_round_id`. `target` is `{"block", "item_id", "fields"}`,
  stored verbatim so a later `UNABLE_TO_ANSWER` grade can be committed back
  into `field_completeness` precisely.
- **`_probe_round(question)`** — same speak/state-reset, but calls
  `crud.append_round_question` on the *same* `_current_round_id` instead of
  opening a new one.
- **`_pop_next_queue_item(exclude_key)`** — pops `questions.queue` in a loop,
  silently discarding any entry whose `key` matches `exclude_key` before
  returning the first one that doesn't. Exists because the queue is
  regenerated asynchronously by the analysis worker: a snapshot popped
  immediately after a round closes (see `_advance_from_queue`'s
  fire-and-forget flush, below) can still list that exact round's own gap/
  forced-topic key if the regeneration cycle that would have dropped it
  hasn't landed yet. Discarding it is safe — the real state (the round's
  answer, its terminal grade or given-up status) is already committed, so
  the next genuine regeneration cycle won't reinsert it; the popped item was
  simply stale.
- **`_advance_from_queue(pending_flush=None, *, exclude_key=None)`** — the
  sole replacement for what used to be a parameterized `_advance_round`:
  ```python
  async def _advance_from_queue(
      self, pending_flush: Optional[FlushRequest] = None, *, exclude_key: Optional[str] = None,
  ) -> None:
      item = await self._pop_next_queue_item(exclude_key)
      if item is None and pending_flush is not None:
          await asyncio.wait_for(pending_flush.done.wait(), timeout=settings.resume_room_flush_timeout_seconds)
          item = await self._pop_next_queue_item(exclude_key)
      if item is None:
          await self._complete_interview()
          return
      key = item.get("key") or ""
      forced = key if key.startswith(("conflict:", "unresolved:")) else None
      target = {"block": item.get("block"), "item_id": item.get("item_id"), "fields": item.get("fields")}
      await self._open_round(item["question"], forced=forced, target=target)
  ```
  No LLM call — the combined analysis call already worded `item["question"]`.
  `pending_flush` is the `FlushRequest` handle from this same turn's
  fire-and-forget flush (see below) — the first pop always reads whatever
  `questions.queue` already holds, off the critical path entirely; only an
  empty first pop falls back to awaiting that in-flight batch and popping
  again, so the interview-ending decision never fires on a merely-stale
  (not-yet-updated) queue. `exclude_key` is the just-closed round's own
  gap/forced-topic key (see `_finish_answer`'s ordinary-round path, below) —
  filtering it out is what stops a stale-but-non-empty queue snapshot from
  immediately re-asking the exact target this round just closed. Once both
  the exclusion filter and the pending-flush retry are exhausted, the
  interview genuinely ends — the queue is exhaustive by construction.

**`_finish_answer()`** — once the answer-silence window elapses, branches on
whether the round has a real `target` (read from the round's own stored
row, `round_row.get("target")`):

- **Opening round (`current_target is None`)** — no grading call at all: a
  multi-block opener has no single `complete_when` bar. Marks answered,
  closes the round (`grade=None` — the round shape's `grade` field is
  already `Optional[str]`), clears round state, fires
  `orchestrator.flush_transcript(..., wait=False)` (fire-and-forget, keeping
  the LLM round trip off this turn's critical path) and passes the returned
  `FlushRequest` straight into `_advance_from_queue(pending_flush)`, which
  only awaits it if the immediate pop comes back empty (see
  `_advance_from_queue` above).
- **Every other round**:
  1. Builds `history` (`_build_conversation_history` flattens every round's
     exchanges, oldest first, into `[{"question", "answer"}, ...]`) and
     `target_complete_when = complete_when_for_target(ASKABLE_COVERAGE_SCHEMA,
     current_target)`.
  2. Fires `orchestrator.flush_transcript(session_id, wait=False)` —
     fire-and-forget, off this turn's critical path; the combined analysis
     call runs entirely in the background.
  3. Awaits `run_answer_grading_chain(history, answer_text,
     target_complete_when)` — the ONE LLM call for this turn.
  4. Meta-question branch (`is_meta_question`/`meta_response`): records the
     aside under `user_aside`/`assistant_aside` transcript roles (invisible
     to extraction) and diverts to `_handle_meta_question` — **without**
     ever calling `crud.record_round_answer`, since a round's exchange is a
     fixed one-shot slot.
  5. Otherwise starts `crud.record_round_answer` as a shielded, deferred
     task, computes `grade`/`terminal`/`capped` from the round's own
     exchange count vs. `max_questions`, then branches:
     - **non-terminal, under cap, `probe_question` present** →
       `_probe_round(probe_question)`, await `record_answer_task`, return.
     - **non-terminal, under cap, no probe at all** — restore the answer to
       `self._buffer` with `_awaiting_answer` re-armed, bounded by
       `_MAX_REGRADE_ATTEMPTS` (1).
     - **terminal, or capped** — proceeds to close the round.
  6. **Await-ordering fix (load-bearing, not just latency).** Three writes
     change what the very NEXT candidate-queue computation
     (`analysis_orchestrator._run_batch`, in a different asyncio task) will
     see: `mark_target_given_up` (on capped-and-non-terminal),
     `mark_forced_topic_spent` (on a forced round closing), and the
     `UNABLE_TO_ANSWER` `apply_field_completeness` patch (via
     `build_unable_to_answer_patch`). All three are **awaited immediately**
     (`await asyncio.shield(...)`), **before** the flush that triggers the
     next combined-call cycle — not deferred alongside
     `record_answer_task`/`close_round_task`, which are pure bookkeeping
     that doesn't affect candidate-queue computation and stay deferred for
     latency. Deferring the three exclusion-relevant writes instead would
     race the flush-triggered combined call, letting a just-declined/
     just-capped/just-forced target reappear in the immediately-next queue.
  7. Computes `closing_key` (this round's own `forced_topic`, else
     `gap_key(target.block, target.item_id)`) BEFORE clearing
     `_current_round_id`/`_current_round_forced`, then fires
     `orchestrator.flush_transcript(..., wait=False)` and passes the
     returned `FlushRequest` into `_advance_from_queue(pending_flush,
     exclude_key=closing_key)` (see above — same fire-and-forget-with-
     fallback shape as the opening-round path, plus the exclusion filter so
     a stale queue snapshot can't immediately re-ask the target this round
     just closed), then awaits the deferred `record_answer_task`/
     `close_round_task` (swallowing `CancelledError`).
- **`except asyncio.CancelledError`** (candidate resumed speaking
  mid-grading): if the answer wasn't graded yet (`graded` flag not yet
  flipped), restores `_awaiting_answer` and prepends the original
  `answer_text` back onto `self._buffer`, then re-raises. Once `graded` has
  flipped `True`, a cancellation only affects already-shielded writes in
  flight, which finish independently.

**Doubt/meta-question handling** — the candidate can go off-script mid-
answer to ask about the process itself, detected as one extra field on the
*same* per-answer grading call (no added LLM round-trip).
`_handle_meta_question(response_text)` speaks it, then **re-speaks the
exact pending question** (cached in `self._current_question_text`), re-arms
`_awaiting_answer`, and leaves `_buffer` empty — no probe spent, no round
state otherwise touched.

**Session bootstrap and closing** — with no persona to fall back on, the
director owns both ends of the session:
- `ask_opening_question` — see above.
- `_complete_interview()` — speaks the fixed `CLOSING_MESSAGE` exactly once
  (guarded by `self._closed`), then calls `self._on_complete()`
  (`bot.end_session`), which queues an `EndFrame` right behind the closing
  `TTSSpeakFrame` — the `EndFrame` only tears down the transport once the
  closing message has actually been synthesized and pushed to output. The
  rest of teardown (`_on_bot_done`/`_close_out`) is the same path every
  other session end goes through — see
  [backend/room-orchestration.md](room-orchestration.md).
- `_leave_interview_mode()` clears `_awaiting_answer`/`_current_round_id`/
  `_current_round_forced`/`_current_question_text`/`_buffer`.

**TTS is never interrupted by user speech.** `pipeline.py`'s
`LLMContextAggregatorPair` construction passes an explicit
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
`enable_interruptions=False` only suppresses `broadcast_interruption()` (the
call that sends the `InterruptionFrame` cancelling in-flight TTS synthesis)
— it does **not** touch `enable_user_speaking_frames` (default `True`,
unaffected), which is the separate flag that still emits
`UserStartedSpeakingFrame`/`UserStoppedSpeakingFrame`, the frames every
silence/turn-detection timer in this file depends on.

**Observable side effect**: `AgentTranscriptBridge._clear_drip` in
`bridges.py` still reacts to `InterruptionFrame` to cut the caption drip
short. With interruption broadcast suppressed, captions keep dripping for
the full bot utterance even while the candidate talks over it.

## Data flow & dependencies
- Reads config from `app.core.config.settings`
  ([backend/app-config.md](app-config.md)) — provider choices, grace/idle
  timeouts, `resume_room_max_questions_per_round`.
- `UserTranscriptBridge`'s `on_final_transcription` callback is wired in
  `pipeline.py`'s `persist()` closure. For `role == "user"`, `persist()`
  always offers the line to `director.record_candidate_text(text)` (a
  buffering side effect only) *and* always does `crud.append_transcript_line`
  + `orchestrator.enqueue_transcript` — the handoff into
  [backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md), the
  same path used outside interview mode, unconditionally, mid-answer
  included, so extraction can fire on character count alone however long
  the candidate keeps talking.
- `UserTranscriptBridge`'s `on_speaking_change` callback is now a single
  direct in-process call to `director.on_speaking_change` — there is no
  second, cross-process consumer any more (the old batched completeness
  worker's speaking queue is gone entirely — see
  [backend/completeness-pipeline.md](completeness-pipeline.md)).
- **VAD is kept even though the chat LLM is gone.** `pipeline.py` still
  builds `context_llm = LLMContext()` and the `LLMContextAggregatorPair`
  above. This is pipecat's *only* configured source of
  `UserStartedSpeakingFrame`/`UserStoppedSpeakingFrame` — remove this pair
  and *all* silence/turn detection breaks, not just chat. Pipecat's
  standalone `VADProcessor` is not a drop-in alternative: it emits a
  different frame type (`VADUserStartedSpeakingFrame`), which
  `UserTranscriptBridge`'s `isinstance` checks would not match.
- Depends on external services: Sarvam STT/TTS and Daily transport/VAD
  (Silero) — all via pipecat service classes, not called directly.
- Only ever invoked from `room_orchestrator._run_guarded_bot` (lazy import,
  see [backend/room-orchestration.md](room-orchestration.md)).

## Conventions & gotchas
- `AGENT_CAPTION_WORDS_PER_SECOND` (2.5) in `bridges.py` is a deliberate
  approximation: Sarvam's TTS has no word-boundary timing events, so agent
  captions are dripped out word-by-word at a fixed pace, rather than
  jumping to the full sentence instantly.
- `AgentTranscriptBridge`'s drip queue must be cleared on
  `InterruptionFrame` (`_clear_drip`) or a stale sentence keeps dripping
  words after the bot has actually been interrupted.
- `GREETING_MESSAGE` (`pipeline.py`) and `CLOSING_MESSAGE`
  (`interview_director.py`) are literal spoken text, not LLM instructions.
- New STT/TTS providers are added by writing a new builder function and
  adding it to that module's `BUILDERS`/`builders` dict, then pointing the
  matching `settings.resume_room_*_provider` at its key.
- `ParticipantTracker.schedule_teardown` cancels the pipeline worker
  (`worker.cancel()`) after `resume_room_empty_room_grace_seconds` of an
  empty room — a pending teardown task is cancelled again if a participant
  rejoins in that window.

## Last synced
2026-09-05 (later still — fixed a live regression from the fire-and-forget
flush change just below: a round that just closed (SUFFICIENT,
UNABLE_TO_ANSWER, or given-up-on-capped) could be immediately re-asked,
because the queue snapshot popped right after closing it was sometimes a
stale one from BEFORE that closure had been folded into a regeneration
cycle -- the previous fix only guarded an EMPTY pop, not a non-empty-but-
stale one. New `_pop_next_queue_item(exclude_key)` discards any popped entry
whose key matches the just-closed round's own gap/forced-topic key;
`_advance_from_queue` gained a matching `exclude_key` parameter, and
`_finish_answer`'s ordinary-round path now computes `closing_key` before
clearing `_current_round_forced`/the round's target and passes it through.
The opening-round path is unaffected (no single target, so nothing to
exclude).)
2026-09-05 (later still — removed the two remaining `flush_transcript(...,
wait=True)` calls before `_advance_from_queue()` (opening-round and
ordinary-round paths in `_finish_answer`), which were adding a full
combined-analysis LLM round trip to every turn's latency before the next
question could be spoken. Both now fire `wait=False` and pass the returned
`FlushRequest` into `_advance_from_queue(pending_flush)`, which pops
`questions.queue` immediately (usually already populated from a prior
cycle) and only falls back to awaiting `pending_flush` if that first pop
comes back empty — preserving the one correctness property the blocking
wait existed for (never reading an empty-but-merely-stale queue as "genuinely
nothing left to ask" and ending the interview early). `ResumeRoomOrchestrator.flush_transcript`
now returns the `FlushRequest` (or `None`) in both the `wait=True` and
`wait=False` cases, rather than `None` always, so a `wait=False` caller can
still observe completion later on demand. See
[backend/room-orchestration.md](room-orchestration.md) for
`flush_transcript`'s own doc entry if one exists, and
[backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md) for
`FlushRequest`'s definition.)
2026-09-05 (major rewrite, ~500 lines top-to-bottom — replaced per-target
selection (`_pick_forced_topic`/`_forced_topic_description`/`_item_label`,
the in-memory `_organic_targets_given_up`/`_forced_topics_spent` sets, the
inline `compute_next_targets` call in `_finish_answer`) with a
queue-popping design: `_advance_from_queue` just pops the head of
`questions.queue` (regenerated wholesale by the combined analysis call —
see [backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md)) and
opens a round for it, no LLM call at pop time. `_finish_answer` now calls
the narrow `question_chain.run_answer_grading_chain` (only the round's own
`target_complete_when` bar, no resume/coverage/candidate-list inputs at
all) and branches on whether the round has a `target` at all (the opening
round has none, so it skips grading entirely). Exclusion bookkeeping
(`given_up_targets`/`forced_topics_spent`) moved off `InterviewDirector`'s
in-memory sets onto the persisted session row, written via new CRUD methods
`mark_target_given_up`/`mark_forced_topic_spent` — needed because the
analysis worker's task, not this class, now computes the candidate queue,
and the two tasks must see consistent state. The three writes that affect
the very next candidate-queue computation
(`mark_target_given_up`/`mark_forced_topic_spent`/the `UNABLE_TO_ANSWER`
`field_completeness` patch) are awaited immediately, before the flush,
rather than deferred like the purely-bookkeeping `record_round_answer`/
`close_round` writes — a deliberate correction over an earlier draft that
would have raced the flush-triggered combined call. `pipeline.py`'s
`on_speaking_change` no longer fans out to
`orchestrator.enqueue_speaking_state` (deleted along with the batched
completeness worker it fed — see
[backend/completeness-pipeline.md](completeness-pipeline.md)). Older
history predating this rewrite described the deleted free-target-selection/
fused-per-answer-call design in detail and has been removed from this file;
`docs/qa-flow-redesign-understanding.md` has the full design trail.)
