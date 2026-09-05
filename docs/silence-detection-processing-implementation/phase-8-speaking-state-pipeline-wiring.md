# Phase 8 — Wiring the Speaking-State Callback into `run_bot`

## What this does

Connects `phase-7`'s new `on_speaking_change` callback to the orchestrator,
via a new `orchestrator.enqueue_speaking_state(session_id, is_speaking)`
method that `phase-10` adds. Mirrors exactly how `persist("user", text)`
already forwards final transcriptions to `orchestrator.enqueue_transcript`
— same closure-over-`orchestrator`/`session_id` pattern, same
"no-op if there's no orchestrator" guard.

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
    # ... unchanged, omitted here ...
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
        UserTranscriptBridge(send_app, on_final_transcription=lambda text: persist("user", text)),
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

**Change 1** — new closure alongside `persist`, right after it:

```python
    def persist(role: str, text: str):
        if crud is not None:
            asyncio.create_task(crud.append_transcript_line(session_id, role, text))
            if role == "user" and orchestrator is not None:
                orchestrator.enqueue_transcript(session_id, text)

    def on_speaking_change(is_speaking: bool):
        if orchestrator is not None:
            orchestrator.enqueue_speaking_state(session_id, is_speaking)
```

**Change 2** — pass it into `UserTranscriptBridge`'s construction:

```python
        UserTranscriptBridge(
            send_app,
            on_final_transcription=lambda text: persist("user", text),
            on_speaking_change=on_speaking_change,
        ),
```

## Key design points, explained

- **`on_speaking_change` doesn't need a `crud` guard the way `persist`
  does** — it never touches `crud` at all, only `orchestrator`, so the
  guard is just `orchestrator is not None`, matching `enqueue_transcript`'s
  own inner guard rather than `persist`'s outer one.
- **`enqueue_speaking_state` is called directly, not wrapped in
  `asyncio.create_task(...)`** — same as `enqueue_transcript` already is:
  both are expected to be cheap, non-blocking, synchronous calls
  (`put_nowait` under the hood, per `phase-10`), so there's nothing to
  schedule as a separate task.
