from __future__ import annotations

from typing import Any, Optional, Protocol, Sequence, runtime_checkable

from .data_models import LLMMessage, LLMRequestOptions, LLMResponse
from .json_schema_validation import SchemaSpec


@runtime_checkable
class JSONGenerating(Protocol):

    async def generate_json(
        self,
        messages: Sequence[LLMMessage],
        schema: SchemaSpec,
        options: Optional[LLMRequestOptions] = None,
        max_repair_retries: Optional[int] = None,
    ) -> tuple[dict[str, Any], LLMResponse]:
        ...


@runtime_checkable
class PromptCaching(Protocol):
    supports_prompt_caching: bool
