# Frontend: Session State & Wire Protocol

## Purpose
`SessionView` is the stateful heart of the frontend: it owns the Daily
meeting-state watchers, the app-message listener that turns backend events
into transcript/speaking/agent-ready state, and the session's elapsed-time
clock. This file documents that state machine and the `AppMessage` wire
protocol it consumes — the shared contract with the backend's pipeline
bridges.

## Key files
- `frontend/src/components/ResumeRoom/SessionView.tsx` — all the state and
  effects described below.
- `frontend/src/lib/resumeroom/types.ts` — `AppMessage`, `TranscriptMessage`,
  `SpeakerId`, `SessionInfo` type definitions (the wire contract itself).

## Public surface
- `SessionView({roomName, onEnded})` — renders the header (status dot +
  clock or "Joining…"/"Connecting…"), the two-tile stage
  ([frontend/components-resumeroom.md](components-resumeroom.md)), and
  `SessionControls` + `DailyAudio`.
- `AppMessage` (discriminated union, `type` field) — the entire backend→
  frontend event contract, sent via Daily app messages:
  - `{type: "transcript", speaker, text, turn, replace}` — `replace: true`
    means `text` is the speaker's full cumulative turn so far (candidate's
    live STT); `replace: false` means `text` is a fragment to append (agent's
    per-word TTS drip, see
    [backend/stt-tts-pipeline.md](../backend/stt-tts-pipeline.md)).
  - `{type: "speaking", speaker, value}` — bot start/stop-speaking toggle.
  - `{type: "agent-ready", participantId}` — sent once, when the bot has
    joined and queued its greeting.
- `TranscriptMessage {id, speaker, text, turn}` — one open line per
  speaking turn, stored in `SessionView`'s `transcript` array.

## Data flow & dependencies
- Consumes `useAppMessage` from `@daily-co/daily-react` as the sole input
  channel for `AppMessage`s — the backend side that emits these is
  documented in
  [backend/stt-tts-pipeline.md](../backend/stt-tts-pipeline.md) (`bridges.py`).
  Any change to the `AppMessage` shape must be made in both places at once.
  `personal note`: keep this file and `backend/stt-tts-pipeline.md` in sync
  whenever the wire shape changes.
- `useParticipantIds({sort: "joined_at"})` from Daily's own roster is the
  source of truth for who's in the room; `agentId` (set from
  `agent-ready`) is what distinguishes the bot's tile from the candidate's.
- Renders into `AgentTile`/`HumanTile`
  ([frontend/components-resumeroom.md](components-resumeroom.md)) by
  looking up each participant's `lastLineFor(id)`.
- `endSession()` calls the `onEnded` prop passed down from `Home`
  ([frontend/routing-app-shell.md](routing-app-shell.md)), which clears the
  session and returns to the entry screen.

## Conventions & gotchas
- `endSession` is guarded by `hasEndedRef` because it can be triggered from
  two independent places: `SessionControls`' explicit End Session click,
  and the `meetingState` watcher a moment later when `daily.leave()`
  actually resolves — without the guard, `onEnded` would fire twice.
- The `meetingState === "left-meeting" || meetingState === "error"` watcher
  is what catches **every** non-candidate-initiated way a session ends
  (bot sign-off, hard timeout, empty-room teardown, admin `/stop`) — Daily
  surfaces a room deleted out from under a still-joined client as the fatal
  `"error"` state, not `"left-meeting"`, so both must be handled by the same
  watcher. Don't split this into two separate effects.
- The elapsed-time clock starts on `agent-ready`, not on joining the Daily
  room — joining happens ~1s before the bot process is actually ready to
  speak, and the clock is meant to measure the conversation, not the
  connection.
- `AGENT_START_TIMEOUT_MS` (30s) exists so a bot that died on startup
  doesn't look identical to a slow one — without it the "Joining…"
  indicator would spin forever on a dead bot.
- Two `// eslint-disable-next-line no-console` debug logs remain in the
  `transcript`/`speaking` message handlers — intentional for now, remove or
  gate behind a debug flag if they become noise.

## Last synced
2026-09-03
