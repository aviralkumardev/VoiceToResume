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
from pipecat.turns.user_start import TranscriptionUserTurnStartStrategy, VADUserTurnStartStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.workers.runner import WorkerRunner


from app.core.config import settings
from app.meeting_room.stt_tts_pipeline.bot_session import BotSession
from app.meeting_room.stt_tts_pipeline.interview_director import InterviewDirector
from app.meeting_room.stt_tts_pipeline.processors.bridges import (
    AgentTranscriptBridge,
    SpeakingBridge,
    UserTranscriptBridge,
)
from app.meeting_room.stt_tts_pipeline.stt import build_stt
from app.meeting_room.stt_tts_pipeline.tts import build_tts


# _pipecat_logger.remove()

# Spoken straight through TTS by BotSession.greet() -- this is the literal
# first thing the bot ever says, no LLM involved. It doubles as
# InterviewDirector's opening question (see ask_opening_question).
GREETING_MESSAGE = (
    "Hi, welcome! I’ll help you build your resume through a short conversation. To get started, tell me about your education, any work experience, personal projects you’ve worked on, and your main skills. Take your time and walk me through each area."
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

    director: "InterviewDirector | None" = None

    def persist(role: str, text: str):
        if crud is None:
            return
        if role == "user" and director is not None:
            # Buffers into the director's own per-answer text (for grading)
            # as a side effect -- doesn't gate anything below. Extraction
            # always sees every line live now, mid-answer included.
            director.record_candidate_text(text)
        asyncio.create_task(crud.append_transcript_line(session_id, role, text))
        if role == "user" and orchestrator is not None:
            orchestrator.enqueue_transcript(session_id, text)


    def on_speaking_change(is_speaking: bool):
        if director is not None:
            director.on_speaking_change(is_speaking)

    # There is no persona LLM anymore, but LLMContextAggregatorPair is kept
    # anyway: LLMUserAggregatorParams(vad_analyzer=...) is what actually
    # builds pipecat's VADController and emits UserStartedSpeakingFrame /
    # UserStoppedSpeakingFrame -- the only source of those frames anywhere in
    # this pipeline, and what UserTranscriptBridge turns into
    # on_speaking_change for both InterviewDirector and the batched
    # completeness worker. Removing this pair would silently kill all
    # silence/turn detection, not just chat.
    #
    # user_turn_strategies is set explicitly (rather than left at pipecat's
    # default) to disable enable_interruptions on the VAD strategy: that flag
    # only gates whether the user speaking triggers broadcast_interruption()
    # (which cancels in-flight TTS synthesis) -- it is independent of
    # enable_user_speaking_frames (untouched, default True), which is what
    # still emits UserStartedSpeakingFrame/UserStoppedSpeakingFrame above.
    # The net effect: a question in flight is spoken to completion even if
    # the candidate starts talking over it, while silence/turn detection for
    # the interview loop keeps working exactly as before.
    # TranscriptionUserTurnStartStrategy() must still be listed explicitly --
    # it's the OTHER half of pipecat's own default and is not implied by
    # naming only the VAD strategy here.
    context_llm = LLMContext()
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

    bot = BotSession(
        transport,
        send_app=send_app,
        greeting=GREETING_MESSAGE,
    )

    pipeline = Pipeline([
        transport.input(),
        build_stt(),
        UserTranscriptBridge(send_app, on_final_transcription=lambda text: persist("user", text), on_speaking_change=on_speaking_change),
        user_aggregator,
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

    if crud is not None:
        director = InterviewDirector(
            session_id, crud, orchestrator, worker, on_complete=bot.end_session
        )
        bot.director = director

    transport.event_handler("on_joined")(bot.on_joined)
    transport.event_handler("on_first_participant_joined")(bot.tracker.on_first_participant_joined)
    transport.event_handler("on_participant_joined")(bot.tracker.on_participant_joined)
    transport.event_handler("on_participant_left")(bot.tracker.on_participant_left)

    runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
    await runner.add_workers(worker)
    try:
        await runner.run()
    finally:
        if director is not None:
            director.cancel()
