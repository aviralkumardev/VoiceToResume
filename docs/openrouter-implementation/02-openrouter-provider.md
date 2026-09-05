# The OpenRouter Provider

Destination: `app/services/llm_providers/openrouter_provider/` (the directory already exists, empty). This package is the *only* place in the codebase that knows about the `openai` SDK, OpenRouter's HTTP field names, or OpenRouter's error shapes. Everything it imports from `app/services/llm_providers/` (the parent package documented in `01-core-abstractions.md`) is provider-agnostic.

Requires `openai`, `jsonschema` (see `03-config-and-logging.md` for `requirements.txt`).

---

## `app/services/llm_providers/openrouter_provider/errors.py`

```python
"""Translates openai-python SDK exceptions (raised by any call through
OpenRouterProvider's AsyncOpenAI client) into the provider-agnostic
provider_exceptions.py hierarchy. This is the ONLY file in this package that knows the
shape of openai SDK exceptions — keeping that knowledge out of provider.py
itself (Single Responsibility)."""
from __future__ import annotations

from typing import Any, Optional

import openai

from app.services.llm_providers.provider_exceptions import (
    LLMAuthenticationError,
    LLMInvalidRequestError,
    LLMProviderError,
    LLMProviderUnavailableError,
    LLMRateLimitError,
    LLMStructuredOutputUnsupportedError,
    LLMTemperatureUnsupportedError,
    LLMTimeoutError,
)

_STRUCTURED_OUTPUT_ERROR_MESSAGE_HINTS = ("response_format", "json_schema", "structured output", "structured_outputs")


def _extract_error_body(sdk_exception: "openai.APIStatusError") -> dict[str, Any]:
    raw_error_body = getattr(sdk_exception, "body", None) or {}
    if isinstance(raw_error_body, dict):
        nested_error_body = raw_error_body.get("error", raw_error_body)
        return nested_error_body if isinstance(nested_error_body, dict) else {}
    return {}


def _extract_retry_after_seconds(sdk_exception: "openai.APIStatusError") -> Optional[float]:
    http_response = getattr(sdk_exception, "response", None)
    retry_after_header_value = http_response.headers.get("retry-after") if http_response is not None else None
    if retry_after_header_value is None:
        return None
    try:
        return float(retry_after_header_value)
    except ValueError:
        return None


def _is_temperature_rejection(sdk_exception: "openai.BadRequestError", error_body: dict[str, Any]) -> bool:
    """True if this 400 is the model refusing a custom `temperature` value
    (reasoning-tier models such as OpenAI's o-series and gpt-5.6-luna only
    support the default temperature and reject the parameter outright).
    Ported from the legacy provider's `_rejects_temperature` free function."""
    error_param_name = getattr(sdk_exception, "param", None) or error_body.get("param")
    if error_param_name == "temperature":
        return True
    error_message_text = str(sdk_exception)
    return "temperature" in error_message_text and "not supported" in error_message_text


def _is_structured_output_rejection(sdk_exception: "openai.BadRequestError", error_body: dict[str, Any]) -> bool:
    """Heuristic — OpenRouter/the upstream provider doesn't document a single
    stable error code for 'this model doesn't support response_format:
    json_schema', so this inspects the `param` field and the error message
    for hints. If you find a model/provider combination this misses, tighten
    the heuristic here; nothing outside this file needs to change."""
    error_param_name = (getattr(sdk_exception, "param", None) or error_body.get("param") or "").lower()
    error_message_text = str(sdk_exception).lower()
    if "response_format" in error_param_name:
        return True
    return any(hint in error_message_text for hint in _STRUCTURED_OUTPUT_ERROR_MESSAGE_HINTS)


def translate_openai_sdk_error(sdk_exception: Exception) -> LLMProviderError:
    """Maps any exception raised by an openai SDK call to our shared
    provider_exceptions.py hierarchy. Unknown exception types are wrapped in a plain
    LLMProviderError rather than left to propagate, so calling code only
    ever needs to catch LLMProviderError and its subclasses."""
    if isinstance(sdk_exception, openai.RateLimitError):
        error_body = _extract_error_body(sdk_exception)
        error_metadata = error_body.get("metadata")
        upstream_provider_code = error_metadata.get("provider_code") if isinstance(error_metadata, dict) else None
        return LLMRateLimitError(
            str(sdk_exception),
            retry_after_seconds=_extract_retry_after_seconds(sdk_exception),
            provider_code=upstream_provider_code,
        )
    if isinstance(sdk_exception, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return LLMAuthenticationError(str(sdk_exception))
    if isinstance(sdk_exception, openai.BadRequestError):
        error_body = _extract_error_body(sdk_exception)
        if _is_temperature_rejection(sdk_exception, error_body):
            return LLMTemperatureUnsupportedError(str(sdk_exception))
        if _is_structured_output_rejection(sdk_exception, error_body):
            return LLMStructuredOutputUnsupportedError(str(sdk_exception))
        return LLMInvalidRequestError(str(sdk_exception))
    if isinstance(sdk_exception, openai.APITimeoutError):
        return LLMTimeoutError(str(sdk_exception))
    if isinstance(sdk_exception, openai.APIConnectionError):
        return LLMProviderUnavailableError(str(sdk_exception))
    if isinstance(sdk_exception, openai.APIStatusError):
        # Anything else with an HTTP status attached — openai's SDK collapses
        # every 5xx into InternalServerError, which we treat uniformly as
        # "provider unavailable, maybe retry later."
        return LLMProviderUnavailableError(str(sdk_exception))
    return LLMProviderError(str(sdk_exception))
```

---

## `app/services/llm_providers/openrouter_provider/provider.py`

```python
"""OpenRouterProvider — the concrete LLMProvider implementation targeting
OpenRouter's Chat Completions API via the official `openai` Python SDK
pointed at OpenRouter's base_url. See docs/02-openrouter-provider.md for the
full design rationale."""
from __future__ import annotations

import asyncio
import json
from typing import Any, Optional, Sequence

import openai
from openai import AsyncOpenAI

from app.core.config import Settings
from app.core.config import settings as global_settings
from app.services.llm_providers.provider_interface import LLMProvider
from app.services.llm_providers.provider_exceptions import (
    LLMRateLimitError,
    LLMResponseParsingError,
    LLMSchemaValidationError,
    LLMStructuredOutputUnsupportedError,
    LLMTemperatureUnsupportedError,
)
from app.services.llm_providers.data_models import LLMMessage, LLMRequestOptions, LLMResponse, Role
from app.services.llm_providers.json_schema_validation import SchemaSpec, build_response_format, validate_against_schema

from .errors import translate_openai_sdk_error


class OpenRouterProvider(LLMProvider):
    provider_name = "openrouter"

    def __init__(
        self,
        *,
        settings: Optional[Settings] = None,
        async_client: Optional[AsyncOpenAI] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        timeout: Optional[int] = None,
    ) -> None:
        self._settings = settings or global_settings
        self.model = model or self._settings.openrouter_default_model
        self.temperature = temperature if temperature is not None else self._settings.default_temperature
        self.timeout = timeout or self._settings.llm_request_timeout_seconds
        default_headers = {
            "HTTP-Referer": self._settings.http_referer,
            "X-Title": self._settings.x_title,
        }
        # max_retries=0: we implement our own rate-limit/temperature retry
        # logic below (_call_chat_completions_async). Disabling the
        # SDK's built-in retry avoids double-retrying and keeps behavior
        # (and tests) predictable.
        self._async_client = async_client or AsyncOpenAI(
            api_key=self._settings.openrouter_api_key,
            base_url=self._settings.openrouter_base_url,
            timeout=self.timeout,
            default_headers=default_headers,
            max_retries=0,
        )

    @property
    def supports_prompt_caching(self) -> bool:
        return True

    # ------------------------------------------------------------------ #
    # LLMProvider                                                        #
    # ------------------------------------------------------------------ #

    async def generate_response(
        self, messages: Sequence[LLMMessage], options: Optional[LLMRequestOptions] = None
    ) -> LLMResponse:
        options = options or LLMRequestOptions()
        payload_messages, extra_body = self._format_messages_for_api(messages, options)
        request_kwargs = self._build_request_kwargs(payload_messages, options, extra_body, model=self.model)
        response = await self._call_chat_completions_async(**request_kwargs)
        return self._parse_raw_response_to_llm_response(response, model=self.model)

    # ------------------------------------------------------------------ #
    # JSONGenerating                                                     #
    # ------------------------------------------------------------------ #

    async def generate_json(
        self,
        messages: Sequence[LLMMessage],
        schema: SchemaSpec,
        options: Optional[LLMRequestOptions] = None,
        max_repair_retries: Optional[int] = None,
    ) -> dict[str, Any]:
        options = options or LLMRequestOptions()
        retries_allowed = (
            max_repair_retries if max_repair_retries is not None else self._settings.max_json_repair_retries
        )
        working_messages: list[LLMMessage] = list(messages)
        mode = "json_schema"
        last_raw_text = ""
        validation_errors: list[str] = []
        repair_attempts_used = 0

        while True:
            payload_messages, extra_body = self._format_messages_for_api(working_messages, options)
            request_kwargs = self._build_request_kwargs(payload_messages, options, extra_body, model=self.model)
            request_kwargs["response_format"] = build_response_format(schema, mode=mode)

            try:
                response = await self._call_chat_completions_async(**request_kwargs)
            except LLMStructuredOutputUnsupportedError:
                if mode == "json_schema":
                    mode = "json_object"
                    continue  # one-time mode downgrade — does not consume a repair attempt
                raise

            last_raw_text = self._extract_text_from_response(response)
            candidate: Optional[dict[str, Any]]
            try:
                candidate = json.loads(last_raw_text)
                validation_errors = validate_against_schema(candidate, schema)
            except json.JSONDecodeError as exc:
                candidate = None
                validation_errors = [f"Response was not valid JSON: {exc}"]

            if candidate is not None and not validation_errors:
                return candidate

            if repair_attempts_used >= retries_allowed:
                raise LLMSchemaValidationError(
                    f"Model output did not satisfy schema {schema.name!r} after "
                    f"{repair_attempts_used} repair attempt(s)",
                    last_raw_text=last_raw_text,
                    validation_errors=validation_errors,
                )

            repair_attempts_used += 1
            working_messages = working_messages + [
                LLMMessage(
                    role=Role.USER,
                    content=self._build_repair_prompt(last_raw_text, schema, validation_errors),
                )
            ]

    # ------------------------------------------------------------------ #
    # Internal: request construction                                    #
    # ------------------------------------------------------------------ #

    def _format_messages_for_api(
        self, messages: Sequence[LLMMessage], options: LLMRequestOptions
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Converts LLMMessage -> OpenAI-shaped message dicts, and attaches
        the system-prompt cache marker if enabled. See §5 below for the
        caching rationale."""
        formatted_payload = [self._convert_message_to_dict(m) for m in messages]

        should_cache_system_prompt = (
            options.cache_system_prompt
            if options.cache_system_prompt is not None
            else self._settings.system_prompt_caching_enabled
        )
        if should_cache_system_prompt:
            for message_entry in formatted_payload:
                if message_entry["role"] == Role.SYSTEM.value:
                    message_entry["content"] = [
                        {
                            "type": "text",
                            "text": message_entry["content"],
                            "prompt_cache_breakpoint": {"mode": "explicit"},
                        }
                    ]
                    break  # only the first system message is marked

        return formatted_payload, {}

    def _convert_message_to_dict(self, message: LLMMessage) -> dict[str, Any]:
        message_dict: dict[str, Any] = {"role": message.role.value, "content": message.content}
        if message.name:
            message_dict["name"] = message.name
        return message_dict

    def _build_request_kwargs(
        self,
        messages_payload: list[dict[str, Any]],
        options: LLMRequestOptions,
        extra_body: dict[str, Any],
        *,
        model: str,
    ) -> dict[str, Any]:
        request_kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages_payload,
            "max_tokens": options.max_output_tokens,
        }
        temperature = options.temperature if options.temperature is not None else self.temperature
        if temperature is not None:
            request_kwargs["temperature"] = temperature

        merged_extra_body = dict(extra_body)
        if options.metadata:
            merged_extra_body["metadata"] = options.metadata
        if options.models_fallback:
            merged_extra_body["models"] = list(options.models_fallback)
        if options.extra_provider_params:
            merged_extra_body.update(options.extra_provider_params)
        # Ask OpenRouter to report real per-request cost so _extract_cost_from_usage can
        # read it back — see the Settings/cost-accounting rationale in
        # docs/implementation.md.
        merged_extra_body.setdefault("usage", {"include": True})

        if merged_extra_body:
            request_kwargs["extra_body"] = merged_extra_body
        return request_kwargs

    # ------------------------------------------------------------------ #
    # Internal: HTTP call with temperature + rate-limit retry            #
    # ------------------------------------------------------------------ #

    async def _call_chat_completions_async(self, **request_kwargs: Any) -> Any:
        attempt_number = 0
        current_request_kwargs = dict(request_kwargs)
        while True:
            try:
                return await self._async_client.chat.completions.create(**current_request_kwargs)
            except openai.APIError as exc:
                translated = translate_openai_sdk_error(exc)
                if isinstance(translated, LLMTemperatureUnsupportedError) and "temperature" in current_request_kwargs:
                    current_request_kwargs = {k: v for k, v in current_request_kwargs.items() if k != "temperature"}
                    continue
                if isinstance(translated, LLMRateLimitError) and attempt_number < self._settings.max_rate_limit_retries:
                    backoff_delay = translated.retry_after_seconds
                    if backoff_delay is None:
                        backoff_delay = self._settings.rate_limit_backoff_base_seconds * (2**attempt_number)
                    await asyncio.sleep(backoff_delay)
                    attempt_number += 1
                    continue
                raise translated from exc

    # ------------------------------------------------------------------ #
    # Internal: response parsing                                        #
    # ------------------------------------------------------------------ #

    def _extract_text_from_response(self, response: Any) -> str:
        choices = getattr(response, "choices", None) or []
        if not choices:
            raise LLMResponseParsingError("Response contained no choices")
        message = getattr(choices[0], "message", None)
        content = getattr(message, "content", None) if message is not None else None
        if content is None:
            raise LLMResponseParsingError("Response choice contained no message content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict):
                    parts.append(part.get("text", ""))
                else:
                    parts.append(str(getattr(part, "text", "")))
            return "".join(parts)
        return str(content)

    def _parse_raw_response_to_llm_response(self, response: Any, *, model: str) -> LLMResponse:
        text = self._extract_text_from_response(response)
        usage = getattr(response, "usage", None)
        prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
        completion_tokens = getattr(usage, "completion_tokens", 0) or 0
        total_tokens = getattr(usage, "total_tokens", None) or (prompt_tokens + completion_tokens)
        cost = self._extract_cost_from_usage(usage)
        cached_tokens, reasoning_tokens = self._extract_token_breakdown_from_usage(usage)
        choices = getattr(response, "choices", None) or []
        finish_reason = getattr(choices[0], "finish_reason", None) if choices else None
        return LLMResponse(
            text=text,
            model=getattr(response, "model", model),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cost=cost,
            cached_tokens=cached_tokens,
            reasoning_tokens=reasoning_tokens,
            response_id=getattr(response, "id", None),
            finish_reason=finish_reason,
            raw_response=response,
        )

    def _extract_cost_from_usage(self, usage: Any) -> Optional[float]:
        if usage is None:
            return None
        cost = getattr(usage, "cost", None)
        if cost is not None:
            return float(cost)
        # openai SDK's typed Usage model allows unknown fields (extra="allow")
        # to future-proof against fields it doesn't know about yet; OpenRouter's
        # `cost` field (only present when `usage: {"include": true}` was
        # requested) lands there if the SDK version in use hasn't added it as
        # a first-class attribute.
        extra = getattr(usage, "model_extra", None) or {}
        if isinstance(extra, dict) and "cost" in extra:
            try:
                return float(extra["cost"])
            except (TypeError, ValueError):
                return None
        return None

    def _extract_token_breakdown_from_usage(self, usage: Any) -> tuple[Optional[int], Optional[int]]:
        if usage is None:
            return None, None
        prompt_details = getattr(usage, "prompt_tokens_details", None)
        cached_tokens = getattr(prompt_details, "cached_tokens", None) if prompt_details else None
        completion_details = getattr(usage, "completion_tokens_details", None)
        reasoning_tokens = getattr(completion_details, "reasoning_tokens", None) if completion_details else None
        return cached_tokens, reasoning_tokens

    def _build_repair_prompt(self, raw_text: str, schema: SchemaSpec, validation_errors: list[str]) -> str:
        errors_block = "\n".join(f"- {e}" for e in validation_errors) or "(no specific errors captured)"
        return (
            "The previous response was supposed to be JSON matching the schema below, "
            "but it did not. Return ONLY corrected JSON — no prose, no markdown fences.\n\n"
            f"Schema (name={schema.name}):\n{json.dumps(schema.json_schema, indent=2)}\n\n"
            f"Validation errors:\n{errors_block}\n\n"
            f"Previous (invalid) response:\n{raw_text}"
        )
```

---

## `app/services/llm_providers/openrouter_provider/__init__.py`

```python
"""Registers OpenRouterProvider with the shared LLMProviderFactory on import.

Anything that wants to use "openrouter" as a provider name must import this
package first, e.g.:

    import app.services.llm_providers.openrouter_provider  # noqa: F401
"""
from app.services.llm_providers.provider_factory import LLMProviderFactory

from .provider import OpenRouterProvider

LLMProviderFactory.register("openrouter", OpenRouterProvider)

__all__ = ["OpenRouterProvider"]
```

---

## §4 — JSON schema validation & strict-mode flow (what `generate_json` above actually does)

1. Resolve `max_repair_retries` from the argument, falling back to `settings.max_json_repair_retries`.
2. Build `response_format` in `"json_schema"` mode (strict) and call chat completions.
3. On success, `json.loads` the text and run `validate_against_schema(...)` **unconditionally**, even though strict mode was requested — see the Draft-7 caveat documented on `validate_against_schema` in `01-core-abstractions.md`. Empty errors → return the parsed dict. Non-empty → fall into the repair loop, with the validation errors embedded in the repair prompt (this is a deliberate improvement over the legacy provider's `ensure_valid_json`, which only repaired on `JSONDecodeError`, never on a schema mismatch).
4. If the call raises `LLMStructuredOutputUnsupportedError` (translated from a 400 whose message/param indicates `response_format` isn't supported for this model), and we're still in `"json_schema"` mode, downgrade to `"json_object"` mode **once** and retry — this downgrade does not consume a repair attempt. In `"json_object"` mode, OpenRouter only guarantees syntactically valid JSON, so `validate_against_schema` becomes the sole enforcement mechanism.
5. The repair loop reuses the legacy `ensure_valid_json`/`_build_repair_prompt` idea, generalized to also fire on schema mismatches, not just parse failures: append a new user message describing the schema, the validation errors, and the previous invalid output, and ask the model to return corrected JSON only. Bounded by `retries_allowed`; on exhaustion, raises `LLMSchemaValidationError` carrying the last raw text and the validation errors for the caller to log/inspect.

## §5 — Provider-side system-prompt caching (the only caching in this design)

There is **no** application-level response cache in this design (an earlier draft had one — `CacheBackend`/`InMemoryTTLCache` — and it was deliberately dropped). The only caching mechanism here is OpenRouter's own provider-side prompt caching, applied specifically to the system message.

- `LLMRequestOptions.cache_system_prompt: Optional[bool] = None` — `None` defers to `settings.system_prompt_caching_enabled` (default `True`).
- `_format_messages_for_api` finds the **first** message with `role == Role.SYSTEM`. If caching resolves to enabled and one exists, its content is restructured from a plain string into a one-element content-parts list, with `"prompt_cache_breakpoint": {"mode": "explicit"}` attached to that part. This is OpenRouter's documented OpenAI-style explicit cache-breakpoint field.
- **Why this is safe across "any model/provider combination":** per OpenRouter's docs, this OpenAI-style marker is automatically converted to Anthropic-style `cache_control` when the request is routed to an Anthropic-serving provider (including Bedrock/Vertex). So the exact same code path benefits Anthropic models, newer OpenAI models, and (per OpenRouter's own framing of `cache_control`/`prompt_cache_breakpoint` as additive, opt-in fields) is expected to be silently ignored — not an error — by models/providers with no caching support at all.
  - **This "silently ignored" behavior is a documented assumption, not a hard guarantee spelled out in OpenRouter's docs for every provider.** Spot-check it once implemented: send a request through a provider that shouldn't support caching and confirm the call still succeeds normally. Step 5 of the Verification checklist in `implementation.md` covers spot-checking the Anthropic side (that caching actually engages); this is the mirror check that it's a no-op elsewhere.
- Scope is deliberately narrow — no generic `cache_breakpoint_at` index parameter, no `prompt_cache_key` session-grouping support. If broader prompt-cache control (e.g. caching a large tool-result block mid-conversation) is wanted later, it's a small additive change to `_format_messages_for_api`/`LLMRequestOptions`, not a redesign.

Next: [`03-config-and-logging.md`](./03-config-and-logging.md).
