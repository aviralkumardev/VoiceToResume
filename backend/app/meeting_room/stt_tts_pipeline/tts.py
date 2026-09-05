from pipecat.services.sarvam.tts import SarvamTTSService

from app.core.config import settings
from app.meeting_room.stt_tts_pipeline import select_provider


def _sarvam():
    return SarvamTTSService(
        api_key=settings.sarvam_api_key,
        settings=SarvamTTSService.Settings(
            voice=settings.resume_room_sarvam_tts_speaker,
            model=settings.resume_room_sarvam_tts_model,
            language=settings.resume_room_sarvam_language,
        ),
    )


BUILDERS = {"sarvam": _sarvam}


def build_tts():
    return select_provider(settings.resume_room_tts_provider, BUILDERS)
