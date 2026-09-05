# Phase 7 — Config additions

## What this does

Adds the settings the earlier phases reference: the buffer trigger size, the
carry cap multiplier, and the extraction call's provider/model/token-limit.
Follows the existing `resume_room_*` naming convention already used
throughout this module.

**Extension**: adds a second, independent set of provider/model/token-limit
settings for the session-end final resolution pass (phases 4/5/6), so a
stronger or larger-context model can be used for that one-shot, whole-transcript
call without touching the per-batch extraction settings.

## File to modify: `app/core/config.py`

The existing `resume_room_*` settings block currently reads (confirmed by
reading the file):

```python
    resume_room_daily_worker_threads: int = 2
    resume_room_expiry_seconds: int = 3600
    resume_room_max_session_seconds: int = 3600
    resume_room_bot_name: str = "AI Resume Expert"

    resume_room_stt_provider: str = "sarvam"
    resume_room_tts_provider: str = "sarvam"
    resume_room_llm_provider: str = "openai"
    resume_room_model: str = "gpt-4o-mini"
    resume_room_reply_tokens: int = 300

    resume_room_sarvam_stt_model: str = "saaras:v3"
    resume_room_sarvam_tts_model: str = "bulbul:v3"
    resume_room_sarvam_tts_speaker: str = "shubh"
    resume_room_sarvam_language: str = "en-IN"

    resume_room_max_sessions: int = 3
    resume_room_max_participants_per_session: int = 2
    resume_room_empty_room_grace_seconds: int = 15
    resume_room_idle_timeout_seconds: int = 300
```

Add this new block immediately after `resume_room_reply_tokens` (i.e. between
the conversational-bot settings and the Sarvam-specific settings, since these
new settings belong to the *extraction* call, not the live conversational
bot):

```python
    resume_room_extraction_trigger_chars: int = 360
    resume_room_extraction_max_carry_multiple: int = 4
    resume_room_extraction_provider: str = "openrouter"
    resume_room_extraction_model: str = "anthropic/claude-sonnet-4.5"
    resume_room_extraction_max_tokens: int = 1200

    resume_room_final_pass_provider: str = "openrouter"
    resume_room_final_pass_model: str = "anthropic/claude-sonnet-4.5"
    resume_room_final_pass_max_tokens: int = 4000
```

## Key design points, explained

- **`resume_room_extraction_trigger_chars: 360`** — same default pitch_room
  uses for `pitch_room_extraction_trigger_chars`. A reasonable starting
  point; tune based on how chatty candidates tend to be per turn.
- **`resume_room_extraction_provider: "openrouter"`** — the only provider
  currently registered in `app/services/llm_providers/` (confirmed via
  `openrouter_provider/__init__.py`'s `LLMProviderFactory.register(...)`
  call). If a direct Anthropic/OpenAI provider gets added to that package
  later, this setting is the only place to change.
- **`resume_room_extraction_model: "anthropic/claude-sonnet-4.5"`** — an
  OpenRouter model slug. **Confirm the exact available slug on your
  OpenRouter account/plan before relying on this** — OpenRouter's model
  catalog and naming can change; check
  `settings.openrouter_default_model`'s current value and OpenRouter's model
  list as a sanity check before shipping.
- **No new "repair retries" setting** — phase 4's calls to
  `provider.generate_json()` default to the already-existing
  `settings.max_json_repair_retries` (confirmed at `config.py:16`, default
  `3`), the same setting every other JSON-generating call in this repo uses.
  Adding a separate
  `resume_room_extraction_repair_retries` would be an unnecessary knob for a
  behavior that should probably stay consistent across the whole app. This
  applies equally to the final-resolution pass — it reuses the same setting,
  no separate `resume_room_final_pass_repair_retries` is added.
- **`resume_room_final_pass_*` is a fully independent block**, defaulting to
  the same provider/model as extraction but with a much larger
  `max_tokens` (4000 vs 1200) — the final pass reads the *entire* candidate
  transcript plus the full resume state in one call, so its response can
  legitimately need to touch far more fields at once than a single ~360-char
  incremental batch ever would. Keeping it a separate setting (rather than
  reusing `resume_room_extraction_max_tokens`) means bumping the final pass's
  budget doesn't inflate the cost of every incremental batch.
