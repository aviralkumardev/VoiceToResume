# Frontend: Shared Hooks

## Purpose
Small, reusable hooks that wrap Daily's audio-level observer into
component-friendly primitives — one ref-based (for 60fps rAF consumers),
one boolean-state based (for caption fade timing).

## Key files
- `frontend/src/lib/resumeroom/useAudioLevel.ts` — `useAudioLevel`.
- `frontend/src/lib/resumeroom/useLingeringCaption.ts` —
  `useLingeringCaption`.

## Public surface
- `useAudioLevel(sessionId, {decay=0.85, interval}): RefObject<number>` —
  subscribes to `useAudioLevelObserver` for `sessionId`, smooths it
  (instant attack, exponential decay by `decay` per callback tick), and
  exposes the level as a **ref**, not state — intended for callers that
  sample it every animation frame (e.g. `AgentOrbVisual`) without forcing a
  React re-render on every audio tick.
- `useLingeringCaption(key: string | null, hold = false): boolean` —
  returns whether a caption should still be visible. `key` must change on
  every caption update (e.g. `${id}:${text}`) to restart the
  `CAPTION_LINGER_MS` (4000ms) fade timer. `hold=true` suspends the timer
  entirely (used for the agent tile while `speaking` is true).

## Data flow & dependencies
- Both wrap `@daily-co/daily-react`'s `useAudioLevelObserver` /
  are consumed by
  [frontend/components-resumeroom.md](components-resumeroom.md)
  (`AgentOrb` uses `useAudioLevel`; `AgentTile`/`HumanTile` use
  `useLingeringCaption`).

## Conventions & gotchas
- `useLingeringCaption`'s visibility is *derived* (`key !== null && key !==
  fadedKey`), not set eagerly to `true` inside the effect — doing the
  latter trips `react-hooks/set-state-in-effect`. Keep this derived-state
  pattern if modifying the hook.
- `useAudioLevel` logs every volume sample via `console.log("[ResumeRoom]
  audio level", ...)` — intentional debug instrumentation still present;
  same caveat as the debug logs in
  [frontend/state-management.md](state-management.md).
- The attack/decay smoothing formula here is duplicated (not shared) in
  `HumanTile`'s inline `MicLevelRing` — see
  [frontend/components-resumeroom.md](components-resumeroom.md)'s gotchas
  for why.

## Last synced
2026-09-03
