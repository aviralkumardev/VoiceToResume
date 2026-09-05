"""
Manual, runnable reference for the OpenRouter LLM provider.

    cd backend
    python -m app.llm_test
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel, Field

from app.services.llm_providers import (
    LLMMessage,
    LLMProviderError,
    LLMProviderFactory,
    LLMRequestOptions,
    SchemaSpec,
)

import app.services.llm_providers.openrouter_provider


def build_provider():
    return LLMProviderFactory.create("openrouter")



async def demo_simple_response(provider) -> None:
    print("\n=== 1. generate_response (simple text) ===")

    messages = [
        LLMMessage(role="system", content="You are a terse assistant. One sentence answers only."),
        LLMMessage(role="user", content="Is AI Solve a company"),
    ]

    response = await provider.generate_response(messages)

    print("text:", response.text)
    print(f"model={response.model} tokens={response.total_tokens} cost={response.cost}")



class ResumeHighlight(BaseModel):
    skill: str = Field(description="A single skill or technology")
    years_of_experience: int


async def demo_json_generation(provider) -> None:
    print("\n=== 2. generate_json (structured output) ===")

    schema = SchemaSpec.from_pydantic(ResumeHighlight)
    messages = [
        LLMMessage(role="system", content="Extract structured data from the user's message."),
        LLMMessage(role="user", content="I've been working with PostgreSQL for about 4 years."),
    ]

    result, response = await provider.generate_json(messages, schema)

    print("parsed JSON:", result)
    print(f"model={response.model} tokens={response.total_tokens} cost={response.cost}")



async def demo_request_options(provider) -> None:
    print("\n=== 3. generate_response with LLMRequestOptions ===")

    options = LLMRequestOptions(
        max_output_tokens=60,
        temperature=0.2,
        metadata={"feature": "llm_test_demo"},
        models_fallback=["openai/gpt-4o-mini"],
        cache_system_prompt=False,
        extra_provider_params={"provider": {"sort": "price"}},
    )

    messages = [
        LLMMessage(role="user", content="Give me one interview tip in a single short sentence."),
    ]

    response = await provider.generate_response(messages, options)

    print("text:", response.text)
    print(f"model={response.model} tokens={response.total_tokens} cost={response.cost}")


async def main() -> None:
    provider = build_provider()

    try:
        await demo_simple_response(provider)
        await demo_json_generation(provider)
        await demo_request_options(provider)
    except LLMProviderError as provider_error:
        print(f"\nLLM call failed: {type(provider_error).__name__}: {provider_error}")


if __name__ == "__main__":
    asyncio.run(main())
