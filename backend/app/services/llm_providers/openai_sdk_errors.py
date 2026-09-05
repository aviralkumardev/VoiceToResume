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

_STRUCTURED_OUTPUT_ERROR_MESSAGE_HINTS = (
    "response_format",
    "json_schema",
    "structured output",
    "structured_outputs",
)


def _extract_error_body(sdk_exception: "openai.APIStatusError") -> dict[str, Any]:
    """Extract the error body dict from an openai SDK status error."""
    raw_error_body = getattr(sdk_exception, "body", None) or {}
    if isinstance(raw_error_body, dict):
        nested_error_body = raw_error_body.get("error", raw_error_body)
        return nested_error_body if isinstance(nested_error_body, dict) else {}
    return {}


def _extract_retry_after_seconds(sdk_exception: "openai.APIStatusError") -> Optional[float]:
    """Read the Retry-After header value from the SDK exception's response."""
    http_response = getattr(sdk_exception, "response", None)
    retry_after_header_value = (
        http_response.headers.get("retry-after") if http_response is not None else None
    )
    if retry_after_header_value is None:
        return None
    try:
        return float(retry_after_header_value)
    except ValueError:
        return None


def _is_temperature_rejection(
    sdk_exception: "openai.BadRequestError", error_body: dict[str, Any]
) -> bool:
    """True if this 400 is the model refusing a custom `temperature` value
    (reasoning-tier models such as OpenAI's o-series and gpt-5.6-luna only
    support the default temperature and reject the parameter outright)."""
    error_param_name = getattr(sdk_exception, "param", None) or error_body.get("param")
    if error_param_name == "temperature":
        return True
    error_message_text = str(sdk_exception)
    return "temperature" in error_message_text and "not supported" in error_message_text


def _is_structured_output_rejection(
    sdk_exception: "openai.BadRequestError", error_body: dict[str, Any]
) -> bool:
    """Heuristic — neither OpenRouter nor OpenAI document a single stable
    error code for 'this model doesn't support structured JSON output', so
    this inspects the `param` field and the error message for hints. If you
    find a model/provider combination this misses, tighten the heuristic
    here; nothing outside this file needs to change."""
    error_param_name = (
        getattr(sdk_exception, "param", None) or error_body.get("param") or ""
    ).lower()
    error_message_text = str(sdk_exception).lower()
    if "response_format" in error_param_name:
        return True
    return any(hint in error_message_text for hint in _STRUCTURED_OUTPUT_ERROR_MESSAGE_HINTS)


def translate_openai_sdk_error(sdk_exception: Exception) -> LLMProviderError:
    """Maps any exception raised by an openai SDK call to our shared
    provider_exceptions.py hierarchy. Unknown exception types are wrapped in a
    plain LLMProviderError rather than left to propagate, so calling code only
    ever needs to catch LLMProviderError and its subclasses.

    Shared by every provider built on the `openai` Python SDK (OpenRouter,
    which is OpenAI-API-compatible, and OpenAI itself) — the SDK raises the
    same exception types regardless of which base_url the client points at.
    """
    if isinstance(sdk_exception, openai.RateLimitError):
        error_body = _extract_error_body(sdk_exception)
        error_metadata = error_body.get("metadata")
        upstream_provider_code = (
            error_metadata.get("provider_code") if isinstance(error_metadata, dict) else None
        )
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
