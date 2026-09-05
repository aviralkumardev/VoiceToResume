from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional, Sequence

from .data_models import LLMMessage, LLMRequestOptions, LLMResponse


class LLMProvider(ABC):

    provider_name: str

    @abstractmethod
    async def generate_response(
        self,
        messages: Sequence[LLMMessage],
        options: Optional[LLMRequestOptions] = None,
    ) -> LLMResponse:
        """Async chat completion."""
        raise NotImplementedError
