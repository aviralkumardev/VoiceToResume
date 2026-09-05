# Backend: LLM Provider Abstraction

## Purpose
Provider-agnostic interface for calling an LLM and getting back either free
text or schema-validated JSON, with normalized error types, usage/cost
accounting, and a JSON-repair retry loop. Two concrete providers exist —
`OpenRouterProvider` (Chat Completions, OpenAI-API-compatible, used by
extraction/final-pass/completeness) and `OpenAIProvider` (OpenAI's own
Responses API, used only by `question_chain.py`'s answer-grading/
question-generation chains) — every other caller in the codebase depends
only on the abstract interface.

## Key files
- `backend/app/services/llm_providers/provider_interface.py` —
  `LLMProvider` ABC (`generate_response`).
- `backend/app/services/llm_providers/provider_capabilities.py` — optional
  capability protocols (`JSONGenerating`, `PromptCaching`).
- `backend/app/services/llm_providers/provider_factory.py` —
  `LLMProviderFactory` registry.
- `backend/app/services/llm_providers/data_models.py` — `LLMMessage`,
  `LLMRequestOptions`, `LLMResponse` (all frozen dataclasses).
- `backend/app/services/llm_providers/provider_exceptions.py` — the shared
  `LLMProviderError` exception hierarchy every provider must raise into.
- `backend/app/services/llm_providers/json_schema_validation.py` —
  `SchemaSpec`, `build_response_format()`, `validate_against_schema()` —
  **Chat-Completions-shaped** (`response_format: {type, json_schema}`), used
  as-is only by `OpenRouterProvider`; `OpenAIProvider` builds its own
  Responses-shaped `text.format` dict directly from a `SchemaSpec` instead of
  calling `build_response_format()`.
- `backend/app/services/llm_providers/openai_sdk_errors.py` —
  `translate_openai_sdk_error()` (+ its temperature/structured-output
  rejection heuristics). Shared by both providers since they both wrap the
  same `openai` Python SDK and hit the same exception types regardless of
  `base_url`. `openrouter_provider/errors.py` is now a thin re-export of this
  module (kept so `from .errors import translate_openai_sdk_error` inside
  `openrouter_provider/provider.py` still works) — do not add new logic
  there, add it here.
- `backend/app/services/llm_providers/openrouter_provider/` —
  `provider.py` (`OpenRouterProvider`), `errors.py` (re-exports
  `translate_openai_sdk_error` from `openai_sdk_errors.py`), `__init__.py`
  (self-registers `"openrouter"` on import).
- `backend/app/services/llm_providers/openai_provider/` —
  `provider.py` (`OpenAIProvider`), `__init__.py` (self-registers
  `"openai"` on import). Talks to `client.responses.create` directly
  (`AsyncOpenAI(api_key=settings.openai_api_key)`, no `base_url` override) —
  OpenAI's own recommended entry point for new integrations over Chat
  Completions. Differences from `OpenRouterProvider` worth knowing:
  - **No `temperature` is ever sent** — current OpenAI guidance for
    reasoning-tier models is to omit it rather than send-then-retry on
    rejection, so there's no temperature-drop-and-retry branch here.
  - **`cost` is always `None`** — OpenAI's own API doesn't report a
    per-call cost figure the way OpenRouter's `usage.cost` extension does.
  - **No `reasoning.effort` is ever sent** — left at the model's own
    default rather than configured; there is no
    `resume_room_question_reasoning_effort` setting.
  - `store: False` is always sent on every request — interview
    transcripts/resume data are candidate PII, so responses aren't retained
    server-side beyond what's needed to serve them.
  - `supports_prompt_caching = True`, but unlike OpenRouter's explicit
    per-message `prompt_cache_breakpoint` marker, Responses API caching is
    automatic prefix-matching — nothing to mark per call.
  - Usage field names differ at the SDK level: `usage.input_tokens`/
    `output_tokens`/`total_tokens` (+ `input_tokens_details.cached_tokens`,
    `output_tokens_details.reasoning_tokens`) rather than Chat Completions'
    `prompt_tokens`/`completion_tokens` — `_parse_raw_response_to_llm_response`
    maps these into the same `LLMResponse` fields either provider uses.

## Public surface
- `LLMProviderFactory.create(provider_name, **kwargs) -> LLMProvider` —
  the only way callers should construct a provider. Raises `ValueError`
  listing `registered_provider_names()` on an unknown name.
- `LLMProviderFactory.register(provider_name, constructor)` — how a new
  provider module plugs in; call it at import time (see
  `openrouter_provider/__init__.py`) so importing the package is what
  registers it.
- `LLMProvider.generate_response(messages, options) -> LLMResponse` —
  abstract; every provider must implement plain chat completion.
- `OpenRouterProvider.generate_json(messages, schema, options,
  max_repair_retries=None) -> (dict, LLMResponse)` — the method most
  callers actually use (not part of the base `LLMProvider` ABC, but present
  via the `JSONGenerating` protocol — check with `isinstance(provider,
  JSONGenerating)` if writing provider-agnostic code that needs it).
  Attempts strict `json_schema` mode first, downgrades to `json_object`
  mode once if the model rejects structured outputs, then repairs invalid
  JSON by feeding the validation errors back to the model up to
  `max_json_repair_retries` times. Returned `LLMResponse` sums token/cost
  across every attempt.
- `SchemaSpec.from_dict(schema, *, name, strict=True)` /
  `.from_pydantic(model, ...)` — how callers describe the expected JSON
  shape (see [backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md)
  for real usage).
- Exception hierarchy (all subclass `LLMProviderError`):
  `LLMRateLimitError`, `LLMInvalidRequestError` (+
  `LLMTemperatureUnsupportedError`, `LLMStructuredOutputUnsupportedError`),
  `LLMProviderUnavailableError`, `LLMAuthenticationError`,
  `LLMSchemaValidationError` (carries `last_raw_text`,
  `validation_errors`, `last_response`), `LLMResponseParsingError`,
  `LLMTimeoutError`. Callers only need to catch `LLMProviderError` to be
  safe against all of them.

## Data flow & dependencies
- `OpenRouterProvider` wraps the `openai` Python SDK pointed at
  `settings.openrouter_base_url`, since OpenRouter is OpenAI-API-compatible.
  Reads `settings.openrouter_api_key`, `openrouter_default_model`,
  `default_temperature`, `llm_request_timeout_seconds`,
  `system_prompt_caching_enabled`, `max_json_repair_retries`,
  `max_rate_limit_retries`, `rate_limit_backoff_base_seconds` — see
  [backend/app-config.md](app-config.md).
- `OpenAIProvider` wraps the same `openai` Python SDK with no `base_url`
  override (hits `api.openai.com` directly). Reads `settings.openai_api_key`,
  `resume_room_question_model`, `llm_request_timeout_seconds`,
  `max_json_repair_retries`, `max_rate_limit_retries`,
  `rate_limit_backoff_base_seconds` — no `default_temperature` (never sends
  one), no `reasoning.effort` (left at the model default — no
  `resume_room_question_reasoning_effort` setting), and no
  `system_prompt_caching_enabled` (Responses API caching is automatic, not
  opt-in per call).
- `openai_sdk_errors.py`'s `translate_openai_sdk_error` is the single place
  that maps raw `openai.*` SDK exceptions to this module's exception
  hierarchy for **both** providers — anything unrecognized falls through to
  a generic `LLMProviderError`.
- Consumed by [backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md)'s
  `analysis_chain.py` and [backend/completeness-pipeline.md](completeness-pipeline.md)'s
  `completeness_chain.py` (both OpenRouter-only, via their own
  `(provider_name, model)`-keyed caches) and `question_chain.py` (OpenAI by
  default, its own separate cache) — see
  [backend/completeness-pipeline.md](completeness-pipeline.md)'s "The
  interview loop" for what `question_chain.py` actually does.

## Conventions & gotchas
- Adding a new provider: implement `LLMProvider` (+ `generate_json` if
  structured output is needed), register it in a package `__init__.py` the
  same way `openrouter_provider/__init__.py`/`openai_provider/__init__.py`
  do, then import that package somewhere it's guaranteed to run before first
  use (each chain module imports the provider package(s) its own setting
  might name for this registration side effect — e.g. `question_chain.py`
  imports both `openai_provider` and `openrouter_provider` so
  `resume_room_question_provider` can be switched between them without an
  import-order surprise).
- `_is_temperature_rejection` / `_is_structured_output_rejection` in
  `errors.py` are **heuristics** based on error message/param inspection —
  OpenRouter/upstream providers don't expose a single stable error code for
  either condition. If a new model/provider combination isn't classified
  correctly, tighten the heuristic in `errors.py` only — nothing outside
  that file should need to change.
- Temperature auto-drop is **`OpenRouterProvider`-only**:
  `_call_chat_completions_async` strips `temperature` and retries once,
  in-place, if the model rejects it (reasoning-tier models like OpenAI's
  o-series/`gpt-5.6-luna` only support the default) — this does not consume
  a JSON-repair retry. `OpenAIProvider` never sends `temperature` at all, so
  it has no equivalent retry branch to worry about.
- `generate_json`'s repair loop and rate-limit retry loop are independent —
  a rate limit inside a repair attempt still goes through the rate-limit
  backoff (`_call_chat_completions_async`/`_call_responses_async`) without
  consuming a repair attempt.
- `system_prompt_caching_enabled` (or the per-call
  `options.cache_system_prompt` override) marks only the **first** system
  message with an explicit `prompt_cache_breakpoint` — additional system
  messages are never marked. **This is `OpenRouterProvider`-only.**
  `_format_messages_for_api` applies the cache breakpoint automatically
  whenever `options.cache_system_prompt` is `None` (the default
  `LLMRequestOptions()` value), falling back to
  `settings.system_prompt_caching_enabled` (default `True`, not overridden
  in `.env`). None of the OpenRouter-routed chain call sites
  (`run_resume_extraction_chain`, `run_resume_final_resolution_chain`,
  `run_completeness_chain`) set `cache_system_prompt` on their
  `LLMRequestOptions`, so all of them already get the default (on) behavior,
  and each sends exactly one system message per call — nothing to add here
  unless a call site explicitly opts out. `run_question_chain`/
  `run_topic_question_chain` now go through `OpenAIProvider` instead, which
  has no cache-breakpoint marking of its own — Responses API prompt caching
  is automatic prefix-matching, not something a per-call flag turns on.

## Last synced
2026-09-05 (later still — dropped `reasoning.effort` from `OpenAIProvider`
entirely; it never sets `reasoning` on the request now, and there's no
`resume_room_question_reasoning_effort` setting.)
2026-09-05 (added `OpenAIProvider` — `openai_provider/` package, wraps
`client.responses.create` directly for `question_chain.py`'s answer-grading/
question-generation chains only; extraction/final-pass/completeness stay on
`OpenRouterProvider`. Relocated `translate_openai_sdk_error` out of
`openrouter_provider/errors.py` into a new shared `openai_sdk_errors.py` so
both providers use it without duplication; `openrouter_provider/errors.py`
is now a thin re-export. See [backend/app-config.md](app-config.md) for the
new `resume_room_question_*` settings and
[backend/completeness-pipeline.md](completeness-pipeline.md) for why only
`question_chain.py` moved.)
2026-09-04 (confirmed system-prompt caching is already on by default for
every OpenRouter-routed call site — no per-call opt-in needed)
