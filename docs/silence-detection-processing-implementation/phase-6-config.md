# Phase 6 — Config Settings

## What this does

Adds the new settings this feature needs, following the exact
`resume_room_<feature>_*` naming convention `CLAUDE.md` calls out as a
project-wide rule — mirrors `resume_room_extraction_*`/
`resume_room_final_pass_*` exactly.

## File to modify: `backend/app/core/config.py`

Current file (for reference — this is what exists today):

```python
from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "Hiring Platform"

    openrouter_api_key: str
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_default_model: str = "openai/gpt-5.6-luna"
    default_temperature: float = 0.7
    llm_request_timeout_seconds: int = 120

    system_prompt_caching_enabled: bool = True
    max_json_repair_retries: int = 3
    max_rate_limit_retries: int = 3
    rate_limit_backoff_base_seconds: float = 1.0

    sarvam_api_key: str
    daily_api_key: str
    openai_api_key: str

    resume_room_daily_worker_threads: int = 2
    resume_room_expiry_seconds: int = 3600
    resume_room_max_session_seconds: int = 3600
    resume_room_bot_name: str = "AI Resume Expert"

    resume_room_stt_provider: str = "sarvam"
    resume_room_tts_provider: str = "sarvam"
    resume_room_llm_provider: str = "openai"
    resume_room_model: str = "gpt-4o-mini"
    resume_room_reply_tokens: int = 300

    resume_room_extraction_trigger_chars: int = 360
    resume_room_extraction_max_carry_multiple: int = 4
    resume_room_extraction_provider: str = "openrouter"
    resume_room_extraction_model: str = "openai/gpt-5.6-luna"
    resume_room_extraction_max_tokens: int = 1200

    resume_room_final_pass_provider: str = "openrouter"
    resume_room_final_pass_model: str = "openai/gpt-5.6-terra"
    resume_room_final_pass_max_tokens: int = 4000


    resume_room_sarvam_stt_model: str = "saaras:v3"
    resume_room_sarvam_tts_model: str = "bulbul:v3"
    resume_room_sarvam_tts_speaker: str = "shubh"
    resume_room_sarvam_language: str = "en-IN"

    resume_room_max_sessions: int = 3
    resume_room_max_participants_per_session: int = 2
    resume_room_empty_room_grace_seconds: int = 15
    resume_room_idle_timeout_seconds: int = 300

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
```

**Change 1** — new block right after `resume_room_final_pass_max_tokens`:

```python
    resume_room_final_pass_provider: str = "openrouter"
    resume_room_final_pass_model: str = "openai/gpt-5.6-terra"
    resume_room_final_pass_max_tokens: int = 4000

    resume_room_silence_hardbound_seconds: float = 2.0
    resume_room_completeness_provider: str = "openrouter"
    resume_room_completeness_model: str = "openai/gpt-5.6-terra"
    resume_room_completeness_max_tokens: int = 3000

```

## Key design points, explained

- **`resume_room_silence_hardbound_seconds` is a `float`**, not an `int` —
  the whole point of the setting is to be tunable during testing (a shorter
  hardbound like `0.5` is much easier to exercise interruption timing with
  manually than waiting out a real 2 seconds every time).
- **`resume_room_completeness_model` defaults to the same model as
  `resume_room_final_pass_model`** (`"openai/gpt-5.6-terra"`) rather than
  the lighter extraction model — this is a starting guess, not a measured
  choice: completeness grading is a judgment call against a rubric, closer
  in kind to final-resolution's conflict-adjudication work than to
  extraction's structured-copy work. Cheapen it later if the smaller model
  turns out to grade well enough.
- **No new provider/model registration needed** — `resume_room_completeness_provider`
  reuses the same `"openrouter"` factory key already registered by the
  extraction/final-pass chains' import side-effects, so `phase-4`'s own
  `import app.services.llm_providers.openrouter_provider` is what actually
  matters at runtime, not anything here.
