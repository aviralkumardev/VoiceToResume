# Phase 8 — Delivering the Question Into the Live Bot

## What this does

`run_bot` gains an optional `question_queue` param (`phase-7` hands it
the per-session queue it created). When present, `run_bot` spawns one more
internal background task — living inside the same coroutine as the rest
of the pipeline setup, with direct access to `context_llm` and `worker` —
that waits on `question_queue.get()` until it sees the `None` teardown
sentinel `_on_bot_done` pushes. On a real question, it:

1. Appends a steering message to `context_llm` — the exact same
   `add_message(...)` mechanism `BotSession.greet()` already uses to seed
   the greeting, just with instructional phrasing instead of the greeting
   text.
2. Queues an `LLMRunFrame()` on `worker` — again exactly what `greet()`
   does — which makes the persona LLM (`PERSONA_PROMPT`) produce its next
   turn informed by that steering message.

The persona LLM decides how to actually phrase it in the conversation;
this never bypasses `PERSONA_PROMPT`'s tone or speaks the question text
verbatim through TTS directly.

## File to modify: `backend/app/meeting_room/stt_tts_pipeline/pipeline.py`

Current file (for reference — this is what exists today):

```python
import asyncio

from loguru import logger as _pipecat_logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.transports.daily.transport import (
    DailyOutputTransportMessageUrgentFrame,
    DailyParams,
    DailyTransport,
)
from pipecat.workers.runner import WorkerRunner

from app.core.config import settings
from app.meeting_room.stt_tts_pipeline.bot_session import BotSession
from app.meeting_room.stt_tts_pipeline.llm import build_llm
from app.meeting_room.stt_tts_pipeline.processors.bridges import (
    AgentTranscriptBridge,
    SpeakingBridge,
    UserTranscriptBridge,
)
from app.meeting_room.stt_tts_pipeline.stt import build_stt
from app.meeting_room.stt_tts_pipeline.tts import build_tts


# _pipecat_logger.remove()

GREETING_PROMPT = "Greet the candidate in one sentence and ask them to walk you through their resume."

PERSONA_PROMPT = (
    "You are a friendly resume coach running a low-pressure mock interview. "
    "Your goal is to help the candidate practice describing their resume "
    "out loud, not to test or challenge them.\n\n"
    "How to behave:\n"
    "- Ask the candidate to walk through their resume, one item at a time.\n"
    "- Do not probe, push back, or ask follow-ups like 'why' or 'how "
    "exactly.' Accept what they say at face value.\n"
    "- If the candidate asks you a simple question — about the process, a "
    "term on their resume, or what to say next — answer it briefly and "
    "supportively, then return to the walkthrough.\n"
    "- Keep every response short (1-3 sentences) and conversational, like a "
    "supportive colleague, not an interviewer.\n\n"
    "Example:\n"
    "Candidate: \"I led the migration to a new backend framework.\"\n"
    "You: \"Nice, that's a solid one to highlight! What's next on your "
    "resume?\"\n"
)


async def run_bot(
    room_url: str,
    token: str,
    *,
    room_name: str = "",
    session_id: str = "",
    orchestrator=None,
):
    transport = DailyTransport(
        room_url,
        token,
        settings.resume_room_bot_name,
        DailyParams(audio_in_enabled=True, audio_out_enabled=True, video_in_enabled=False),
    )

    async def send_app(msg: dict):
        try:
            await transport.output().send_message(DailyOutputTransportMessageUrgentFrame(message=msg))
        except Exception:
            pass

    crud = orchestrator._crud if orchestrator is not None else None

    def persist(role: str, text: str):
        if crud is not None:
            asyncio.create_task(crud.append_transcript_line(session_id, role, text))
            if role == "user" and orchestrator is not None:
                orchestrator.enqueue_transcript(session_id, text)


    def on_speaking_change(is_speaking: bool):
        if orchestrator is not None:
            orchestrator.enqueue_speaking_state(session_id, is_speaking)

    context_llm = LLMContext()
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context_llm,
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
    )

    bot = BotSession(
        transport,
        send_app=send_app,
        context=context_llm,
        greeting=GREETING_PROMPT,
    )

    pipeline = Pipeline([
        transport.input(),
        build_stt(),
        UserTranscriptBridge(send_app, on_final_transcription=lambda text: persist("user", text), on_speaking_change=on_speaking_change),
        user_aggregator,
        build_llm(PERSONA_PROMPT),
        build_tts(),
        SpeakingBridge(send_app, bot.get_bot_id),
        transport.output(),
        AgentTranscriptBridge(send_app, bot.get_bot_id, on_text=lambda text: persist("assistant", text)),
        assistant_aggregator,
    ])

    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
        idle_timeout_secs=settings.resume_room_idle_timeout_seconds,
    )

    bot.worker = worker

    transport.event_handler("on_joined")(bot.on_joined)
    transport.event_handler("on_first_participant_joined")(bot.tracker.on_first_participant_joined)
    transport.event_handler("on_participant_joined")(bot.tracker.on_participant_joined)
    transport.event_handler("on_participant_left")(bot.tracker.on_participant_left)

    runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
    await runner.add_workers(worker)
    await runner.run()
```

**Change 1** — new import, alongside the other `pipecat` imports:

```python
from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
```

(i.e. add the `LLMRunFrame` import line; placement relative to the other
imports doesn't matter, this keeps it near the other top-level `pipecat`
imports.)

**Change 2** — `run_bot`'s signature gains `question_queue`:

```python
async def run_bot(
    room_url: str,
    token: str,
    *,
    room_name: str = "",
    session_id: str = "",
    orchestrator=None,
    question_queue=None,
):
```

**Change 3** — spawn the consumer task right after `bot.worker = worker`,
before the `transport.event_handler(...)` registrations:

```python
    bot.worker = worker

    if question_queue is not None:
        async def consume_next_questions():
            while True:
                question = await question_queue.get()
                if question is None:
                    break
                context_llm.add_message({
                    "role": "user",
                    "content": (
                        "(Internal interview note, not something the candidate "
                        "said out loud -- naturally steer your next reply "
                        f"toward asking: {question})"
                    ),
                })
                try:
                    await worker.queue_frames([LLMRunFrame()])
                except Exception:
                    pass

        question_task = asyncio.create_task(consume_next_questions())

    transport.event_handler("on_joined")(bot.on_joined)
```

The rest of `run_bot` (event handler registrations, `runner`,
`await runner.run()`) is unchanged.

## Key design points, explained

- **`question_task` is a plain local variable, never explicitly
  cancelled** — it's kept alive simply by staying in `run_bot`'s local
  scope for the function's whole lifetime (Python/asyncio only needs *a*
  live reference somewhere to avoid the task being garbage-collected
  mid-await; it doesn't need to be stored on `bot` or awaited). It exits
  on its own once it reads the `None` sentinel `_on_bot_done` pushes
  (`phase-7`) — by the time that fires, `run_bot`'s own `await
  runner.run()` has already returned (that's *why* `_on_bot_done` runs, as
  the bot task's done-callback), so this task quietly finishes shortly
  after in the background. Nothing needs to await it, matching how nothing
  in `room_orchestrator.py` reaches into `run_bot`'s own internal
  worker/runner teardown either.
- **Placed after `bot.worker = worker`, not earlier** — the closure needs
  a real `worker` reference to call `worker.queue_frames(...)`, and
  `context_llm` already exists by this point too (created well before the
  `pipeline = Pipeline([...])` block). Spawning it any earlier would just
  mean an unnecessarily early `asyncio.create_task` with nothing new
  available yet.
- **The steering message uses `role: "user"`, matching `BotSession.greet()`
  exactly** (`self._context.add_message({"role": "user", "content":
  self._greeting})`) rather than `"system"` — this codebase's existing
  convention for injecting bot-behavior directives is a `user`-role turn
  with instructional phrasing (see `GREETING_PROMPT` itself: "Greet the
  candidate in one sentence and ask them to..." is written as an
  instruction, not something a candidate would say), not a `system`
  message. Staying consistent with that avoids introducing a second
  pattern for the same kind of thing.
- **`await worker.queue_frames([LLMRunFrame()])` is wrapped in a bare
  `try/except Exception: pass`**, matching this file's existing fail-soft
  posture elsewhere (`send_app`'s own `try/except Exception: pass`) — a
  transient failure to inject one steering turn should never crash the
  whole bot pipeline or the consumer loop itself (the `while True` keeps
  running either way, ready for the next question).
- **If `question_queue` is `None`** (shouldn't happen given `phase-7`
  always creates and passes one, but keeps this file robust standalone,
  e.g. if `run_bot` is ever invoked directly in a test without an
  orchestrator) — no consumer task is spawned at all, and the rest of the
  pipeline behaves exactly as it did before this phase.
