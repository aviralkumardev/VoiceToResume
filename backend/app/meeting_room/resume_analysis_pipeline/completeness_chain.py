from typing import Any, Dict, Tuple

from app.services.llm_providers.json_schema_validation import SchemaSpec
from app.core.config import settings
import app.services.llm_providers.openrouter_provider
from app.services.llm_providers.data_models import LLMMessage, LLMRequestOptions, LLMResponse
from app.services.llm_providers.provider_exceptions import LLMProviderError, LLMSchemaValidationError
from app.services.llm_providers.provider_factory import LLMProviderFactory
from app.meeting_room.resume_analysis_pipeline.completeness_prompts import (
    SYSTEM_PROMPT,
    build_completeness_user_prompt,
)
from app.services.llm_providers.provider_interface import LLMProvider

COMPLETENESS_RESPONSE_SCHEMA = SchemaSpec.from_dict(
    {
        "type": "object",
        "properties": {
            "reasoning": {"type": "string"},
            "blocks": {"type": "object"},
        },
        "required": ["blocks"]
    },
    name="completeness_result",
    strict = False
)


_completeness_provider_cache: Dict[Tuple[str, str], LLMProvider] = {}


def _get_provider(cache: Dict[Tuple[str, str], LLMProvider], provider_name: str, model: str) -> LLMProvider:
    cache_key = (provider_name, model)
    if cache_key not in cache:
        cache[cache_key] = LLMProviderFactory.create(provider_name, model=model)
    return cache[cache_key]


def _get_completeness_provider() -> LLMProvider:
    return _get_provider(
        _completeness_provider_cache,
        settings.resume_room_completeness_provider,
        settings.resume_room_completeness_model,
    )


def _usage_dict(response: LLMResponse) -> Dict[str, Any]:
    return {
        "model": response.model,
        "prompt_tokens": response.prompt_tokens,
        "completion_tokens": response.completion_tokens,
        "total_tokens": response.total_tokens,
        "cost": response.cost,
    }


async def run_completeness_chain(
    to_judge: Dict[str, Any],
    coverage: Dict[str, Any],
) -> Dict[str, Any]:
    """Batched whole-resume completeness grading only -- the silence worker's
    volunteered-info sweep. Question wording is no longer this chain's job:
    the only place a question is ever LLM-worded is the fused answer-grading-
    and-next-question call in `question_chain.py` (plus the small shared
    `run_topic_question_chain` for forced/gap topics)."""
    if not to_judge:
        return {"reasoning": "", "blocks": {}, "_llm_usage": None}

    provider = _get_completeness_provider()
    messages = [
        LLMMessage(role="system", content=SYSTEM_PROMPT),
        LLMMessage(
            role="user",
            content=build_completeness_user_prompt(to_judge, coverage),
        )
    ]

    options = LLMRequestOptions(max_output_tokens=settings.resume_room_completeness_max_tokens)

    try:
        parsed, response = await provider.generate_json(messages, COMPLETENESS_RESPONSE_SCHEMA, options)
    except LLMSchemaValidationError as exc:
        return {
            "reasoning": "", "blocks": {},
            "_llm_usage": _usage_dict(exc.last_response) if exc.last_response else None,
        }
    except LLMProviderError:
        return {"reasoning": "", "blocks": {}, "_llm_usage": None}

    parsed.setdefault("blocks", {})
    parsed["_llm_usage"] = _usage_dict(response)
    return parsed
