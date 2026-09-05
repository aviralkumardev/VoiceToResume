from pipecat.services.sarvam.stt import SarvamSTTService

from app.core.config import settings
from app.meeting_room.stt_tts_pipeline import select_provider


def _sarvam():
    return SarvamSTTService(
        api_key=settings.sarvam_api_key,
        settings=SarvamSTTService.Settings(model=settings.resume_room_sarvam_stt_model),
    )


BUILDERS = {"sarvam": _sarvam}


def build_stt():
    return select_provider(settings.resume_room_stt_provider, BUILDERS)
