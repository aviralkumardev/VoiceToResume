from typing import Dict, Tuple, Any

import app.services.llm_providers.openrouter_provider
from app.meeting_room.resume_analysis_pipeline.analysis_prompts import FINAL_RESOLUTION_SYSTEM_PROMPT, build_final_resolution_user_prompt
from app.services.llm_providers.data_models import LLMMessage, LLMRequestOptions, LLMResponse
from app.services.llm_providers.json_schema_validation import SchemaSpec
from app.services.llm_providers.provider_exceptions import LLMProviderError, LLMSchemaValidationError
from app.services.llm_providers.provider_factory import LLMProviderFactory
from app.services.llm_providers.provider_interface import LLMProvider

from app.core.config import settings

FINAL_RESOLUTION_RESPONSE_SCHEMA = SchemaSpec.from_dict(
    {
        "type": "object",
        "properties": {
            "reasoning": {"type": "string"},
            "updates": {"type": "object"},
        },
        "required": ["updates"],
    },
    name="resume_final_resolution_result",
    strict=False,
)


_final_pass_provider_cache: Dict[Tuple[str, str], LLMProvider] = {}


def _get_final_pass_provider() -> LLMProvider:
    cache_key = (settings.resume_room_final_pass_provider, settings.resume_room_final_pass_model)
    if cache_key not in _final_pass_provider_cache:
        _final_pass_provider_cache[cache_key] = LLMProviderFactory.create(cache_key[0], model=cache_key[1])
    return _final_pass_provider_cache[cache_key]


def _usage_dict(response: LLMResponse) -> Dict[str, Any]:
    return {
        "model": response.model,
        "prompt_tokens": response.prompt_tokens,
        "completion_tokens": response.completion_tokens,
        "total_tokens": response.total_tokens,
        "cost": response.cost,
    }


async def run_resume_final_resolution_chain(resume: Dict[str, Any], full_transcript: str) -> Dict[str, Any]:
    provider = _get_final_pass_provider()
    messages = [
        LLMMessage(role="system", content=FINAL_RESOLUTION_SYSTEM_PROMPT),
        LLMMessage(role="user", content=build_final_resolution_user_prompt(resume, full_transcript)),
    ]
    options = LLMRequestOptions(max_output_tokens=settings.resume_room_final_pass_max_tokens)

    try:
        parsed, response = await provider.generate_json(messages, FINAL_RESOLUTION_RESPONSE_SCHEMA, options)
    except LLMSchemaValidationError as exc:
        return {
            "reasoning": "", "updates": {},
            "_llm_usage": _usage_dict(exc.last_response) if exc.last_response else None,
        }
    except LLMProviderError as exc:
        return {"reasoning": "", "updates": {}, "_llm_usage": None}

    parsed["_llm_usage"] = _usage_dict(response)
    return parsed
