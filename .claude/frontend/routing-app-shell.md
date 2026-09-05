# Frontend: App Shell & Routing

## Purpose
The Next.js App Router entry surface — single-page app with no real routing
(one route, `/`). Owns the top-level "not in a session yet" vs "in a live
session" state and wires the Daily call object provider around the session
view.

## Key files
- `frontend/src/app/page.tsx` — `Home`, the single page component.
- `frontend/src/app/layout.tsx` — `RootLayout`, page metadata.
- `frontend/src/app/globals.css` — see
  [frontend/styling-globals.md](styling-globals.md).

## Public surface
- `Home` (default export of `page.tsx`) — holds `session: SessionInfo |
  null`. When null, renders an "Enter Room" button that calls
  `startResumeRoomSession()`. When set, wraps
  `SessionView` in `<DailyProvider url token subscribeToTracksAutomatically>`
  from `@daily-co/daily-react`.
- `RootLayout` — sets `<html lang="en">`, imports `globals.css`, exports
  static `metadata` (title "AI Resume Expert").

## Data flow & dependencies
- Calls `startResumeRoomSession()` from
  [frontend/api-client.md](api-client.md) to get a `SessionInfo`.
- Renders `SessionView` from
  [frontend/components-resumeroom.md](components-resumeroom.md), passing
  `roomName` and an `onEnded` callback that clears `session` back to `null`
  (returning to the entry button).
- `DailyProvider` (from `@daily-co/daily-react`) is what actually creates
  the underlying Daily call object from `session.roomUrl` +
  `session.token` — everything under it (including all of
  `components-resumeroom.md`) depends on being inside this provider.

## Conventions & gotchas
- `inFlight` ref guards `enterRoom` against double-invocation (e.g. a fast
  double-click) — `starting` state alone isn't enough because it's set
  after the guard check, not before.
- There is intentionally no client-side router/route table — this is a
  single full-screen experience gated entirely by local component state,
  not URL state. Adding a second route means introducing real App Router
  segments, not extending this file's state machine.

## Last synced
2026-09-03
