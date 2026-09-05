# OpenRouter LLM Provider — Implementation Guide

This is the master index for adding a SOLID-structured OpenRouter LLM provider to this backend. It explains the architecture and the reasoning behind every decision; the actual code to type into each file lives in the companion docs listed below. Nothing in `docs/` is executable — every file here is a specification for you to transcribe into the real source tree.

## Companion documents

| Doc | Destination file(s) it specifies |
|---|---|
| [`01-core-abstractions.md`](./01-core-abstractions.md) | `app/services/llm_providers/{__init__.py, provider_interface.py, provider_capabilities.py, data_models.py, provider_exceptions.py, json_schema_validation.py, provider_factory.py}` |
| [`02-openrouter-provider.md`](./02-openrouter-provider.md) | `app/services/llm_providers/openrouter_provider/{__init__.py, provider.py, errors.py}` |
| [`03-config-and-logging.md`](./03-config-and-logging.md) | `app/core/config.py`, `requirements.txt` |
| [`05-adding-a-new-provider.md`](./05-adding-a-new-provider.md) | Recipe for a future provider (e.g. native Anthropic) — no new code of its own |

## Why this exists

The starting point was a "legacy-style" `OpenAIProvider`: one concrete class, hardcoded to OpenAI's Responses API, with a hand-maintained per-model $/1K-token pricing table and no interface to swap in another backend. That shape works for a single hardcoded provider, but breaks down the moment you want a second one (Anthropic, Gemini, a self-hosted model) — every caller would need to know which concrete class it's holding.

This design fixes that by introducing a small, provider-agnostic contract (`LLMProvider` + a few narrow `Protocol`s) that `OpenRouterProvider` implements, and that any future provider implements the same way. Callers depend only on the abstraction; OpenRouter-specific details (message shape, error translation, prompt-caching mechanics) live entirely inside `app/services/llm_providers/openrouter_provider/`.

## Starting repo state

Before this work, the backend has:
- `app/core/config.py` — a 10-line `Settings` class with only `app_name` and `openrouter_api_key`.
- `app/services/llm_providers/openrouter_provider/` — an empty directory, already created in anticipation of this work.
- `requirements.txt` — lists a non-existent `openrouter` package (remove it — see `03-config-and-logging.md`).
- No provider interface, no factory, no JSON/caching utilities.

Everything described here is new.

## Key decisions and why

| Decision | Choice | Rationale |
|---|---|---|
| API surface | **Chat Completions** (`client.chat.completions.create`) | Broadest structured-output/model support across OpenRouter's catalog. A deliberate shape change from the legacy sample's `client.responses.create` — the Responses API is real on OpenRouter but narrower in provider/model coverage, especially for `response_format: json_schema`. |
| HTTP layer | **Official `openai` Python SDK**, pointed at `base_url="https://openrouter.ai/api/v1"` | This is OpenRouter's own documented recommendation for Python. It gives retries, timeouts, and typed exceptions for free, and `extra_headers`/`extra_body` cover every OpenRouter-specific field (prompt caching, provider routing, `usage.include`) as an open-ended escape hatch. Because all HTTP specifics stay isolated inside `app/services/llm_providers/openrouter_provider/provider.py`, swapping to raw `httpx` later — if the SDK ever genuinely can't express something OpenRouter needs — is a contained, low-risk change with no blast radius outside that one package. |
| Default model | `openai/gpt-5.6-luna` via `settings.openrouter_default_model` | Just a fallback default — every call can override it. Change the setting, not the code. |
| Cost accounting | Read the real `usage.cost` field from OpenRouter's response (`extra_body={"usage": {"include": True}}`) | OpenRouter serves hundreds of models at different prices; a hardcoded per-model pricing table (the legacy approach) is unmaintainable and unnecessary — OpenRouter already knows the real number. `LLMResponse.cost` is `Optional[float]` and is `None` if usage accounting wasn't available for some reason, rather than silently wrong. |
| Caching scope | **Only** provider-side system-prompt caching — no application-level response cache | An app-level cache (store-the-whole-response-and-skip-the-API-call) is a materially different, separable concern that was deliberately dropped from this pass. What's in scope is OpenRouter's own prompt-caching mechanism, applied specifically to the system message, because that's almost always the large, repeated, cacheable prefix in a chat request. |
| Interface style | `abc.ABC` for the minimal required contract (`LLMProvider`); `typing.Protocol` for optional capabilities (`provider_capabilities.py`) | `LLMProvider` is deliberately tiny (just `generate_response`) and uses nominal typing (real subclassing) because the factory does `isinstance` checks and should fail loudly at class-definition time if a required method is missing. Optional capabilities (JSON generation, prompt-caching support) use structural `Protocol`s instead, because not every provider will support all of them, and forcing a fat interface on every provider would violate Interface Segregation. |

## File tree (destination, not `docs/`)

```
backend/
├── app/
│   ├── __init__.py
│   ├── core/
│   │   ├── __init__.py
│   │   └── config.py                  # NEW
│   └── services/
│       ├── __init__.py
│       └── llm_providers/
│           ├── __init__.py            # NEW
│           ├── provider_interface.py  # NEW
│           ├── provider_capabilities.py # NEW
│           ├── data_models.py         # NEW
│           ├── provider_exceptions.py # NEW
│           ├── json_schema_validation.py # NEW
│           ├── provider_factory.py    # NEW
│           └── openrouter_provider/
│               ├── __init__.py        # NEW
│               ├── provider.py        # NEW
│               └── errors.py          # NEW
└── requirements.txt
```

## SOLID — where each principle actually lives

- **Single Responsibility.** `provider.py` only orchestrates HTTP calls and shapes responses. `json_schema_validation.py` only knows JSON Schema. `errors.py` only knows how to translate `openai` SDK exceptions. `provider_factory.py` only knows construction/registration. None of these files' jobs overlap — contrast with the legacy sample, where one class did all of this itself.
- **Open/Closed.** Adding a new provider means adding a new file and one `LLMProviderFactory.register(...)` call (see `05-adding-a-new-provider.md`) — zero edits to `provider_interface.py`, `provider_capabilities.py`, `data_models.py`, `provider_exceptions.py`, or `provider_factory.py`'s internals. Supporting a new OpenRouter model is a settings/argument change, never a code branch.
- **Liskov Substitution.** Every `LLMProvider` subclass returns an `LLMResponse` and raises only `LLMProviderError` subclasses. Anywhere code type-hints `LLMProvider`, `OpenRouterProvider` and any future provider are interchangeable.
- **Interface Segregation.** `provider_capabilities.py`'s `JSONGenerating` and `PromptCaching` are separate `Protocol`s. A consumer that only needs JSON generation type-hints `JSONGenerating`, not the concrete `OpenRouterProvider` — and a future minimal provider isn't forced to implement capabilities it doesn't support.
- **Dependency Inversion.** `OpenRouterProvider.__init__` takes injected `settings` and `async_client` (each falls back to a sensible default only if not supplied). Callers or higher-level services can inject custom settings or an existing `AsyncOpenAI` client instance rather than hardcoding dependencies inside `OpenRouterProvider`.

## Settings glossary (full list; see `03-config-and-logging.md` for the file)

| Field | Default | Purpose |
|---|---|---|
| `openrouter_api_key` | *(required, from `.env`)* | Bearer token for OpenRouter. |
| `openrouter_base_url` | `https://openrouter.ai/api/v1` | Passed as `base_url` to the OpenAI SDK client. |
| `openrouter_default_model` | `openai/gpt-5.6-luna` | Fallback model when no `model` is passed to `OpenRouterProvider(...)`. |
| `default_temperature` | `0.7` | Fallback temperature when no per-call/per-instance value is set. |
| `llm_request_timeout_seconds` | `120` | Timeout for the async client. |
| `http_referer` | `https://voicetoresume.app` | OpenRouter attribution header (`HTTP-Referer`). |
| `x_title` | `VoiceToResume` | OpenRouter attribution header (`X-Title`). |
| `system_prompt_caching_enabled` | `True` | Default for whether the system message gets a `prompt_cache_breakpoint` marker; overridable per call via `LLMRequestOptions.cache_system_prompt`. |
| `max_json_repair_retries` | `1` | How many repair round-trips `generate_json` will attempt before raising `LLMSchemaValidationError`. |
| `max_rate_limit_retries` | `3` | How many times a 429 is retried with backoff before `LLMRateLimitError` propagates. |
| `rate_limit_backoff_base_seconds` | `1.0` | Base for exponential backoff when OpenRouter doesn't send a `Retry-After` header. |

## Verification (run these yourself after transcribing the code)

1. Smoke test against the real API:
   ```python
   import asyncio
   import app.services.llm_providers.openrouter_provider  # noqa: F401 — registers "openrouter"
   from app.services.llm_providers.provider_factory import LLMProviderFactory
   from app.services.llm_providers.data_models import LLMMessage, Role

   async def main():
       provider = LLMProviderFactory.create("openrouter")
       response = await provider.generate_response([LLMMessage(role=Role.USER, content="Say hi in 5 words.")])
       print(response.text, response.cost)

   asyncio.run(main())
   ```
   Confirm `response.text` is non-empty and `response.cost` is a number.
2. Repeat with `generate_json` and a small pydantic schema against `settings.openrouter_default_model`, confirm the result validates and no repair round-trip was needed (inspect the returned result / execution to check).
3. Send a multi-turn request with a substantial system prompt against an Anthropic model routed through OpenRouter (e.g. `model="anthropic/claude-sonnet-4.5"`), and check the response for a cache-read signal on the second call — this spot-checks the "safe no-op on unsupported providers" assumption documented in `02-openrouter-provider.md`.
