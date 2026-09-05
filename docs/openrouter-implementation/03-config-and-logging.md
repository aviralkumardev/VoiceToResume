# Config and Dependencies

## `app/core/config.py` (edit — full replacement content)

Keeps `app_name` and `openrouter_api_key` exactly as they are today; everything else is new, and every new field is defaulted so `.env` needs no changes beyond the existing `OPENROUTER_API_KEY`.

```python
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "Hiring Platform"

    # --- OpenRouter / LLM provider ---
    openrouter_api_key: str
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_default_model: str = "openai/gpt-5.6-luna"
    default_temperature: float = 0.7
    llm_request_timeout_seconds: int = 120
    http_referer: str = "https://voicetoresume.app"
    x_title: str = "VoiceToResume"

    # --- Provider-side (OpenRouter) system-prompt caching ---
    system_prompt_caching_enabled: bool = True

    # --- JSON schema / structured outputs ---
    max_json_repair_retries: int = 1

    # --- Retry/backoff ---
    max_rate_limit_retries: int = 3
    rate_limit_backoff_base_seconds: float = 1.0

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
```

See `implementation.md`'s settings glossary for what each field does and why its default was chosen.

---

## `requirements.txt` (edit — full replacement content)

```
fastapi
uvicorn[standard]
pydantic[email]
pydantic-settings
python-dotenv
openai
jsonschema
httpx
pipecat-ai[daily,openai,sarvam,silero]
```

Changes from what's there today:
- **Added** `openai` (the SDK `OpenRouterProvider` is built on) and `jsonschema` (local schema validation in `json_schema_validation.py`) as key dependencies for the provider layer.
- **Pinned `httpx` explicitly** — it was already present transitively (pulled in by `fastapi`/`uvicorn`), but the provider's retry/timeout behavior depends on it directly, so it shouldn't stay implicit.

---

Next: [`05-adding-a-new-provider.md`](./05-adding-a-new-provider.md).
