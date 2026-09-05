# Frontend: Styling & Global Theme

## Purpose
Global CSS baseline and Tailwind v4 setup. There is no separate design-token
file — the theme is dark, fixed, and expressed largely as inline Tailwind
utility classes in each component rather than centralized tokens.

## Key files
- `frontend/src/app/globals.css` — Tailwind v4 entry (`@import "tailwindcss"`
  or equivalent) plus any global resets.
- `frontend/postcss.config.mjs` — PostCSS pipeline wiring
  `@tailwindcss/postcss`.
- `frontend/eslint.config.mjs` — lint rules (flat config, `eslint-config-next`).

## Public surface
- No exported tokens/theme object — styling is inline Tailwind utility
  classes per component (e.g. `SHELL = "min-h-screen bg-[#0a0a0b]
  text-neutral-100"` in `page.tsx`).
- The one shared visual primitive is the `--orb` CSS custom property (set
  where the tiles are laid out) that both `AgentTile`'s orb and
  `HumanTile`'s avatar/ring size themselves against — see
  [frontend/components-resumeroom.md](components-resumeroom.md).

## Data flow & dependencies
- Tailwind v4 via the `@tailwindcss/postcss` PostCSS plugin — no
  `tailwind.config.js`; v4 configures via CSS (`@theme` etc.) inside
  `globals.css` itself if customized.
- `globals.css` is imported once, in `layout.tsx`
  ([frontend/routing-app-shell.md](routing-app-shell.md)).

## Conventions & gotchas
- Dark theme only, hardcoded hex/neutral values (`#0a0a0b`, `#141416`) —
  there is no light-mode variant or theme switcher to preserve.
- Color-coding convention (blue=agent, emerald=human, red=danger/mute,
  amber=connecting) is established per-component, not via shared token
  classes — grep for the hex/Tailwind color name across
  `components/ResumeRoom/` before changing one, since it's not centralized.

## Last synced
2026-09-03
