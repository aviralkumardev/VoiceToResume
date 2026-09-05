from app.core.config import settings

_initialized = False


def ensure_daily_runtime() -> None:
    """Initialise Daily's native runtime once per process. Idempotent."""
    global _initialized
    if _initialized:
        return

    from daily import Daily
    from pipecat.transports.daily.transport import DailyTransportClient

    if not hasattr(DailyTransportClient, "_daily_initialized"):
        raise RuntimeError(
            "RESUME ROOM: pipecat's DailyTransportClient._daily_initialized is gone — "
            "re-verify how Daily.init() is called before trusting worker_threads "
            "(runtime.py ensure_daily_runtime)"
        )

    worker_threads = settings.resume_room_daily_worker_threads
    Daily.init(worker_threads=worker_threads)
    DailyTransportClient._daily_initialized = True
    _initialized = True
