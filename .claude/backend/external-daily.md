# Backend: Daily.co Integration

## Purpose
Thin wrapper around Daily's REST API (room/token management) and its native
runtime initialization, isolating the rest of the backend from the
Linux-only `daily-python`/pipecat SDKs.

## Key files
- `backend/app/meeting_room/daily/client.py` — `DailyClient`,
  `DailyClientError` hierarchy.
- `backend/app/meeting_room/daily/runtime.py` — `ensure_daily_runtime()`.

## Public surface
- `DailyClient(api_key, aiohttp_session)` — constructed once per process by
  the orchestrator.
  - `ensure_available()` — raises `DailyNotConfiguredError` (no API key) or
    `DailyUnavailableError` (SDK not installed on this platform) without
    making a network call.
  - `create_room(*, room_expiry_seconds, max_participants) -> Any` — private
    room, video off by default, no prejoin UI.
  - `get_token(room_url, *, expiry_time, owner, user_name=None) -> str` —
    owner tokens (the bot) get no extra properties; non-owner tokens
    (candidate) get `user_name` + `start_video_off`.
  - `get_name_from_url(room_url) -> str`.
  - `delete_room(room_name) -> None`.
- `ensure_daily_runtime()` — idempotent, process-global `Daily.init(...)`
  call; must run before the pipecat `DailyTransport` is used.
- `DailyClientError`, `DailyNotConfiguredError`, `DailyUnavailableError` —
  caught in `room_orchestrator._get_daily()` and translated to a 503
  `HTTPException`.

## Data flow & dependencies
- Wraps `pipecat.transports.daily.utils.DailyRESTHelper` and related param
  types — imported lazily inside `_daily_utils()`, not at module load, so
  importing `daily/client.py` itself never requires the SDK to be installed
  (only calling into it does).
- Called exclusively by `ResumeRoomOrchestrator` — see
  [backend/room-orchestration.md](room-orchestration.md).
- `ensure_daily_runtime()` is called once, right before spawning the bot
  task, and reads `settings.resume_room_daily_worker_threads`.

## Conventions & gotchas
- `daily-python` and pipecat's Daily transport ship **Linux-only wheels** —
  this whole domain is written to degrade to a clean `DailyUnavailableError`
  rather than an import crash on other platforms. Preserve the lazy-import
  pattern in both files if you touch them.
- `ensure_daily_runtime()` asserts
  `DailyTransportClient._daily_initialized` exists before setting it — this
  is a defensive check against a pipecat upgrade silently removing that
  attribute; if that assertion ever fires, re-verify how pipecat expects
  `Daily.init()` to be called before trusting `worker_threads` again (this
  is the exact repo-authored gotcha comment in `runtime.py`).
- `DailyClient` lazily builds its `DailyRESTHelper` on first real use (not
  in `__init__`), so constructing a `DailyClient` with a bad/missing API key
  doesn't fail until `ensure_available()` or another method is called.

## Last synced
2026-09-03
