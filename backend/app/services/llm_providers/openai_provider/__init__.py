from app.services.llm_providers.provider_factory import LLMProviderFactory

from .provider import OpenAIProvider

LLMProviderFactory.register("openai", OpenAIProvider)

__all__ = ["OpenAIProvider"]
