from typing import Any, Dict, List, Optional, Tuple

from app.core.config import settings
import app.services.llm_providers.openai_provider
import app.services.llm_providers.openrouter_provider
from app.services.llm_providers.data_models import LLMMessage, LLMRequestOptions, LLMResponse
from app.services.llm_providers.json_schema_validation import SchemaSpec
from app.services.llm_providers.provider_exceptions import LLMProviderError, LLMSchemaValidationError
from app.services.llm_providers.provider_factory import LLMProviderFactory
from app.services.llm_providers.provider_interface import LLMProvider

from app.meeting_room.resume_analysis_pipeline.combined_prompts import (
    SYSTEM_PROMPT,
    build_combined_user_prompt,
)


COMBINED_RESPONSE_SCHEMA = SchemaSpec.from_dict(
    {
        "type": "object",
        "properties": {
            "reasoning": {"type": "string"},
            "status": {"type": "string", "enum": ["update", "extracted", "no_update"]},
            "updates": {"type": "object"},
            "unresolved": {"type": "array"},
            "resolved_conflicts": {"type": "array"},
            "resolved_unresolved_ids": {"type": "array"},
            "remaining_text": {"type": "string"},
            "blocks": {"type": "object"},
            "queue": {"type": "array"},
            "more_items_asked": {"type": "array"},
        },
        "required": ["updates", "remaining_text", "status", "blocks", "queue"],
    },
    name="combined_chain_result",
    strict=False,
)


_provider_cache: Dict[Tuple[str, str], LLMProvider] = {}


def _get_provider() -> LLMProvider:
    cache_key = (settings.resume_room_combined_provider, settings.resume_room_combined_model)
    if cache_key not in _provider_cache:
        # `reasoning_effort` is an OpenAIProvider-only constructor kwarg --
        # passed explicitly here (rather than left to OpenAIProvider's own
        # fallback, which defaults to resume_room_question_reasoning_effort)
        # so this chain gets its own independent reasoning-effort knob
        # instead of silently inheriting the question chain's.
        kwargs: Dict[str, Any] = {"model": cache_key[1]}
        if cache_key[0] == "openai":
            kwargs["reasoning_effort"] = settings.resume_room_combined_reasoning_effort
        _provider_cache[cache_key] = LLMProviderFactory.create(cache_key[0], **kwargs)
    return _provider_cache[cache_key]


def _usage_dict(response: LLMResponse) -> Dict[str, Any]:
    return {
        "model": response.model,
        "prompt_tokens": response.prompt_tokens,
        "completion_tokens": response.completion_tokens,
        "total_tokens": response.total_tokens,
        "cost": response.cost,
    }


def _empty_result(new_text: str, llm_usage: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "reasoning": "",
        "status": "no_update",
        "updates": {},
        "unresolved": [],
        "resolved_conflicts": [],
        "resolved_unresolved_ids": [],
        "remaining_text": new_text,
        "blocks": {},
        # None (not []) -- a total call failure produced no judgment on the
        # queue at all, so the caller must leave the persisted queue
        # untouched rather than reading this as "genuinely nothing left to
        # ask". See _validate_queue / analysis_orchestrator._run_batch.
        "queue": None,
        "more_items_asked": [],
        "_llm_usage": llm_usage,
    }


def _validate_queue(returned: Any, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keeps only entries whose `key` names one of the given `candidates`
    and whose `question` is a non-empty string, drops duplicates, and
    re-sorts to `candidates`' own given order -- this re-sort is the actual
    enforcement of "Python validates the order", not just containment. A
    hallucinated/malformed/omitted entry is simply left out (the candidate
    it would have named just doesn't appear in the queue this cycle)."""
    picked: Dict[str, str] = {}
    if isinstance(returned, list):
        for entry in returned:
            if not isinstance(entry, dict):
                continue
            key = entry.get("key")
            question = entry.get("question")
            if not isinstance(key, str) or key in picked:
                continue
            if not isinstance(question, str) or not question.strip():
                continue
            picked[key] = question.strip()

    ordered: List[Dict[str, Any]] = []
    for candidate in candidates:
        key = candidate["key"]
        if key not in picked:
            continue
        ordered.append({
            "kind": candidate["kind"],
            "key": key,
            "block": candidate.get("block"),
            "item_id": candidate.get("item_id"),
            "fields": candidate.get("fields"),
            "question": picked[key],
        })
    return ordered


def _validate_more_items_asked(returned: Any) -> List[str]:
    """Keeps only non-empty string block names, deduped -- the trust
    boundary for the model's self-reported more_items_asked list, mirroring
    _validate_queue's containment-and-cleanup approach."""
    if not isinstance(returned, list):
        return []
    seen: List[str] = []
    for entry in returned:
        if isinstance(entry, str) and entry.strip() and entry not in seen:
            seen.append(entry)
    return seen


async def run_combined_chain(
    resume: Dict[str, Any],
    coverage: Dict[str, Any],
    to_judge: Dict[str, Any],
    candidate_queue: List[Dict[str, Any]],
    new_text: str,
    *,
    last_asked_question: Optional[str] = None,
    more_items_checked: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """The ONE background LLM call per ~100-char buffer trigger: extracts any
    resume facts the excerpt supports, grades whatever `to_judge` currently
    needs a fresh completeness verdict, and words the upcoming spoken
    question for every still-open entry of `candidate_queue`.

    `coverage` must be the FULL COVERAGE_SCHEMA (not askable-only) -- `to_judge`
    (built by the caller via `completeness_status.prune_for_judgment`) can
    legitimately include `personal`/`summary`, which are graded for
    completeness even though they're never asked about through a spoken
    question. `candidate_queue` (from `next_target.compute_candidate_queue`,
    always called with `ASKABLE_COVERAGE_SCHEMA`) already carries each
    candidate's own `complete_when` baked in, so this call never needs a
    second coverage lookup for wording.

    Fail-soft: any provider/schema error returns `_empty_result()`, whose
    `queue` is `None` -- distinct from `[]` -- so the caller can tell "this
    cycle produced no judgment at all, leave the persisted queue untouched"
    apart from "the call succeeded and genuinely nothing is left to ask".

    `last_asked_question` (the full text, ack included, of the most
    recently spoken question) and `more_items_checked` (block names already
    given their one-time "any other X?" side-question) are purely
    prompt-steering context for JOB 3's wording rules -- neither affects
    extraction or completeness grading. `more_items_asked` in the return
    value is the model's self-reported list of block names it appended that
    side-question to this cycle, sanitized by `_validate_more_items_asked`;
    the caller persists it via `crud.mark_more_items_checked` so it isn't
    asked again.
    """
    if not new_text.strip():
        return _empty_result(new_text, None)

    provider = _get_provider()
    messages = [
        LLMMessage(role="system", content=SYSTEM_PROMPT),
        LLMMessage(
            role="user",
            content=build_combined_user_prompt(
                resume, coverage, to_judge, candidate_queue, new_text,
                last_asked_question=last_asked_question,
                more_items_checked=more_items_checked or [],
            ),
        ),
    ]
    options = LLMRequestOptions(max_output_tokens=settings.resume_room_combined_max_tokens)

    try:
        parsed, response = await provider.generate_json(messages, COMBINED_RESPONSE_SCHEMA, options)
    except LLMSchemaValidationError as exc:
        return _empty_result(new_text, _usage_dict(exc.last_response) if exc.last_response else None)
    except LLMProviderError:
        return _empty_result(new_text, None)

    parsed.setdefault("unresolved", [])
    parsed.setdefault("resolved_conflicts", [])
    parsed.setdefault("resolved_unresolved_ids", [])
    parsed.setdefault("blocks", {})
    parsed["queue"] = _validate_queue(parsed.get("queue"), candidate_queue)
    parsed["more_items_asked"] = _validate_more_items_asked(parsed.get("more_items_asked"))
    parsed["_llm_usage"] = _usage_dict(response)
    return parsed
