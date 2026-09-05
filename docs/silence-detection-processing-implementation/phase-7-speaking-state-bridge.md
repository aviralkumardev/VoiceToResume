# Phase 7 — Speaking-State Signal on `UserTranscriptBridge`

## What this does

`UserTranscriptBridge` already sits at the right point in the pipeline and
already imports `UserStartedSpeakingFrame` (to reset per-turn caption
numbering) — but it has no handling for `UserStoppedSpeakingFrame` at all
today, and nothing about "the user started/stopped speaking" ever leaves
this class. This phase extends the existing bridge (confirmed over adding a
new dedicated bridge class) with an `on_speaking_change` callback, called
`True` on start and `False` on stop, so `phase-8` can forward that signal
out to the orchestrator.

**This class's existing caption-related behavior is untouched** — the new
code is additive (a new constructor param, two new callback calls), nothing
about `_start_line`/`_turns`/`_sent` or the transcript-forwarding logic
changes.

## File to modify: `backend/app/meeting_room/stt_tts_pipeline/processors/bridges.py`

Current file (for reference — this is what exists today):

```python
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
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

SendApp = Callable[[dict], Awaitable[None]]

# ... AGENT_CAPTION_WORDS_PER_SECOND, SpeakingBridge unchanged, omitted here ...


class UserTranscriptBridge(FrameProcessor):
    def __init__(
        self,
        send_app: SendApp,
        *,
        on_final_transcription: Optional[Callable[[str], None]] = None,
    ):
        super().__init__()
        self._send_app = send_app
        self._on_final_transcription = on_final_transcription
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
```

(`AgentTranscriptBridge` and the rest of the file are untouched by this phase — omitted above for brevity, but nothing below it needs to change.)

**Change 1** — add `UserStoppedSpeakingFrame` to the `pipecat.frames.frames` import:

```python
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    LLMFullResponseStartFrame,
    TranscriptionFrame,
    TTSTextFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
```

**Change 2** — new constructor param on `UserTranscriptBridge`:

```python
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
```

**Change 3** — call it from `process_frame`, right alongside the existing `UserStartedSpeakingFrame` handling, plus a new branch for the stop frame:

```python
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
```

## Key design points, explained

- **`on_speaking_change` is called synchronously, not awaited** — same
  style as `on_final_transcription` already being a plain (non-async)
  callback. `phase-8`'s wiring keeps it that way by having the callback do
  a non-blocking `put_nowait` rather than anything that needs an event
  loop await inside `process_frame`.
- **Called unconditionally on every start/stop frame**, no debouncing or
  de-duplication here — that's `phase-9`'s worker's job entirely. This
  bridge's only responsibility is "tell the orchestrator the raw VAD signal
  changed," not "decide what that means."
- **Placed before the transcript-forwarding block**, not after — keeps the
  ordering "handle discrete state-transition frames first, then handle
  frames that carry the actual transcript text," matching how
  `UserStartedSpeakingFrame`'s existing turn-reset logic is already
  positioned first in the method.
