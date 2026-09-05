from typing import Any, Dict, List, Optional, Tuple

from app.core.config import settings
import app.services.llm_providers.openai_provider
import app.services.llm_providers.openrouter_provider
from app.services.llm_providers.data_models import LLMMessage, LLMRequestOptions, LLMResponse
from app.services.llm_providers.json_schema_validation import SchemaSpec
from app.services.llm_providers.provider_exceptions import LLMProviderError, LLMSchemaValidationError
from app.services.llm_providers.provider_factory import LLMProviderFactory
from app.services.llm_providers.provider_interface import LLMProvider

from app.meeting_room.resume_analysis_pipeline.question_prompts import (
    SYSTEM_PROMPT,
    build_question_user_prompt,
)


ANSWER_GRADE_PARTIAL = "PARTIAL"
ANSWER_GRADE_SUFFICIENT = "SUFFICIENT"
ANSWER_GRADE_UNABLE_TO_ANSWER = "UNABLE_TO_ANSWER"

# Terminal for a ROUND, not to be confused with completeness_status's
# TERMINAL_STATUSES (which also includes NOT_APPLICABLE, a concept that has
# no meaning for a single graded answer).
TERMINAL_GRADES = frozenset({ANSWER_GRADE_SUFFICIENT, ANSWER_GRADE_UNABLE_TO_ANSWER})


QUESTION_RESPONSE_SCHEMA = SchemaSpec.from_dict(
    {
        "type": "object",
        "properties": {
            "is_meta_question": {"type": "boolean"},
            "meta_response": {"type": ["string", "null"]},
            "answer_grade": {"type": "string"},
            "reason": {"type": ["string", "null"]},
            "probe_question": {"type": ["string", "null"]},
        },
        "required": ["answer_grade"],
    },
    name="question_chain_result",
    strict=False,
)


_provider_cache: Dict[Tuple[str, str], LLMProvider] = {}


def _get_provider() -> LLMProvider:
    cache_key = (
        settings.resume_room_question_provider,
        settings.resume_room_question_model,
    )
    if cache_key not in _provider_cache:
        _provider_cache[cache_key] = LLMProviderFactory.create(cache_key[0], model=cache_key[1])
    return _provider_cache[cache_key]


def _usage_dict(response: LLMResponse) -> Dict[str, Any]:
    return {
        "model": response.model,
        "prompt_tokens": response.prompt_tokens,
        "completion_tokens": response.completion_tokens,
        "total_tokens": response.total_tokens,
        "cost": response.cost,
    }


def _empty_result(llm_usage: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "is_meta_question": False,
        "meta_response": None,
        "answer_grade": ANSWER_GRADE_PARTIAL,
        "reason": None,
        "probe_question": None,
        "_llm_usage": llm_usage,
    }


async def run_answer_grading_chain(
    conversation_history: List[Dict[str, Any]],
    answer_text: str,
    target_complete_when: Any,
) -> Dict[str, Any]:
    """The ONE LLM call per answer turn: grades the last answer in
    `conversation_history` against the round's own `target_complete_when`
    bar, and drafts a probe if it stays open.

    Target *selection* is not this chain's job at all any more -- the
    upcoming question queue is regenerated separately by the combined
    analysis call (`combined_chain.run_combined_chain`, triggered by the
    ~100-char buffer / answer-end flush) and `InterviewDirector` just pops
    the next already-worded question off it. This call's ONLY concern is:
    is THIS answer, against THIS round's own bar, done? `target_complete_when`
    is a single string for a whole-block target, or a list of strings for a
    field-scoped target (see `coverage_schema.complete_when_for_target`) --
    there is no `resume`/`coverage`/`field_completeness`/candidate list here
    at all, deliberately: grading one answer against its own bar needs none
    of that.

    Fail-soft: any provider/schema error returns `_empty_result()`, whose
    `answer_grade` is PARTIAL -- "nothing usable" means "still open", never
    "done", same convention every other chain in this pipeline uses.
    """
    if not answer_text.strip():
        return _empty_result()

    provider = _get_provider()
    messages = [
        LLMMessage(role="system", content=SYSTEM_PROMPT),
        LLMMessage(
            role="user",
            content=build_question_user_prompt(
                conversation_history, answer_text, target_complete_when,
            ),
        ),
    ]
    options = LLMRequestOptions(max_output_tokens=settings.resume_room_question_max_tokens)

    try:
        parsed, response = await provider.generate_json(messages, QUESTION_RESPONSE_SCHEMA, options)
    except LLMSchemaValidationError as exc:
        return _empty_result(_usage_dict(exc.last_response) if exc.last_response else None)
    except LLMProviderError:
        return _empty_result()

    parsed.setdefault("is_meta_question", False)
    parsed.setdefault("meta_response", None)
    parsed.setdefault("reason", None)
    parsed.setdefault("probe_question", None)

    if parsed.get("answer_grade") not in (
        ANSWER_GRADE_PARTIAL, ANSWER_GRADE_SUFFICIENT, ANSWER_GRADE_UNABLE_TO_ANSWER,
    ):
        parsed["answer_grade"] = ANSWER_GRADE_PARTIAL

    parsed["_llm_usage"] = _usage_dict(response)
    return parsed
