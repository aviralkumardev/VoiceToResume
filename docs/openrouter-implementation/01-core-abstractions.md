# Core Abstraction Layer

Destination: `app/services/llm_providers/` (everything in this doc is provider-agnostic — it must not import anything from `openrouter_provider/`, only the other way around).

Create the package with an empty-looking `__init__.py` (shown in full at the bottom of this doc — it re-exports the public surface) plus the six modules below.

---

## `app/services/llm_providers/data_models.py`

```python
"""Provider-agnostic data models for the LLM provider abstraction layer.

Every concrete provider (OpenRouter today, potentially Anthropic/Gemini
later) speaks these dataclasses at its public boundary, so callers never
depend on a specific provider's SDK types.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Sequence


class Role(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True)
class LLMMessage:
    """A single chat turn."""

    role: Role
    content: str
    name: Optional[str] = None


@dataclass(frozen=True)
class LLMRequestOptions:
    """Per-call knobs. Every field is optional; a provider fills in its own
    default (usually from Settings) for anything left as None."""

    max_output_tokens: int = 1200
    temperature: Optional[float] = None
    metadata: Optional[dict[str, Any]] = None
    models_fallback: Optional[Sequence[str]] = None
    cache_system_prompt: Optional[bool] = None
    extra_provider_params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LLMResponse:
    """Normalized result of a single generation call."""

    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost: Optional[float]
    cached_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None
    response_id: Optional[str] = None
    finish_reason: Optional[str] = None
    raw_response: Optional[Any] = None
```

**Notes**
- `cost` is deliberately positional/required-but-nullable (no default) so every call site has to consciously decide what to pass — usually the result of `_extract_cost(usage)`, which is `None` when OpenRouter didn't return a `cost` field.
- There's no `cache_breakpoint`/`cache_breakpoint_at`/`prompt_cache_key` field anywhere here — caching in this design is narrowed to "the system prompt gets a cache marker or it doesn't," controlled by `cache_system_prompt`. See `02-openrouter-provider.md` §5 for the mechanics.

---

## `app/services/llm_providers/provider_exceptions.py`

```python
"""Provider-agnostic exception hierarchy.

Every concrete provider's errors.py module is responsible for translating its
SDK's exceptions into these classes, so calling code only ever needs to catch
one hierarchy regardless of which provider is behind an LLMProvider instance.
"""
from __future__ import annotations

from typing import Optional


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

    def __init__(self, message: str, *, last_raw_text: str, validation_errors: list[str]) -> None:
        super().__init__(message)
        self.last_raw_text = last_raw_text
        self.validation_errors = validation_errors


class LLMResponseParsingError(LLMProviderError):
    """The HTTP call succeeded but the response body couldn't be parsed into
    the expected shape (missing text, malformed usage block, etc.)."""


class LLMTimeoutError(LLMProviderError):
    """The request exceeded the configured timeout."""
```

---

## `app/services/llm_providers/provider_interface.py`

```python
"""The minimal contract every LLM provider must implement.

Kept intentionally small (Interface Segregation Principle) — optional
capabilities (JSON generation, prompt caching) live in
provider_capabilities.py as separate Protocols instead of being forced into this base
class, so a provider that can't support one of them isn't obligated to stub
it out.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional, Sequence

from .data_models import LLMMessage, LLMRequestOptions, LLMResponse


class LLMProvider(ABC):
    """Every concrete provider (OpenRouterProvider, a future
    AnthropicProvider, etc.) subclasses this. Subclassing an ABC — rather
    than a Protocol — is deliberate: the factory registry does isinstance()
    checks and we want a hard error at class-definition time if a required
    method is missing, not a silent structural mismatch discovered at call
    time."""

    provider_name: str

    @abstractmethod
    async def generate_response(
        self,
        messages: Sequence[LLMMessage],
        options: Optional[LLMRequestOptions] = None,
    ) -> LLMResponse:
        """Async chat completion."""
        raise NotImplementedError
```

---

## `app/services/llm_providers/provider_capabilities.py`

```python
"""Narrow, optional capability contracts (JSON generation, prompt caching).

These are typing.Protocol, not ABCs: a provider satisfies one just by having
matching methods (structural typing) — no explicit inheritance required.
Consumers that only need one capability type-hint against that Protocol
instead of the full concrete provider class, and can feature-detect at
runtime with isinstance() because each is @runtime_checkable.
"""
from __future__ import annotations

from typing import Any, Optional, Protocol, Sequence, runtime_checkable

from .data_models import LLMMessage, LLMRequestOptions
from .json_schema_validation import SchemaSpec


@runtime_checkable
class JSONGenerating(Protocol):
    async def generate_json(
        self,
        messages: Sequence[LLMMessage],
        schema: SchemaSpec,
        options: Optional[LLMRequestOptions] = None,
        max_repair_retries: Optional[int] = None,
    ) -> dict[str, Any]:
        ...


@runtime_checkable
class PromptCaching(Protocol):
    """Marker capability: does this provider support provider-side prompt
    caching at all? This is unrelated to any application-level response
    cache — there isn't one in this design. See
    docs/02-openrouter-provider.md for how OpenRouter's system-prompt
    caching is implemented."""

    supports_prompt_caching: bool
```

---

## `app/services/llm_providers/json_schema_validation.py`

```python
"""JSON Schema construction and validation for structured LLM outputs.

Supports both a pydantic BaseModel subclass and a raw JSON-Schema dict as the
source of truth, unified behind SchemaSpec so provider code never branches on
which one the caller supplied.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Type

from jsonschema import Draft7Validator
from pydantic import BaseModel


@dataclass(frozen=True)
class SchemaSpec:
    name: str
    json_schema: dict[str, Any]
    strict: bool = True

    @classmethod
    def from_pydantic(
        cls, model: Type[BaseModel], *, name: Optional[str] = None, strict: bool = True
    ) -> "SchemaSpec":
        generated_json_schema = model.model_json_schema()
        generated_json_schema.setdefault("additionalProperties", False)
        return cls(name=name or model.__name__, json_schema=generated_json_schema, strict=strict)

    @classmethod
    def from_dict(
        cls, schema: dict[str, Any], *, name: str, strict: bool = True
    ) -> "SchemaSpec":
        return cls(name=name, json_schema=schema, strict=strict)


def build_response_format(
    schema_spec: SchemaSpec, *, response_format_mode: str = "json_schema"
) -> dict[str, Any]:
    """Build the `response_format` request field.

    mode="json_schema" -> strict structured output, enforced provider-side
    (when the model supports it) and always re-validated locally as defense
    in depth.
    mode="json_object" -> basic JSON mode; the provider only guarantees
    syntactically valid JSON, so validate_against_schema() below is the only
    schema enforcement that actually happens.
    """
    if response_format_mode == "json_schema":
        return {
            "type": "json_schema",
            "json_schema": {
                "name": schema_spec.name,
                "strict": schema_spec.strict,
                "schema": schema_spec.json_schema,
            },
        }
    if response_format_mode == "json_object":
        return {"type": "json_object"}
    raise ValueError(f"Unknown response_format mode: {response_format_mode!r}")


def validate_against_schema(
    json_payload: dict[str, Any], schema_spec: SchemaSpec
) -> list[str]:
    """Validate `payload` against spec.json_schema using JSON Schema Draft 7
    — deliberately the same draft OpenRouter documents its strict mode as
    enforcing, so local validation and provider-side enforcement agree even
    when strict mode is used. Returns a list of human-readable error
    messages (empty list = valid). Called unconditionally on every JSON
    response in OpenRouterProvider.generate_json, strict mode or not — see
    02-openrouter-provider.md §4 for why.

    Known, documented divergence from provider-side strict enforcement
    (not fixable locally): keywords introduced only in JSON Schema Draft
    2019-09/2020-12 (e.g. `unevaluatedProperties`, `$dynamicRef`) are
    enforced here by python-jsonschema's Draft7Validator as written into the
    schema, but OpenRouter's strict mode does not enforce them provider-side;
    and `pattern` regexes are matched with Python `re` semantics here vs
    ECMA-262 semantics provider-side, which can differ for advanced regex
    features. In practice this only matters for schemas using those
    less-common keywords.
    """
    json_schema_validator = Draft7Validator(schema_spec.json_schema)
    validation_errors = sorted(
        json_schema_validator.iter_errors(json_payload), key=lambda error: list(error.path)
    )
    return [
        f"{'/'.join(str(p) for p in error.path) or '<root>'}: {error.message}"
        for error in validation_errors
    ]
```

---

## `app/services/llm_providers/provider_factory.py`

```python
"""Registry mapping a provider name to a constructor, so calling code never
imports a concrete provider class directly (Open/Closed + Dependency
Inversion: adding a new provider means a new module that calls
LLMProviderFactory.register(...) once — zero edits here).
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List

from .provider_interface import LLMProvider

_LLMProviderConstructor = Callable[..., LLMProvider]


class LLMProviderFactory:
    _provider_registry: Dict[str, _LLMProviderConstructor] = {}

    @classmethod
    def register(cls, provider_name: str, provider_constructor: _LLMProviderConstructor) -> None:
        cls._provider_registry[provider_name] = provider_constructor

    @classmethod
    def create(cls, provider_name: str, **kwargs: Any) -> LLMProvider:
        try:
            constructor = cls._provider_registry[provider_name]
        except KeyError as exc:
            raise ValueError(
                f"Unknown LLM provider {provider_name!r}. Registered providers: {cls.registered_provider_names()}"
            ) from exc
        return constructor(**kwargs)

    @classmethod
    def registered_provider_names(cls) -> List[str]:
        return sorted(cls._provider_registry)
```

Consumers never do `from app.services.llm_providers.openrouter_provider import OpenRouterProvider` in business logic — they do:

```python
import app.services.llm_providers.openrouter_provider  # noqa: F401 — side effect: registers "openrouter"
from app.services.llm_providers.provider_factory import LLMProviderFactory

provider = LLMProviderFactory.create("openrouter")
```

---

## `app/services/llm_providers/__init__.py`

```python
"""Provider-agnostic LLM abstraction layer.

Import a concrete provider package (e.g.
`app.services.llm_providers.openrouter_provider`) to register it with
LLMProviderFactory before calling LLMProviderFactory.create(...).
"""
from .provider_interface import LLMProvider
from .provider_capabilities import JSONGenerating, PromptCaching
from .provider_exceptions import (
    LLMAuthenticationError,
    LLMInvalidRequestError,
    LLMProviderError,
    LLMProviderUnavailableError,
    LLMRateLimitError,
    LLMResponseParsingError,
    LLMSchemaValidationError,
    LLMStructuredOutputUnsupportedError,
    LLMTemperatureUnsupportedError,
    LLMTimeoutError,
)
from .provider_factory import LLMProviderFactory
from .data_models import LLMMessage, LLMRequestOptions, LLMResponse, Role
from .json_schema_validation import SchemaSpec, build_response_format, validate_against_schema

__all__ = [
    "LLMProvider",
    "JSONGenerating",
    "PromptCaching",
    "LLMAuthenticationError",
    "LLMInvalidRequestError",
    "LLMProviderError",
    "LLMProviderUnavailableError",
    "LLMRateLimitError",
    "LLMResponseParsingError",
    "LLMSchemaValidationError",
    "LLMStructuredOutputUnsupportedError",
    "LLMTemperatureUnsupportedError",
    "LLMTimeoutError",
    "LLMProviderFactory",
    "LLMMessage",
    "LLMRequestOptions",
    "LLMResponse",
    "Role",
    "SchemaSpec",
    "build_response_format",
    "validate_against_schema",
]
```

Next: [`02-openrouter-provider.md`](./02-openrouter-provider.md).
