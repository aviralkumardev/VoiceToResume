from app.services.llm_providers.provider_factory import LLMProviderFactory

from .provider import OpenRouterProvider

LLMProviderFactory.register("openrouter", OpenRouterProvider)

__all__ = ["OpenRouterProvider"]
