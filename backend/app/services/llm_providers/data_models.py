from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Sequence


@dataclass(frozen=True)
class LLMMessage:
    """A single chat turn."""

    role: str
    content: str


@dataclass(frozen=True)
class LLMRequestOptions:
    max_output_tokens: int = 1200
    temperature: Optional[float] = None
    metadata: Optional[dict[str, Any]] = None
    models_fallback: Optional[Sequence[str]] = None
    cache_system_prompt: Optional[bool] = None
    extra_provider_params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LLMResponse:
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
