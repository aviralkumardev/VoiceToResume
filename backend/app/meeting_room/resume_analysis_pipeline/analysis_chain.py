from typing import Dict, Tuple, Any

import app.services.llm_providers.openrouter_provider
from app.meeting_room.resume_analysis_pipeline.analysis_prompts import EXTRACTION_SYSTEM_PROMPT, FINAL_RESOLUTION_SYSTEM_PROMPT, build_extraction_user_prompt, build_final_resolution_user_prompt
from app.services.llm_providers.data_models import LLMMessage, LLMRequestOptions, LLMResponse
from app.services.llm_providers.json_schema_validation import SchemaSpec
from app.services.llm_providers.provider_exceptions import LLMProviderError, LLMSchemaValidationError
from app.services.llm_providers.provider_factory import LLMProviderFactory
from app.services.llm_providers.provider_interface import LLMProvider

from app.core.config import settings

EXTRACTION_RESPONSE_SCHEMA = SchemaSpec.from_dict(
    {
        "type": "object",
        "properties": {
            "reasoning": {"type": "string"},
            "updates": {"type": "object"},
            "unresolved": {"type": "array"},
            "resolved_conflicts": {"type": "array"},
            "resolved_unresolved_ids": {"type": "array"},
            "remaining_text": {"type": "string"},
            "status": {"type": "string", "enum": ["update", "extracted", "no_update"]},
        },
        "required": ["updates", "remaining_text", "status"],
    },
    name="resume_extraction_result",
    strict=False,
)


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


_extraction_provider_cache: Dict[Tuple[str, str], LLMProvider] = {}
_final_pass_provider_cache: Dict[Tuple[str, str], LLMProvider] = {}


def _get_provider(cache: Dict[Tuple[str, str], LLMProvider], provider_name: str, model: str) -> LLMProvider:
    cache_key = (provider_name, model)
    if cache_key not in cache:
        cache[cache_key] = LLMProviderFactory.create(provider_name, model=model)
    return cache[cache_key]


def _get_extraction_provider() -> LLMProvider:
    return _get_provider(
        _extraction_provider_cache,
        settings.resume_room_extraction_provider,
        settings.resume_room_extraction_model
    )


def _get_final_pass_provider() -> LLMProvider:
    return _get_provider(
        _final_pass_provider_cache,
        settings.resume_room_final_pass_provider,
        settings.resume_room_final_pass_model
    )


def _usage_dict(response: LLMResponse) -> Dict[str, Any]:
    return {
        "model": response.model,
        "prompt_tokens": response.prompt_tokens,
        "completion_tokens": response.completion_tokens,
        "total_tokens": response.total_tokens,
        "cost": response.cost,        
    }


async def run_resume_extraction_chain(resume: Dict[str, Any], new_text: str) -> Dict[str, Any]:
    provider = _get_extraction_provider()
    messages = [
        LLMMessage(role="system", content=EXTRACTION_SYSTEM_PROMPT),
        LLMMessage(role="user", content=build_extraction_user_prompt(resume, new_text))
    ]

    options = LLMRequestOptions(max_output_tokens=settings.resume_room_extraction_max_tokens)

    try:
        parsed, response = await provider.generate_json(messages, EXTRACTION_RESPONSE_SCHEMA, options)

    except LLMSchemaValidationError as exc:
        return {
            "reasoning": "", "updates": {}, "unresolved": [], "resolved_conflicts": [],
            "resolved_unresolved_ids": [], "remaining_text": new_text,
            "status": "no_update",
            "_llm_usage": _usage_dict(exc.last_response) if exc.last_response else None,
        }
    except LLMProviderError as exc:
        return {
            "reasoning": "", "updates": {}, "unresolved": [], "resolved_conflicts": [],
            "resolved_unresolved_ids": [], "remaining_text": new_text,
            "status": "no_update", "_llm_usage": None,
        }

    parsed.setdefault("unresolved", [])
    parsed.setdefault("resolved_conflicts", [])
    parsed.setdefault("resolved_unresolved_ids", [])
    parsed["_llm_usage"] = _usage_dict(response)
    return parsed


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