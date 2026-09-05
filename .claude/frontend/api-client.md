# Frontend: API Client

## Purpose
The only place the frontend makes HTTP calls to the backend. Two functions,
one error type, normalizing FastAPI's error response shapes (plain string
`detail` or pydantic validation-error array) into a readable message.

## Key files
- `frontend/src/lib/resume-room-api.ts`

## Public surface
- `startResumeRoomSession(): Promise<SessionInfo>` — `POST
  /resume-room/start`. Throws `ResumeRoomApiError` on a non-ok response.
- `stopResumeRoomSession(roomName: string): Promise<void>` — `POST
  /resume-room/stop/{roomName}`. Throws a plain `Error` (not
  `ResumeRoomApiError`) on failure — callers can't branch on `.status` for
  this one.
- `ResumeRoomApiError extends Error` — carries `status: number` so callers
  can branch (e.g. treat 503 as "all slots full" rather than a generic
  failure) without string-matching `.message`.

## Data flow & dependencies
- `API_BASE_URL` = `process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000"`
  — must match wherever the FastAPI backend
  ([backend/app-config.md](../backend/app-config.md)) is actually running;
  the backend's CORS `allow_origins` must in turn include this app's own
  origin.
- Calls the two endpoints documented in
  [backend/api-routes.md](../backend/api-routes.md); the `SessionInfo`
  shape returned by `startResumeRoomSession` must stay in sync with
  backend's `StartSessionResponse`.
- Consumed by `Home` (start) in
  [frontend/routing-app-shell.md](routing-app-shell.md) and
  `SessionControls` (stop) in
  [frontend/components-resumeroom.md](components-resumeroom.md).

## Conventions & gotchas
- `errorMessage()` strips the literal `"query"` location segment out of
  pydantic validation-error `loc` arrays before joining them — a
  FastAPI-specific detail; if the backend ever validates path/body params
  instead of only query params, revisit this filter.
- The two functions are inconsistent on purpose vs. by accident-looking:
  `startResumeRoomSession` throws the richer `ResumeRoomApiError`, `stop`
  throws a plain `Error`. Preserve `ResumeRoomApiError`'s status-branching
  ability if you unify these later — don't silently downgrade `start`'s
  error type.

## Last synced
2026-09-03
