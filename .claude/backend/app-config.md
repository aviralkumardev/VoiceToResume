# Backend: App Entrypoint & Config

## Purpose
FastAPI application bootstrap and all runtime configuration. This is where
the app object is created, CORS is set up, the single router is mounted, and
every tunable (API keys, model names, timeouts, session limits) is declared
as a typed setting read from `.env`.

## Key files
- `backend/app/main.py` — FastAPI app factory, CORS middleware, router
  mount, lifespan shutdown hook.
- `backend/app/core/config.py` — `Settings` (pydantic-settings) — the single
  source of truth for all config.
- `backend/.env` — actual secret values (not committed; see `.env` in repo
  for local dev keys).
- `backend/requirements.txt` — Python dependencies.

## Public surface
- `app.main.app` — the FastAPI instance, includes `resume_room_router` under
  no extra prefix (routes already prefix themselves, see
  [backend/api-routes.md](api-routes.md)).
- `app.core.config.settings` — module-level singleton `Settings()` instance;
  imported everywhere else in the backend as the config source.
- `Settings` fields of note: `openrouter_*` (LLM provider), `sarvam_api_key`,
  `daily_api_key`, `openai_api_key` (external services), `resume_room_*`
  (feature-specific tuning — session limits, provider choice per role,
  extraction batching thresholds).

## Data flow & dependencies
- `main.py` imports `get_orchestrator_instance` (see
  [backend/room-orchestration.md](room-orchestration.md)) only to call
  `.shutdown()` on app lifespan exit — it does not manage its lifecycle
  otherwise.
- `allowed_origins` is hardcoded to `http://localhost:3000` — the Next.js
  dev server. Update this for any non-localhost frontend deployment.
- Every other backend module reads `from app.core.config import settings`
  rather than constructing its own config.

## Conventions & gotchas
- `Settings.Config.env_file = ".env"`, `case_sensitive = False` — env var
  names are case-insensitive and map 1:1 to the lowercase field names above.
- Fields with no default (`openrouter_api_key`, `sarvam_api_key`,
  `daily_api_key`, `openai_api_key`) are **required** — app startup fails
  fast if `.env` is missing them.
- `resume_room_*_provider` settings (`resume_room_stt_provider`,
  `resume_room_tts_provider`, `resume_room_combined_provider`,
  `resume_room_final_pass_provider`, `resume_room_question_provider`) are
  string keys looked up in per-role builder dicts — see
  [backend/stt-tts-pipeline.md](stt-tts-pipeline.md) and
  [backend/llm-providers.md](llm-providers.md). Adding a new provider means
  registering a builder there, not just changing this string. There is no
  `resume_room_llm_provider`/`_model`/`_reply_tokens` any more — those
  configured the persona/chat LLM, which was removed entirely; every word
  the bot says now is either a fixed literal (`GREETING_MESSAGE`/
  `CLOSING_MESSAGE`) or worded by one of the resume-analysis pipeline's own
  LLM chains, each with their own settings.
- **The one background analysis LLM call (extraction + coverage grading +
  question-queue wording) is fully collapsed onto one config group:**
  `resume_room_combined_provider` (default `"openai"`),
  `resume_room_combined_model` (default `"gpt-5.6-terra"`),
  `resume_room_combined_max_tokens`, `resume_room_combined_reasoning_effort`
  (default `"none"`) — routed through `OpenAIProvider` (Responses API), not
  `OpenRouterProvider`. `resume_room_combined_reasoning_effort` is passed
  explicitly to `OpenAIProvider`'s constructor in
  `combined_chain._get_provider()` — deliberately not left to
  `OpenAIProvider`'s own fallback default (which otherwise reads
  `resume_room_question_reasoning_effort`), so this chain has an
  independent reasoning-effort knob rather than silently inheriting the
  narrow per-answer grading chain's. There are no separate
  `resume_room_extraction_*`/`resume_room_completeness_*` settings any
  more — both the dedicated extraction chain and the dedicated batched
  completeness-grading chain they used to configure are gone, fused into
  this one combined call. See
  [backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md) and
  [backend/completeness-pipeline.md](completeness-pipeline.md).
- `resume_room_silence_hardbound_seconds` and
  `resume_room_answer_silence_seconds` (check `config.py` for current
  values — under active live-tuning as of 2026-09-05) are the interview
  director's own two silence windows: `resume_room_silence_hardbound_seconds`
  is the idle-recovery debounce (nothing awaiting an answer),
  `resume_room_answer_silence_seconds` ends a candidate's actual answer —
  this is the number that matters for "did a normal thinking-pause
  mid-sentence get mistaken for the end of the answer." Two separate
  settings on purpose even though they're currently close in value. Both
  had drifted down to `0.1`/`1.0` chasing turn-taking latency and were
  reverted back up after a live report of exactly the failure mode this
  file's history already documents below (a real mid-sentence cutoff, not a
  logic bug — `InterviewDirector.on_speaking_change(True)` correctly cancels
  the pending debounce task the instant speech resumes, so the fix is
  purely the threshold, not the cancellation code). Note there is a
  **second**, currently-unconfigured silence threshold upstream of both of
  these: `pipeline.py`'s `SileroVADAnalyzer()` is constructed with no
  explicit `VADParams`, so pipecat's own default `stop_secs` (its own
  "how long a pause counts as stopped speaking," independent of and prior
  to either setting here) also contributes to the real end-to-end pause a
  candidate can take before `UserStoppedSpeakingFrame` even fires and either
  debounce above starts counting. If cutoffs recur even at generous values
  here, that VAD-level default is the next place to look — it isn't
  overridden anywhere in this codebase today. There is no separate
  silence-triggered completeness sweep to tune any more — see
  [backend/completeness-pipeline.md](completeness-pipeline.md).
- `resume_room_max_questions_per_round` (default `2`) caps how many
  exchanges one round can hold — the opening question plus any probes —
  before the interview director gives up on that subject and moves on
  regardless of grade. Stamped onto each round as `max_questions` at
  `start_round` time; changing the setting mid-session only affects rounds
  opened afterwards. This is a deadlock guard, not a tuning knob: a forced
  conflict/unresolved topic outranks every other question, so an
  unsettleable one would otherwise stall the whole interview.
- `question_chain.py`'s narrow per-answer grading chain
  (`run_answer_grading_chain`) has its **own** settings:
  `resume_room_question_provider` (default `"openai"`),
  `resume_room_question_model` (default `"gpt-5.6-terra"`),
  `resume_room_question_max_tokens` (default `3000`),
  `resume_room_question_reasoning_effort` (default `"none"`) — routed
  through `OpenAIProvider`, same as the combined call but as an
  independently-configured/independently-cached provider instance (each
  chain module keeps its own `_provider_cache` dict).
- `resume_room_final_pass_provider`/`_model`/`_max_tokens` — the
  session-end final-resolution pass, the **only** chain still on
  `OpenRouterProvider`.
- `resume_room_min_evidence_tokens` (default `3`) is the minimum
  token-overlap `merge.py`'s `is_redundant_with_accepted_update` requires
  before dropping an `unresolved` entry as redundant with an
  already-accepted extraction update.
- `resume_room_flush_timeout_seconds` (default `8.0`) bounds how long
  `ResumeRoomOrchestrator.flush_transcript` will wait for a forced,
  out-of-turn combined-analysis batch — used by
  `room_orchestrator.flush_transcript` itself.
- CORS `allow_origins` is a fixed list, not env-driven — a genuine gap if
  this ever needs a second allowed origin.

## Last synced
2026-09-05 (later still — live report of a mid-sentence answer cutoff
("only a small pause was there") traced to `resume_room_silence_hardbound_seconds`/
`resume_room_answer_silence_seconds` having drifted back down to `0.1`/`1.0`
— a milder recurrence of the exact `0.0`/`0.0` failure mode already
documented below. Reverted to `0.5`/`2.0` (short of the historical `2.0`/
`2.0` on the hardbound side, since that one only gates idle-recovery, not
answer-end). Confirmed this is a threshold issue, not a logic bug: the
cancel-on-resumed-speech path in `InterviewDirector.on_speaking_change`
already works correctly. Also flagged `pipeline.py`'s `SileroVADAnalyzer()`
as a second, currently-unconfigured silence threshold layered in front of
both settings here — see the bullet above.)
2026-09-05 (later same day — `resume_room_combined_provider`/`_model` moved
from `"openrouter"`/`"openai/gpt-5.6-terra:nitro"` to `"openai"`/
`"gpt-5.6-terra"`; added `resume_room_combined_reasoning_effort` (default
`"none"`). The combined analysis call (extraction + coverage grading +
question-queue wording) now runs through `OpenAIProvider`, same as the
narrow per-answer grading chain, but with its own independent
reasoning-effort setting rather than inheriting
`resume_room_question_reasoning_effort`. `resume_room_final_pass_*` remains
the only chain on `OpenRouterProvider`. See
[backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md).)
2026-09-05 (major rewrite — collapsed `resume_room_extraction_provider`/
`_model`/`_max_tokens` and `resume_room_completeness_provider`/`_model`/
`_max_tokens` into one `resume_room_combined_provider`/`_model`/
`_max_tokens` group, feeding the new combined analysis call
(`combined_chain.run_combined_chain`) that fuses extraction + completeness
grading + question-queue wording into one background LLM call. Also noted
`resume_room_silence_hardbound_seconds`/`resume_room_answer_silence_seconds`
no longer gate any separate batched-worker sweep, since that worker no
longer exists — both are purely the interview director's own two silence
windows now. See [backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md)
and [backend/completeness-pipeline.md](completeness-pipeline.md). Older
history predating this rewrite (settings renames/deletions from the earlier
per-target-selection/shortlist design) has been compressed out of this
file.)
