# Backend: API Routes (Resume Room)

## Purpose
The only HTTP surface the frontend talks to: start and stop a resume-coaching
video session. Thin — all real logic is delegated to the orchestrator (see
[backend/room-orchestration.md](room-orchestration.md)).

## Key files
- `backend/app/meeting_room/routes.py` — the `APIRouter`.
- `backend/app/meeting_room/models.py` — request/response pydantic models.

## Public surface
- `POST /resume-room/start` → `StartSessionResponse {roomUrl, token,
  roomName}`. Creates a Daily room, spawns the voice bot, returns join
  credentials for the candidate's browser. 503 if the session cap is hit or
  Daily isn't configured/available; 502 if Daily room/token creation fails;
  500 on internal/session-record/bot-spawn failure.
- `POST /resume-room/stop/{room_name}` → `StopSessionResponse {ok: true}`.
  Cancels the running bot task for that room (or marks it finished directly
  if no task is tracked) and deletes the Daily room. Always returns `ok:
  true`, even if no active session was found for that room name.

## Data flow & dependencies
- Both endpoints depend on `get_orchestrator_instance()` (FastAPI `Depends`)
  — see [backend/room-orchestration.md](room-orchestration.md) for what
  `start_session`/`stop_session` actually do.
- Router is mounted in `app.main` with prefix `/resume-room` and tag
  `resume-room` (plus an app-level tag `resume-meeting-room` on include).
- Consumed by the frontend's `startResumeRoomSession` /
  `stopResumeRoomSession` in
  [frontend/api-client.md](../frontend/api-client.md).

## Conventions & gotchas
- Route handlers never construct `HTTPException` bodies themselves for
  business logic — they re-raise whatever `HTTPException` the orchestrator
  already raised, and only wrap *unexpected* exceptions in a generic 500.
  Keep new routes following this pattern rather than duplicating error
  mapping in the route layer.
- `room_name` in the URL is the Daily room name (not the internal
  `session_id`) — the orchestrator resolves it back to a session via
  `get_active_by_room_name`.

## Last synced
2026-09-03
