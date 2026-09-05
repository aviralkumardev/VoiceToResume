# Backend: Room Orchestration

## Purpose
The stateful coordinator for a resume-coaching session's whole lifecycle:
Daily room creation, spawning the voice bot as a background asyncio task,
wiring up the resume-analysis background worker (transcript-triggered,
combined extraction+grading+queue-regeneration), and tearing everything
down cleanly (normal end, timeout, crash, or explicit stop) — including
deleting the Daily room and finalizing the CRUD record.

There is now only **one** background worker per session (the resume-analysis
worker) — the old separate silence-triggered completeness worker and its
speaking-state queue are gone entirely; see
[backend/completeness-pipeline.md](completeness-pipeline.md).

## Key files
- `backend/app/meeting_room/room_orchestrator.py` — `ResumeRoomOrchestrator`,
  the module singleton `get_orchestrator_instance()`.
- `backend/app/meeting_room/models.py` — `StartSessionResponse`,
  `StopSessionResponse` (shared with [api-routes.md](api-routes.md)).

## Public surface
- `ResumeRoomOrchestrator.start_session() -> StartSessionResponse` — checks
  the active-session cap (`resume_room_max_sessions`), creates a Daily room +
  bot/user tokens, creates a CRUD session record, spawns the transcript
  queue + resume-analysis worker task, then spawns the guarded bot task
  (`run_bot`, timeout = `resume_room_max_session_seconds`). Raises
  `HTTPException` (503/502/500) on any failure, unwinding whatever was
  already created (the queue and the worker task).
- `ResumeRoomOrchestrator.stop_session(room_name) -> StopSessionResponse` —
  looks up the active session by room name, cancels its bot task (awaiting
  the cancellation) or marks it finished + deletes the room directly if no
  task is tracked.
- `ResumeRoomOrchestrator.enqueue_transcript(session_id, text)` — called by
  the STT/TTS pipeline (see
  [backend/stt-tts-pipeline.md](stt-tts-pipeline.md)) to push each finalized
  candidate utterance onto that session's analysis queue, as a plain string.
  Synchronous (`put_nowait`), which is what lets the director enqueue an
  answer and then flush, knowing the text is already in line ahead of the
  sentinel.
- `ResumeRoomOrchestrator.flush_transcript(session_id, *, wait=True) ->
  Optional[FlushRequest]` (async) — puts a `FlushRequest` on the session's
  transcript queue and, if `wait` is true, awaits its `done` event (bounded
  by `resume_room_flush_timeout_seconds`). Because it's the same queue
  `enqueue_transcript` feeds, FIFO ordering guarantees every chunk enqueued
  before the call has already been folded into `accumulated_text` by the
  time the worker services the flush. Returns the `FlushRequest` itself in
  BOTH the `wait=True` and `wait=False` cases (`None` only if the session's
  queue is already gone) — a `wait=False` caller keeps the handle so it can
  await `request.done` later, on demand, instead of never being able to
  observe completion at all. See
  [backend/stt-tts-pipeline.md](stt-tts-pipeline.md)'s
  `InterviewDirector._advance_from_queue` for the caller that does exactly
  this. No queue, a full queue, or a timeout all just no-op/return early —
  the caller reads whatever `resume_data`/`questions.queue` currently holds,
  exactly as if the flush didn't exist.
  **`wait=False`** still enqueues the `FlushRequest` (so the combined batch
  is requested promptly rather than waiting for the char trigger) but
  returns immediately without blocking on the batch's LLM call.
  `InterviewDirector._finish_answer` calls it `wait=False` twice per turn now
  — once right after grading starts (fire-and-forget, off the turn's
  critical path), and once more right before `_advance_from_queue`, handing
  that call's returned `FlushRequest` in as `pending_flush` so it's only
  awaited if popping `questions.queue` immediately comes back empty. See
  [backend/stt-tts-pipeline.md](stt-tts-pipeline.md).
- `ResumeRoomOrchestrator.shutdown()` — cancels all tracked tasks and closes
  the shared `aiohttp.ClientSession`; called from `app.main`'s FastAPI
  lifespan on process exit.
- `get_orchestrator_instance() -> ResumeRoomOrchestrator` — process-wide
  singleton accessor, used as a FastAPI dependency.

## Data flow & dependencies
- Depends on: `app.core.config.settings`, the CRUD layer (
  [backend/database-models.md](database-models.md)), `DailyClient` +
  `ensure_daily_runtime` ([backend/external-daily.md](external-daily.md)),
  `run_bot` (imported lazily inside `_run_guarded_bot` to avoid a hard
  import-time dependency on pipecat/daily-python — see
  [backend/stt-tts-pipeline.md](stt-tts-pipeline.md)), and
  `run_resume_analysis_worker` (
  [backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md)).
- Owns three in-memory maps keyed by `session_id`: `_tasks` (bot task, plus
  `f"analysis_{session_id}"` for the analysis worker), `_queues` (transcript
  queue per session), plus a single shared `_daily` client and `_http`
  aiohttp session created lazily on first use. There is no
  `_speaking_queues` map any more — the interview director gets its VAD
  speaking-state signal as a direct in-process call from `pipeline.py`, not
  through the orchestrator (see
  [backend/stt-tts-pipeline.md](stt-tts-pipeline.md)).
- `_on_bot_done` (task `add_done_callback`) classifies how the bot task
  ended (cancelled / timed out / raised / finished normally), pushes the
  `None` end-sentinel onto the transcript queue, and schedules `_close_out`
  as a new task to finalize CRUD status, delete the Daily room, and wait
  (bounded, 30s) for the analysis worker task to finish.

## Conventions & gotchas
- This is a single in-process orchestrator with in-memory task/queue maps —
  it does not survive a process restart and does not work across multiple
  backend processes/workers. Any change toward horizontal scaling needs a
  different task-tracking mechanism.
- `_run_guarded_bot` imports `run_bot` **inside the function body**, not at
  module top — pipecat/daily-python ship Linux-only wheels, so importing
  them at module load time would break non-Linux dev environments even when
  the meeting-room feature is never invoked. Don't move that import to the
  top of the file.
- Closing out a session always attempts CRUD `mark_finished` and Daily
  `delete_room`, each independently wrapped in `except Exception: pass` —
  errors here are intentionally swallowed so one failure doesn't block the
  other. Don't add strict error propagation here without re-checking this
  intent.
- `enqueue_transcript` silently drops text if the queue is full or missing
  (`asyncio.QueueFull` / no queue) — analysis loss during backpressure is
  accepted, not fatal to the call.

## Last synced
2026-09-05 (major rewrite — removed the separate speaking-state queue
plumbing entirely: `enqueue_speaking_state`, `_speaking_queues`, and the
`f"completeness_{session_id}"` task tracking (creation in `start_session`,
sentinel-push in `_on_bot_done`, wait/cancel in `_close_out`, cleanup on the
bot-spawn-failure path) are all deleted, along with the
`run_silence_completeness_worker` import and `cancel_grading_tasks` call
sites — the batched completeness worker they supported no longer exists,
since coverage grading is now fused into the same combined analysis call
extraction already used. There is exactly one background worker per session
now. See [backend/completeness-pipeline.md](completeness-pipeline.md) and
[backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md). `pipeline.py`'s
`on_speaking_change` now calls only `director.on_speaking_change` directly
— see [backend/stt-tts-pipeline.md](stt-tts-pipeline.md).)
2026-09-05 (later still — `flush_transcript` now returns the `FlushRequest`
(or `None`) in both the `wait=True` and `wait=False` cases, instead of
always returning `None`. This is what let `InterviewDirector._finish_answer`
drop its last two blocking `flush_transcript(..., wait=True)` calls before
`_advance_from_queue` — every flush call is now `wait=False`, with the
returned `FlushRequest` handed into `_advance_from_queue(pending_flush)` so
it's only awaited as a fallback if popping `questions.queue` immediately
comes back empty. Net effect: a full combined-analysis LLM round trip is no
longer on the critical path of every single turn, only on the rare turn
where the queue happens to be transiently empty. See
[backend/stt-tts-pipeline.md](stt-tts-pipeline.md).)
2026-09-05 (noted the interview director no longer ever awaits a flush for
a required-coverage safety net — that safety net doesn't exist any more;
`_advance_from_queue` reads whatever `questions.queue` snapshot is already
on the row.)
