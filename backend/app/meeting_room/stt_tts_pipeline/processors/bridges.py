import asyncio
from typing import Awaitable, Callable, Optional

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    LLMFullResponseStartFrame,
    TranscriptionFrame,
    TTSTextFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

SendApp = Callable[[dict], Awaitable[None]]

# Sarvam's streaming TTS API has no word-boundary/timing events (unlike
# Cartesia, which pipecat feeds word-by-word with a real pts per word). Its
# TTSTextFrame carries one whole sentence, unstamped, so the base TTS service
# delivers it to the transport's "sync" path the instant the sentence is
# dispatched — before that sentence's audio has actually finished playing.
# Left alone, the caption would jump to the full sentence immediately and then
# sit static while the voice is still catching up. Dripping it out word by
# word at a plausible speaking pace is our own approximation of the timing
# Sarvam's API doesn't provide.
AGENT_CAPTION_WORDS_PER_SECOND = 2.5


class SpeakingBridge(FrameProcessor):
    """Forward bot speaking state to the frontend.

    Emits `{type: "speaking", speaker: <bot_daily_id>, value}` so the
    frontend can use one message shape for all participants and key it to
    the correct tile by session id.
    """

    def __init__(self, send_app: SendApp, speaker_id: Callable[[], str]):
        super().__init__()
        self._send_app = send_app
        self._speaker_id = speaker_id

    async def process_frame(self, frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, BotStartedSpeakingFrame):
            try:
                await self._send_app({"type": "speaking", "speaker": self._speaker_id(), "value": True})
            except Exception:
                pass
        elif isinstance(frame, BotStoppedSpeakingFrame):
            try:
                await self._send_app({"type": "speaking", "speaker": self._speaker_id(), "value": False})
            except Exception:
                pass
        await self.push_frame(frame, direction)


class UserTranscriptBridge(FrameProcessor):
    def __init__(
        self,
        send_app: SendApp,
        *,
        on_final_transcription: Optional[Callable[[str], None]] = None,
        on_speaking_change: Optional[Callable[[bool], None]] = None,
    ):
        super().__init__()
        self._send_app = send_app
        self._on_final_transcription = on_final_transcription
        self._on_speaking_change = on_speaking_change
        self._turns: dict[str, int] = {}
        self._sent: dict[str, str] = {}

    def _user_id(self, frame) -> str:
        return getattr(frame, "user_id", None) or "unknown"

    def _start_line(self, user_id: str):
        turn_n = self._turns.get(user_id, 0) + 1
        self._turns[user_id] = turn_n
        self._sent[user_id] = ""

    async def process_frame(self, frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, UserStartedSpeakingFrame):
            for uid in list(self._turns.keys()):
                self._start_line(uid)
            if self._on_speaking_change:
                self._on_speaking_change(True)

        if isinstance(frame, UserStoppedSpeakingFrame):
            if self._on_speaking_change:
                self._on_speaking_change(False)

        if isinstance(frame, (InterimTranscriptionFrame, TranscriptionFrame)) and frame.text:
            uid = self._user_id(frame)
            turn_n = self._turns.get(uid, 0)
            if frame.text != self._sent.get(uid, ""):
                self._sent[uid] = frame.text
                try:
                    await self._send_app(
                        {
                            "type": "transcript",
                            "speaker": uid,
                            "text": frame.text,
                            "turn": f"user-{uid}-{turn_n}",
                            "replace": True,
                        }
                    )
                except Exception:
                    pass

            if isinstance(frame, TranscriptionFrame):
                if self._on_final_transcription:
                    self._on_final_transcription(frame.text)
                self._start_line(uid)

        await self.push_frame(frame, direction)


class AgentTranscriptBridge(FrameProcessor):
    def __init__(
        self,
        send_app: SendApp,
        speaker_id: Callable[[], str],
        *,
        on_text: Optional[Callable[[str], None]] = None,
    ):
        super().__init__()
        self._send_app = send_app
        self._speaker_id = speaker_id
        self._fallback_turn = 0
        self._on_text = on_text
        self._drip_queue: asyncio.Queue = asyncio.Queue()
        self._drip_task: Optional[asyncio.Task] = None

    async def process_frame(self, frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMFullResponseStartFrame):
            self._fallback_turn += 1
        if isinstance(frame, InterruptionFrame):
            await self._clear_drip()
        if isinstance(frame, TTSTextFrame) and frame.text:
            if self._on_text:
                self._on_text(frame.text)
            if self._drip_task is None:
                self._drip_task = self.create_task(self._drip_worker())
            await self._drip_queue.put((frame.text, frame.context_id or f"agent-{self._fallback_turn}"))
        await self.push_frame(frame, direction)

    async def _clear_drip(self):
        if self._drip_task is not None:
            await self.cancel_task(self._drip_task)
            self._drip_task = None
        self._drip_queue = asyncio.Queue()

    async def _drip_worker(self):
        while True:
            text, turn = await self._drip_queue.get()
            words = text.split()
            for i, word in enumerate(words):
                try:
                    await self._send_app(
                        {
                            "type": "transcript",
                            "speaker": self._speaker_id(),
                            "text": word,
                            "turn": turn,
                            "replace": False,
                        }
                    )
                except Exception:
                    pass
                if i < len(words) - 1:
                    await asyncio.sleep(1 / AGENT_CAPTION_WORDS_PER_SECOND)

    async def cleanup(self):
        await self._clear_drip()
        await super().cleanup()
