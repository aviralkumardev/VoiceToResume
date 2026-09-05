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
            provider_constructor = cls._provider_registry[provider_name]
        except KeyError as key_error:
            raise ValueError(
                f"Unknown LLM provider {provider_name!r}. "
                f"Registered providers: {cls.registered_provider_names()}"
            ) from key_error
        return provider_constructor(**kwargs)

    @classmethod
    def registered_provider_names(cls) -> List[str]:
        return sorted(cls._provider_registry)
