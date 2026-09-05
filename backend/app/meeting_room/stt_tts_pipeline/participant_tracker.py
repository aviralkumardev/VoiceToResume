from __future__ import annotations

import asyncio
from typing import Callable, Optional

from app.core.config import settings


def _pid(participant) -> Optional[str]:
    return participant.get("id") if isinstance(participant, dict) else getattr(participant, "id", None)


class ParticipantTracker:
    def __init__(
        self,
        transport,
        *,
        get_bot_id: Callable[[], str],
        greet: Callable[[], None],
        get_worker: Callable[[], object],
    ) -> None:
        self._transport = transport
        self._get_bot_id = get_bot_id
        self._greet = greet
        self._get_worker = get_worker
        self.captured: set[str] = set()
        self.teardown_task: Optional[asyncio.Task] = None

    def human_ids(self) -> set[str]:
        return {
            pid
            for pid, p in self._transport.participants().items()
            if pid != "local" and not p.get("local")
        }

    async def capture_participant(self, pid: str) -> None:
        self._cancel_pending_teardown()
        if pid in self.captured:
            return
        self.captured.add(pid)
        await self._transport.capture_participant_audio(
            pid, "microphone", self._transport.input().sample_rate
        )

    def _cancel_pending_teardown(self) -> None:
        pending = self.teardown_task
        if pending and not pending.done():
            pending.cancel()

    def schedule_teardown(self) -> None:
        pending = self.teardown_task
        if pending and not pending.done():
            return

        tracker_ref = self

        async def _wait_then_cancel():
            try:
                await asyncio.sleep(settings.resume_room_empty_room_grace_seconds)
                if not tracker_ref.human_ids():
                    worker = tracker_ref._get_worker()
                    if worker is not None:
                        await worker.cancel()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

        self.teardown_task = asyncio.create_task(_wait_then_cancel())

    async def on_first_participant_joined(self, transport, participant):
        pid = _pid(participant)
        if pid:
            await self.capture_participant(pid)
        else:
            for p_id in self.human_ids():
                await self.capture_participant(p_id)
                break
        await self._greet()

    async def on_participant_joined(self, transport, participant):
        pid = _pid(participant)
        if pid and pid != self._get_bot_id():
            await self.capture_participant(pid)

    async def on_participant_left(self, transport, participant, reason):
        pid = _pid(participant)
        self.captured.discard(pid)
        remaining = self.human_ids() - {pid}
        if remaining:
            return
        self.schedule_teardown()
