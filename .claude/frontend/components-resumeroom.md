# Frontend: ResumeRoom UI Components

## Purpose
The presentational layer of the meeting room: the two participant tiles
(agent + human), their live captions, the animated voice orb, and the
mic/end-session controls. All state driving these lives in
[frontend/state-management.md](state-management.md) — this domain is mostly
props-in, pixels-out.

## Key files
- `frontend/src/components/ResumeRoom/AgentTile.tsx` — bot's tile.
- `frontend/src/components/ResumeRoom/HumanTile.tsx` — candidate's tile.
- `frontend/src/components/ResumeRoom/AgentOrb.tsx` /
  `AgentOrbVisual.tsx` / `orbShader.ts` — the animated WebGL/canvas voice
  orb for the agent tile.
- `frontend/src/components/ResumeRoom/LiveCaption.tsx` — shared caption
  bubble used by both tiles.
- `frontend/src/components/ResumeRoom/SessionControls.tsx` — mic
  toggle + End Session button.
- `frontend/src/components/ResumeRoom/icons.tsx` — inline SVG icon set
  (`MicIcon`, `MicOffIcon`, `LeaveIcon`).

## Public surface
- `AgentTile({sessionId, speaking, caption, name})` — renders `AgentOrb` +
  `LiveCaption`, a "Speaking" badge while `speaking`, and the agent's name.
- `HumanTile({sessionId, caption})` — renders an initials avatar with a
  live `MicLevelRing` (driven by `useAudioLevelObserver`), a "Muted" badge,
  and `LiveCaption`. Distinguishes local vs. remote participant for the
  display name.
- `AgentOrb({sessionId, speaking})` — thin wrapper: reads a live audio
  level via `useAudioLevel` and hands a getter to `AgentOrbVisual`.
- `AgentOrbVisual({getLevel, speaking})` — the actual shader-driven orb
  render (see `orbShader.ts` for `ORB_FRAG`/`ORB_VERT`); tuned with named
  constants for attack/release smoothing, flow/spin/pulse rates, and a
  `SPEAKING_FLOOR` so the orb never looks fully inert mid-sentence.
- `LiveCaption({text, visible})` — fixed two-line scrolling caption window;
  bottom-anchored via explicit `scrollTop`, fades via `visible`.
- `SessionControls({roomName, onEnded})` — mic mute toggle (via
  `daily.setLocalAudio`) and End Session button (calls
  `stopResumeRoomSession` then `daily.leave()` then `onEnded`).
- Icons: `MicIcon`, `MicOffIcon`, `LeaveIcon` — presentational only.

## Data flow & dependencies
- Both tiles use `useLingeringCaption` from
  [frontend/hooks-lib.md](hooks-lib.md) to decide caption fade-out timing —
  `AgentTile` passes `speaking` as the `hold` argument so the agent's
  caption never fades mid-utterance; `HumanTile` doesn't hold (the
  candidate's mic-level ring already signals liveness).
- `AgentOrb`/`HumanTile`'s `MicLevelRing` both read Daily's
  `useAudioLevelObserver` — two independent smoothing implementations
  (`useAudioLevel` hook vs. inline `MicLevelRing` state) using the same
  attack-instant/decay-exponential formula; kept separate because the orb
  needs a ref for 60fps sampling while the ring re-renders via state.
- `SessionControls` calls into
  [frontend/api-client.md](api-client.md)'s `stopResumeRoomSession`.
- All tiles read/write Daily state via `@daily-co/daily-react` hooks
  (`useParticipantProperty`, `useLocalSessionId`, etc.) — always called
  from inside the `DailyProvider` set up in
  [frontend/routing-app-shell.md](routing-app-shell.md).

## Conventions & gotchas
- Color convention: **blue = agent**, **emerald = human** — consistent
  across the "Speaking" badges on both tiles; keep new speaking/state
  indicators consistent with this if added.
- `--orb` is a CSS custom property both tiles size their central
  avatar/orb and its surrounding ring against — change the orb's on-screen
  size there, not by editing pixel values in each component.
- `captionKey` (`${id}:${text}`) is duplicated identically in `AgentTile`
  and `HumanTile` rather than shared, so each restarts its own linger timer
  correctly — this is intentional per-instance hook usage (one legal hook
  call per component instance), not an oversight to dedupe carelessly.
- `orbShader.ts` constants (`LEVEL_ATTACK_TAU`, `LEVEL_RELEASE_TAU`,
  `FLOW_BASE/GAIN`, `SPIN_BASE/GAIN`, `PULSE_BASE/GAIN`, `MAX_DT`,
  `SPEAKING_FLOOR`) are tuned in **seconds**, not per-frame factors, so
  motion stays consistent across display refresh rates — don't convert
  these back to per-frame lerps.

## Last synced
2026-09-03
