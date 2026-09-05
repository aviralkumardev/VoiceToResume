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
    TOPIC_QUESTION_SYSTEM_PROMPT,
    build_question_user_prompt,
    build_topic_question_user_prompt,
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
            "next_question": {"type": ["string", "null"]},
            "next_question_target": {
                "type": ["object", "null"],
                "properties": {
                    "block": {"type": "string"},
                    "item_id": {"type": ["string", "null"]},
                    "fields": {"type": ["array", "null"], "items": {"type": "string"}},
                },
            },
        },
        "required": ["answer_grade"],
    },
    name="question_chain_result",
    strict=False,
)

TOPIC_QUESTION_RESPONSE_SCHEMA = SchemaSpec.from_dict(
    {
        "type": "object",
        "properties": {
            "question": {"type": "string"},
        },
        "required": ["question"],
    },
    name="topic_question_result",
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
        "next_question": None,
        "next_question_target": None,
        "_llm_usage": llm_usage,
    }


def _validate_next_target(
    target: Any,
    candidates: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """`target` must name one of the Python-computed `candidates` (by
    `(block, item_id)`) -- this is a cheap containment/subset check against a
    small Python-built list, not the old `_sanitize_target`'s full
    coverage-schema validation, since every candidate is already
    schema-valid by construction. `fields` may be narrowed to a subset of
    the matched candidate's own fields (the model may have resolved some of
    them via the live conversation already); an unrecognized/empty subset
    falls back to the candidate's full field list. Any target that doesn't
    match a given candidate at all (hallucination, wrong shape, omitted)
    falls back to `candidates[0]` -- the single highest-priority pick --
    verbatim."""
    if isinstance(target, dict):
        key = (target.get("block"), target.get("item_id"))
        for candidate in candidates:
            if (candidate["block"], candidate.get("item_id")) == key:
                allowed_fields = candidate.get("fields")
                if allowed_fields is None:
                    return {**candidate, "fields": None}
                raw_fields = target.get("fields")
                fields = (
                    [f for f in raw_fields if f in allowed_fields]
                    if isinstance(raw_fields, list)
                    else []
                )
                return {**candidate, "fields": fields or list(allowed_fields)}
    return dict(candidates[0])


async def run_question_chain(
    resume: Dict[str, Any],
    coverage: Dict[str, Any],
    conversation_history: List[Dict[str, Any]],
    answer_text: str,
    field_completeness: Optional[Dict[str, Any]] = None,
    *,
    current_target: Optional[Dict[str, Any]] = None,
    next_target_candidates: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """The ONE LLM call per answer turn: grades the last answer in
    `conversation_history` AND, in the same response, drafts both the probe
    (if it stays open) and the next question (if it resolves).

    Target *selection* is no longer this chain's decision. `current_target`
    is the round's own already-known `{"block", "item_id", "fields"}` (or
    `None` for the opening round) -- `probe_question` is grounded in it
    directly instead of being re-inferred from raw conversation text.
    `next_target_candidates` is the COMPLETE, Python-computed,
    priority-ordered list of everything left to ask about (see
    `next_target.compute_next_targets`) -- the model may only pick the
    first entry not already resolved by the live conversation (including
    the answer it's grading right now, which `field_completeness` can't
    have caught up to yet) and word `next_question` for it; it can never
    invent a target outside this list. `coverage` must already have any
    not_applicable blocks filtered out by the caller (see
    `askable_coverage_schema` in `coverage_schema.py`).

    Whenever `next_question` is non-null, the response echoes back
    `next_question_target: {"block", "item_id", "fields"}` naming exactly
    which given candidate it used (narrowed to whichever of that
    candidate's fields are still genuinely open) -- validated by
    `_validate_next_target` against `next_target_candidates` before being
    trusted, since a small subset-match check is enough now that every
    candidate is already schema-valid by construction. If
    `next_target_candidates` is empty, or the model determines every
    candidate is already resolved, both `next_question` and
    `next_question_target` are `null` -- since the candidate list is
    exhaustive, that legitimately means nothing is left to ask.
    `next_question_target` is what the caller (`InterviewDirector`) stores
    on the round it opens, and what a later `UNABLE_TO_ANSWER` grade commits
    back into `field_completeness` for every field it named
    (`completeness_status.build_unable_to_answer_patch`) -- the one verdict
    the batched grader can never infer from `resume_data` alone.

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
                resume,
                coverage,
                conversation_history,
                answer_text,
                field_completeness,
                current_target,
                next_target_candidates,
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
    parsed.setdefault("next_question", None)
    parsed.setdefault("next_question_target", None)

    candidates = next_target_candidates or []
    if not candidates or not parsed.get("next_question"):
        parsed["next_question"] = None
        parsed["next_question_target"] = None
    else:
        parsed["next_question_target"] = _validate_next_target(
            parsed.get("next_question_target"), candidates
        )

    if parsed.get("answer_grade") not in (
        ANSWER_GRADE_PARTIAL, ANSWER_GRADE_SUFFICIENT, ANSWER_GRADE_UNABLE_TO_ANSWER,
    ):
        parsed["answer_grade"] = ANSWER_GRADE_PARTIAL

    parsed["_llm_usage"] = _usage_dict(response)
    return parsed


def _topic_fallback_question(topic_description: str) -> Dict[str, Any]:
    return {
        "question": f"Let's cover one more thing -- could you tell me about {topic_description}?",
        "_llm_usage": None,
    }


async def run_topic_question_chain(
    resume: Dict[str, Any],
    coverage: Dict[str, Any],
    conversation_history: List[Dict[str, Any]],
    topic_description: str,
    field_completeness: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Words ONE natural spoken question for a topic Python has already
    decided must be asked -- a forced conflict/unresolved record, or a
    required-coverage gap the safety net caught. Shared by both callers so
    there is one LLM-wording chain for every forced/gap question, not three.

    Fail-soft: on provider/schema error, falls back to one minimal
    Python-built sentence from `topic_description` -- the only non-LLM
    question wording left anywhere in the interview loop.
    """
    if not topic_description.strip():
        return _topic_fallback_question("that")

    provider = _get_provider()
    messages = [
        LLMMessage(role="system", content=TOPIC_QUESTION_SYSTEM_PROMPT),
        LLMMessage(
            role="user",
            content=build_topic_question_user_prompt(
                resume, coverage, conversation_history, topic_description, field_completeness
            ),
        ),
    ]
    options = LLMRequestOptions(max_output_tokens=settings.resume_room_question_max_tokens)

    try:
        parsed, response = await provider.generate_json(
            messages, TOPIC_QUESTION_RESPONSE_SCHEMA, options
        )
    except LLMSchemaValidationError:
        return _topic_fallback_question(topic_description)
    except LLMProviderError:
        return _topic_fallback_question(topic_description)

    if not isinstance(parsed.get("question"), str) or not parsed["question"].strip():
        return _topic_fallback_question(topic_description)

    parsed["_llm_usage"] = _usage_dict(response)
    return parsed
