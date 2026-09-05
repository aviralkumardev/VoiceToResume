# VoiceToResume — Codebase Map

## Project overview
VoiceToResume ("AI Resume Expert" / "Meeting Room") is a live voice-based
mock-interview tool: a candidate joins a Daily.co video room with an AI
voice bot that walks them through describing their resume out loud, while
two background pipelines run off that conversation via LLM calls — one
incrementally extracts a structured resume document from the transcript,
the other grades how complete that resume is against a coverage rubric
every time the candidate goes silent. Backend: Python/FastAPI +
pipecat (voice pipeline) + Daily.co (WebRTC) + OpenRouter/OpenAI/Sarvam (LLM/
STT/TTS). Frontend: Next.js (App Router) + React + `@daily-co/daily-react` +
Tailwind v4. Entry points: `backend/app/main.py` (FastAPI app) and
`frontend/src/app/page.tsx` (the one page).

## High-level architecture
```
frontend (Next.js)                    backend (FastAPI)
┌─────────────────────┐   HTTP        ┌───────────────────────────┐
│ page.tsx             │ ───start───▶  │ routes.py                 │
│  └ SessionView        │              │  └ room_orchestrator.py    │
│     ├ AgentTile/      │ ◀─Daily App  │      ├─ daily/ (Daily API) │
│     │  HumanTile      │   Messages   │      ├─ stt_tts_pipeline/  │
│     └ SessionControls │              │      │   (voice bot)       │
└─────────────────────┘               │      ├─ resume_analysis_   │
                                        │      │   pipeline/         │
                                        │      │   (extraction +     │
                                        │      │    completeness)    │
                                        │      └─ data/ (in-mem CRUD)│
                                        │  services/llm_providers/  │
                                        │   (OpenRouter abstraction) │
                                        └───────────────────────────┘
```
There is no persona/chat LLM: the candidate's speech flows Daily transport →
STT → `InterviewDirector` → TTS → back to Daily, where `InterviewDirector`
picks fixed or LLM-worded question text but never free-generates a reply.
The session opens on a fixed greeting/opening question and closes on a fixed
closing line once every gap is covered. Every candidate transcript line is
also queued into the resume-analysis pipeline, which periodically calls an
LLM to merge structured updates into that session's resume document; every
VAD start/stop event drives the completeness pipeline, which — after 2s of
silence — grades that resume document against a coverage rubric, cancelling
the grading call outright if the candidate resumes speaking before it
finishes.

The whole session is **interview mode** — a round-based Q&A loop, not a
free-flowing chat: each round is spoken *straight through TTS* (never
interrupted by the candidate resuming speech), and once silence ends the
answer, a single LLM call grades it and freely drafts both a same-topic
probe and a genuinely different next question in one response — no
backend-side target selection. Two small deterministic guardrails sit on
top: an outstanding conflicting/ambiguous fact in the resume always jumps
the queue ahead of whatever question was organically drafted, and the
interview is never allowed to end while a required coverage block is still
missing, generating one more question for it instead. If the candidate
instead asks a process/doubt question about the interview itself, that's
detected on the same grading call, answered directly via TTS, and the
original pending question is re-spoken before waiting again.

## The Map

| File | Domain | Covers |
|---|---|---|
| backend/app-config.md | App entrypoint & config | FastAPI app factory, CORS, `Settings` (all env-driven config) |
| backend/api-routes.md | API routes | `/resume-room/start`, `/resume-room/stop/{room_name}` |
| backend/room-orchestration.md | Room orchestration | Session lifecycle, task/queue tracking, teardown |
| backend/external-daily.md | Daily.co integration | Room/token creation, native runtime init |
| backend/database-models.md | Session data / CRUD | In-memory session store, debug JSON export |
| backend/stt-tts-pipeline.md | Voice bot pipeline | pipecat STT→TTS pipeline (no chat LLM), caption/speaking bridges, InterviewDirector (opening/closing/meta-question handling) |
| backend/resume-analysis-pipeline.md | Resume analysis pipeline | Transcript batching, extraction/final-resolution LLM chains, merge logic |
| backend/completeness-pipeline.md | Completeness grading & interview mode | Coverage-rubric grading (batched sweep), fused per-answer LLM grading, forced-topic/required-gap guardrails |
| backend/llm-providers.md | LLM provider abstraction | Provider interface/factory, OpenRouter implementation, JSON schema validation |
| frontend/routing-app-shell.md | App shell & routing | Single-page app state, `DailyProvider` wiring |
| frontend/api-client.md | API client | `startResumeRoomSession`, `stopResumeRoomSession` |
| frontend/state-management.md | Session state & wire protocol | `SessionView`, `AppMessage` contract with backend |
| frontend/components-resumeroom.md | ResumeRoom UI components | Tiles, voice orb, captions, controls |
| frontend/hooks-lib.md | Shared hooks | `useAudioLevel`, `useLingeringCaption` |
| frontend/styling-globals.md | Styling & theme | Tailwind v4 setup, dark theme conventions |
| frontend/build-config.md | Build & project config | Next.js/TS config, scripts, path aliases |

`docs/` at the repo root holds phase-by-phase implementation write-ups
(`docs/extraction-implementation/`, `docs/openrouter-implementation/`) —
useful for *why* a design was chosen, but the `.claude/` domain files above
are the current-state source of truth; prefer them for "what exists now."

## Global conventions
- **Provider selection pattern**: every swappable external service (STT,
  TTS, chat LLM, extraction LLM, final-pass LLM) is chosen by a
  `settings.resume_room_*_provider` string key, dispatched through a small
  per-role `builders`/registry dict. Adding a provider means registering a
  builder, never adding conditional branches at call sites.
- **Fail-soft in the pipeline**: both the voice pipeline and the resume
  analysis pipeline swallow per-call exceptions and degrade gracefully
  (falling back to "no update", carrying text forward, logging and
  continuing) rather than crashing a live session. Preserve this posture
  when touching either pipeline.
- **No real database yet**: session state is in-process, in-memory,
  behind an `asyncio.Lock`, restated as a `Protocol` (`ResumeRoomCRUD`) so a
  real backing store can be swapped in later without touching callers.
- **Naming**: backend feature-specific settings are prefixed
  `resume_room_*`; frontend imports use the `@/*` → `src/*` path alias
  throughout.
- **Testing approach**: no automated test suite currently exists in this
  repo; verify pipeline changes by running the backend and frontend
  locally and exercising a session end-to-end.

## Lookup protocol
> When answering any question about this codebase: (1) check the Map
> above for the relevant domain file(s); (2) read only those file(s);
> (3) answer from their content; (4) open actual source files only if the
> docs don't cover the question or look stale — and if you do, update the
> doc afterward.

## Maintenance protocol
> Whenever a code change is made or observed: update the affected
> backend/frontend `.md` file(s) in the same turn — don't defer it. If the
> change adds, removes, or renames a domain, update the Map table in
> `CLAUDE.md` too.

## Standing behavioral rules
- Default to reading docs, not code. Fall back to source only when the Map
  has no matching entry, the matched file is silent on the question, or the
  file's "Last synced" marker looks older than the relevant code.
- Never let this file exceed 300 lines — if it's approaching the cap, move
  detail into a domain file and leave a pointer behind.
- File-per-domain, never file-per-source-file.
- Every task that changes code ends with a doc-sync step, not just a code
  diff — this is not optional cleanup, it's part of the task.
