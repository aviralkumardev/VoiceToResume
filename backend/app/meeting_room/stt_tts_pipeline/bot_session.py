from pipecat.frames.frames import EndFrame

from app.meeting_room.stt_tts_pipeline.participant_tracker import ParticipantTracker


class BotSession:
    def __init__(self,
        transport,
        *,
        send_app,
        greeting: str,
        worker=None,
    ):
        self._transport = transport
        self._send_app = send_app
        self._greeting = greeting
        self.worker = worker
        self.director = None

        self.bot_id: str = ""
        self.greeted: bool = False

        self.tracker = ParticipantTracker(
            transport,
            get_bot_id=self.get_bot_id,
            greet=self.greet,
            get_worker=lambda: self.worker,
        )

    def get_bot_id(self) -> str:
        return self.bot_id

    async def greet(self) -> None:
        if self.greeted:
            return
        self.greeted = True
        await self._send_app({"type": "agent-ready", "participantId": self.bot_id})
        if self.director is not None:
            await self.director.ask_opening_question(self._greeting)

    async def end_session(self) -> None:
        if not self.worker:
            return
        await self.worker.queue_frames([EndFrame()])

    async def on_joined(self, transport, data):
        local = data.get("participants", {}).get("local", {})
        self.bot_id = local.get("id", "")

        for pid in self.tracker.human_ids():
            await self.tracker.capture_participant(pid)
            await self.greet()
