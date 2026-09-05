from __future__ import annotations

from typing import Optional

from .data_models import LLMResponse


class LLMProviderError(Exception):
    """Base class for every error raised by an LLMProvider."""


class LLMRateLimitError(LLMProviderError):
    """The provider (or OpenRouter itself) rejected the request with 429."""

    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: Optional[float] = None,
        provider_code: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds
        self.provider_code = provider_code


class LLMInvalidRequestError(LLMProviderError):
    """Generic 400 — bad request shape, invalid params, context length
    exceeded, etc."""


class LLMTemperatureUnsupportedError(LLMInvalidRequestError):
    """The selected model rejects a custom `temperature` value (reasoning-tier
    models such as OpenAI's o-series only support the default temperature)."""


class LLMStructuredOutputUnsupportedError(LLMInvalidRequestError):
    """The selected model/provider combination does not support strict
    `response_format: json_schema` structured outputs."""


class LLMProviderUnavailableError(LLMProviderError):
    """Upstream provider is overloaded, unavailable, or returned a 5xx."""


class LLMAuthenticationError(LLMProviderError):
    """401/403 — missing or invalid API key, or insufficient permissions."""


class LLMSchemaValidationError(LLMProviderError):
    """The model's JSON output could not be made to satisfy the requested
    schema even after all repair retries were exhausted."""

    def __init__(
        self,
        message: str,
        *,
        last_raw_text: str,
        validation_errors: list[str],
        last_response: Optional[LLMResponse] = None,
    ) -> None:
        super().__init__(message)
        self.last_raw_text = last_raw_text
        self.validation_errors = validation_errors
        self.last_response = last_response


class LLMResponseParsingError(LLMProviderError):
    """The HTTP call succeeded but the response body couldn't be parsed into
    the expected shape (missing text, malformed usage block, etc.)."""


class LLMTimeoutError(LLMProviderError):
    """The request exceeded the configured timeout."""
