# Backend: Room Orchestration

## Purpose
The stateful coordinator for a resume-coaching session's whole lifecycle:
Daily room creation, spawning the voice bot as a background asyncio task,
wiring up **two** independent background workers (transcript-triggered
resume extraction, and silence-triggered completeness grading), and tearing
everything down cleanly (normal end, timeout, crash, or explicit stop) —
including deleting the Daily room and finalizing the CRUD record.

## Key files
- `backend/app/meeting_room/room_orchestrator.py` — `ResumeRoomOrchestrator`,
  the module singleton `get_orchestrator_instance()`.
- `backend/app/meeting_room/models.py` — `StartSessionResponse`,
  `StopSessionResponse` (shared with [api-routes.md](api-routes.md)).

## Public surface
- `ResumeRoomOrchestrator.start_session() -> StartSessionResponse` — checks
  the active-session cap (`resume_room_max_sessions`), creates a Daily room +
  bot/user tokens, creates a CRUD session record, spawns the transcript
  queue + resume-analysis worker task, spawns the speaking-state queue +
  completeness worker task, then spawns the guarded bot task (`run_bot`,
  timeout = `resume_room_max_session_seconds`). Raises `HTTPException`
  (503/502/500) on any failure, unwinding whatever was already created
  (both queues, both worker tasks).
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
- `ResumeRoomOrchestrator.flush_transcript(session_id, *, wait=True) -> None`
  (async) — puts a `FlushRequest` on the session's transcript queue and, if
  `wait` is true, awaits its `done` event (bounded by
  `resume_room_flush_timeout_seconds`). Because it's the same queue
  `enqueue_transcript` feeds, FIFO ordering guarantees every chunk enqueued
  before the call has already been folded into `accumulated_text` by the
  time the worker services the flush. No queue, a full queue, or a timeout
  all just no-op — the caller reads whatever `resume_data` currently holds,
  exactly as if the flush didn't exist.
  **`wait=False`** still enqueues the `FlushRequest` (so extraction is
  requested promptly rather than waiting for the char trigger) but returns
  immediately without blocking on the batch's LLM call.
  `InterviewDirector._finish_answer` calls it this way, unconditionally, on
  every turn — fire-and-forget, never awaited: Task A (extraction+coverage
  grading) always runs in the background, off the turn's critical path,
  which is now just the single per-answer LLM call (see
  [backend/stt-tts-pipeline.md](stt-tts-pipeline.md)). The director never
  awaits a flush at all any more — its former required-coverage safety net
  (`_await_task_a_settle`, which used to pass `wait=True` and additionally
  await `run_completeness_grading_cycle` directly right before deciding
  whether the interview could end) was deleted along with `required_gap.py`;
  `field_completeness` is now read as whatever snapshot is already on the
  row, no forced catch-up. The silence grader's own cycle (`_run_one_cycle`, see
  [backend/completeness-pipeline.md](completeness-pipeline.md)) separately
  calls `flush_transcript(session_id)` (default `wait=True`) before its own
  `resume_data` read — unrelated to and unaffected by the director's calls
  above, since each session's queue is shared but every caller simply waits
  on whichever `FlushRequest` it itself enqueued.
- `ResumeRoomOrchestrator.enqueue_speaking_state(session_id, is_speaking)` —
  called by the STT/TTS pipeline's `UserTranscriptBridge` on every VAD
  start/stop event, pushing `True`/`False` onto that session's speaking-state
  queue — see [backend/completeness-pipeline.md](completeness-pipeline.md).
  There is deliberately **no** question-delivery counterpart here: the
  interview director lives inside `run_bot`, already holds the pipeline
  worker, and speaks questions directly (see
  [backend/stt-tts-pipeline.md](stt-tts-pipeline.md)) — so the orchestrator
  needs no queue for it.
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
  [backend/stt-tts-pipeline.md](stt-tts-pipeline.md)),
  `run_resume_analysis_worker` (
  [backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md)), and
  `run_silence_completeness_worker` (
  [backend/completeness-pipeline.md](completeness-pipeline.md)).
- Owns four in-memory maps keyed by `session_id`: `_tasks` (bot task, plus
  `f"analysis_{session_id}"` for the analysis worker and
  `f"completeness_{session_id}"` for the completeness worker), `_queues`
  (transcript queue per session), `_speaking_queues` (speaking-state queue
  per session), plus a single shared `_daily` client and `_http` aiohttp
  session created lazily on first use.
- `_on_bot_done` (task `add_done_callback`) classifies how the bot task
  ended (cancelled / timed out / raised / finished normally), pushes the
  `None` end-sentinel onto **both** the transcript and speaking-state
  queues, and schedules `_close_out` as a new task to finalize CRUD
  status, delete the Daily room, and wait (bounded, 30s each) for both
  worker tasks to finish.

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
2026-09-05 (noted the interview director no longer ever awaits a flush —
its former required-coverage safety net, `_await_task_a_settle`, was
deleted along with `required_gap.py` as part of deterministic
block-priority target selection; see
[backend/stt-tts-pipeline.md](stt-tts-pipeline.md) and
[backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md).)
2026-09-04 (updated for enqueue_transcript dropping seq / reserve_transcript_seq;
added `flush_transcript`'s `wait` parameter — the director skips the blocking
wait when no BLOCK claim is pending)
