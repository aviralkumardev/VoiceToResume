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
  `resume_room_tts_provider`, `resume_room_extraction_provider`,
  `resume_room_final_pass_provider`, `resume_room_completeness_provider`,
  `resume_room_question_provider`) are string keys looked up in per-role
  builder dicts — see
  [backend/stt-tts-pipeline.md](stt-tts-pipeline.md) and
  [backend/llm-providers.md](llm-providers.md). Adding a new provider means
  registering a builder there, not just changing this string. There is no
  `resume_room_llm_provider`/`_model`/`_reply_tokens` any more — those
  configured the persona/chat LLM, which was removed entirely; every word the
  bot says now is either a fixed literal (`GREETING_MESSAGE`/
  `CLOSING_MESSAGE`) or worded by the completeness pipeline's own LLM chains,
  which already had their own settings.
- `resume_room_silence_hardbound_seconds` (currently `2.0`) and
  `resume_room_completeness_model`/`resume_room_completeness_max_tokens`
  tune the silence-triggered completeness pipeline — see
  [backend/completeness-pipeline.md](completeness-pipeline.md).
- `resume_room_answer_silence_seconds` (currently `2.0`) is the *separate*
  silence window that ends a candidate's answer during interview mode.
  Two separate settings on purpose even though they currently coincide:
  `resume_room_silence_hardbound_seconds` is "you paused, let me re-grade
  in the background", `resume_room_answer_silence_seconds` is "you're
  actually finished answering" — see [backend/completeness-pipeline.md](completeness-pipeline.md).
  Both were at one point tuned all the way down to `0.0`/`0.0` (from an
  original `2.0`/`3.0`) chasing turn-taking latency — this doc had drifted
  and still claimed a `1.0`/`1.0` default at the time. `0.0` meant no
  debounce at all: the VAD's very first silence tick was treated as
  end-of-turn, cutting answers off mid-sentence (a live opening answer was
  truncated mid-word) well before the candidate actually finished. Now
  `2.0`/`2.0`.
- `resume_room_max_questions_per_round` (default `2`, renamed from
  `resume_room_max_probes_per_target`) caps how many exchanges one round can
  hold — the opening question plus any probes — before the interview
  director gives up on that subject and moves on regardless of grade. It's
  stamped onto each round as `max_questions` at `start_round` time; changing
  the setting mid-session only affects rounds opened afterwards. This is a
  deadlock guard, not a tuning knob: a forced conflict/unresolved topic
  outranks every other question, so an unsettleable one would otherwise
  stall the whole interview. **If any deployment `.env` overrides the old
  `RESUME_ROOM_MAX_PROBES_PER_TARGET` env var name, it will silently stop
  applying** — update it to `RESUME_ROOM_MAX_QUESTIONS_PER_ROUND`.
- **Deleted entirely** (fed only the removed target-selection/shortlist/
  claim machinery — see
  [backend/completeness-pipeline.md](completeness-pipeline.md) and
  [backend/stt-tts-pipeline.md](stt-tts-pipeline.md) for the round-based
  replacement): `resume_room_speculative_next_question_enabled`,
  `resume_room_next_target_shortlist_size`,
  `resume_room_next_question_transcript_lines`,
  `resume_room_dedup_candidate_targets`, and the earlier
  `resume_room_claim_*` knobs. The interview director's one per-turn LLM
  call (`question_chain.run_question_chain`) reasons over the whole
  resume/rubric/conversation directly — there is no backend-built
  shortlist, no recent-transcript window, no `also_covered` menu, and no
  claim-verification timer/grace-window/poll-loop left to tune.
- `resume_room_min_evidence_tokens` (default `3`) is still used, but by a
  different consumer now: it's the minimum token-overlap `merge.py`'s
  `is_redundant_with_accepted_update` requires before dropping an
  `unresolved` entry as redundant with an already-accepted extraction
  update — see [backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md).
  It no longer has anything to do with grading `also_covered` evidence
  spans, which no longer exist.
  `resume_room_flush_timeout_seconds` (default `8.0`) bounds how long
  `ResumeRoomOrchestrator.flush_transcript` will wait for a forced,
  out-of-turn extraction batch — used by `room_orchestrator.flush_transcript`
  itself and now also by the interview director's required-coverage safety
  net (`_await_task_a_settle`, which bounds both the flush wait and the
  follow-up `run_completeness_grading_cycle` call with this same timeout) —
  see [backend/room-orchestration.md](room-orchestration.md) and
  [backend/stt-tts-pipeline.md](stt-tts-pipeline.md).
  `question_chain.py`'s two chains (`run_question_chain`,
  `run_topic_question_chain`) now have their **own** settings —
  `resume_room_question_provider` (default `"openai"`),
  `resume_room_question_model` (default `"gpt-5.6-terra"`),
  `resume_room_question_max_tokens` (default `3000`) — rather than reusing
  `resume_room_completeness_provider`/`_model`/`_max_tokens` the way
  `run_answer_evaluation_chain` (now deleted) used to. `OpenAIProvider` sends
  no `reasoning.effort` (left at the model's own default) — there's no
  `resume_room_question_reasoning_effort` setting. This is the one place
  in the interview pipeline that talks to OpenAI directly instead of through
  OpenRouter; see [backend/llm-providers.md](llm-providers.md)'s
  `OpenAIProvider` section and
  [backend/completeness-pipeline.md](completeness-pipeline.md) for why only
  these two chains moved. `run_completeness_chain` remains the batched
  silence-worker's whole-resume grading call only, unchanged (still
  OpenRouter, via `resume_room_completeness_*`).
- CORS `allow_origins` is a fixed list, not env-driven — a genuine gap if
  this ever needs a second allowed origin.

## Last synced
2026-09-05 (later still — added `resume_room_question_provider` (default
`"openai"`), `resume_room_question_model` (default `"gpt-5.6-terra"`),
`resume_room_question_max_tokens` (default `3000`). `question_chain.py` no
longer reuses `resume_room_completeness_*` — see
[backend/llm-providers.md](llm-providers.md)'s new `OpenAIProvider`. No
`resume_room_question_reasoning_effort` setting — `OpenAIProvider` doesn't
set `reasoning.effort` at all, deliberately left at the model default.)
2026-09-05 (renamed `resume_room_max_probes_per_target` →
`resume_room_max_questions_per_round` (same default `2`, now a per-round
budget instead of per-target-path); deleted
`resume_room_dedup_candidate_targets`,
`resume_room_next_target_shortlist_size`,
`resume_room_next_question_transcript_lines` — all fed the removed
target-selection/shortlist/`also_covered` machinery, replaced by one LLM
call that reasons over the whole resume/rubric/conversation directly with
no backend-built menu of any kind. `resume_room_min_evidence_tokens` is kept
but now serves a different consumer (`merge.py`'s
`is_redundant_with_accepted_update`), not answer-grading evidence spans.
See [backend/completeness-pipeline.md](completeness-pipeline.md) and
[backend/stt-tts-pipeline.md](stt-tts-pipeline.md) for the round-based
redesign this supports.)
2026-09-04 (removed `resume_room_speculative_next_question_enabled` —
answer-grading and next-question wording were fused into ONE LLM call, so
the speculative guess-and-discard optimization it gated no longer exists.
Added `resume_room_next_target_shortlist_size` (default `3`) and
`resume_room_next_question_transcript_lines` (default `6`) for the fused
call's backend-built shortlist and recent-transcript context. Also noted
`run_completeness_chain` is now narrowed to the batched whole-resume grading
call only, no question-wording responsibility left. See
[backend/completeness-pipeline.md](completeness-pipeline.md)'s "Fused
answer-grading + next-question".)
2026-09-04 (corrected `resume_room_silence_hardbound_seconds`/
`resume_room_answer_silence_seconds` to their actual live values (`2.0`/
`2.0`) after finding they'd drifted to `0.0`/`0.0` against this doc's stale
`1.0`/`1.0` claim — see the bullets above and
[backend/completeness-pipeline.md](completeness-pipeline.md)'s "The wait is
unconditional". Also added `resume_room_speculative_next_question_enabled` — the
interview director's speculative-parallel next-question wording, see
[backend/completeness-pipeline.md](completeness-pipeline.md). Also removed
`resume_room_llm_provider`/`resume_room_model`/`resume_room_reply_tokens` —
the persona/chat LLM they configured was deleted entirely; see
[backend/stt-tts-pipeline.md](stt-tts-pipeline.md). Also reflects the
earlier deleted resume_room_claim_* settings and the per-thread probe
budget. Corrected `resume_room_silence_hardbound_seconds`/
`resume_room_answer_silence_seconds` defaults to `1.0`/`1.0` — the doc had
drifted from an earlier code change that tuned both down from 2.0/3.0)
