# Phase 4 — The LLM Call Itself

## What this does

Three small, mechanical changes to the one function that actually calls
the LLM:

1. `COMPLETENESS_RESPONSE_SCHEMA` gains a `"question"` property.
2. `run_completeness_chain` gains `question_target`/`resume` params, threads
   them into `build_completeness_user_prompt` (`phase-3`), and includes
   `"question": None` in every fallback/early-return shape so callers never
   have to guess whether the key is present.
3. **The empty-`to_judge` guard changes** from `if not to_judge` to
   `if not to_judge and question_target is None` — this is the fix for the
   `phase-0` bug: a session where every block is still completely `MISSING`
   (nothing to verdict) must still fire the call when there's a question
   target, or a block-level question could never be generated on session
   start.

## File to modify: `backend/app/meeting_room/resume_analysis_pipeline/completeness_chain.py`

Current file (for reference — this is what exists today):

```python
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
            "blocks": {"type": "object"}
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


async def run_completeness_chain(to_judge: Dict[str, Any], coverage: Dict[str, Any]) -> Dict[str, Any]:
    if not to_judge:
        return {"reasoning": "", "blocks": {}, "_llm_usage": None}


    provider = _get_completeness_provider()
    messages = [
        LLMMessage(role="system", content=SYSTEM_PROMPT),
        LLMMessage(role="user", content=build_completeness_user_prompt(to_judge, coverage))
    ]

    options = LLMRequestOptions(max_output_tokens=settings.resume_room_completeness_max_tokens)

    try:
        parsed, response = await provider.generate_json(messages, COMPLETENESS_RESPONSE_SCHEMA, options)
    except LLMSchemaValidationError as exc:
        return {
            "reasoning": "", "blocks": {},
            "_llm_usage": _usage_dict(exc.last_response) if exc.last_response else None,
        }
    except LLMProviderError as exc:
        return {"reasoning": "", "blocks": {}, "_llm_usage": None}

    parsed.setdefault("blocks", {})
    parsed["_llm_usage"] = _usage_dict(response)
    return parsed
```

**Change 1** — `Optional` import:

```python
from typing import Any, Dict, Optional, Tuple
```

**Change 2** — response schema gains `question`:

```python
COMPLETENESS_RESPONSE_SCHEMA = SchemaSpec.from_dict(
    {
        "type": "object",
        "properties": {
            "reasoning": {"type": "string"},
            "blocks": {"type": "object"},
            "question": {"type": ["string", "null"]}
        },
        "required": ["blocks"]
    },
    name="completeness_result",
    strict = False
)
```

**Change 3** — `run_completeness_chain`'s signature, guard, prompt call,
and both fallback returns:

```python
async def run_completeness_chain(
    to_judge: Dict[str, Any],
    coverage: Dict[str, Any],
    question_target: Optional[Dict[str, Any]] = None,
    resume: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if not to_judge and question_target is None:
        return {"reasoning": "", "blocks": {}, "question": None, "_llm_usage": None}


    provider = _get_completeness_provider()
    messages = [
        LLMMessage(role="system", content=SYSTEM_PROMPT),
        LLMMessage(
            role="user",
            content=build_completeness_user_prompt(to_judge, coverage, question_target, resume),
        )
    ]

    options = LLMRequestOptions(max_output_tokens=settings.resume_room_completeness_max_tokens)

    try:
        parsed, response = await provider.generate_json(messages, COMPLETENESS_RESPONSE_SCHEMA, options)
    except LLMSchemaValidationError as exc:
        return {
            "reasoning": "", "blocks": {}, "question": None,
            "_llm_usage": _usage_dict(exc.last_response) if exc.last_response else None,
        }
    except LLMProviderError as exc:
        return {"reasoning": "", "blocks": {}, "question": None, "_llm_usage": None}

    parsed.setdefault("blocks", {})
    parsed.setdefault("question", None)
    parsed["_llm_usage"] = _usage_dict(response)
    return parsed
```

Everything else in the file (`_get_provider`, `_get_completeness_provider`,
`_usage_dict`, `_completeness_provider_cache`) is untouched.

## Key design points, explained

- **Fail-soft posture is preserved exactly**: every early-return/exception
  path now also carries `"question": None`, so `phase-6`'s worker never
  needs a `.get("question")` default-guard — the key is always present on
  every possible return shape from this function, matching how `"blocks"`
  already works via `parsed.setdefault("blocks", {})`.
- **The guard change is the one behavior-affecting line in this phase.**
  Before: a round with nothing to verdict skipped the network call
  entirely. After: it still skips when there's *also* no question target
  (a fully `SUFFICIENT` resume, `phase-2`'s `select_focus_target` returned
  `None`) — the no-op case is unchanged — but no longer skips just because
  `to_judge` alone is empty. This is what makes the very first silence
  cycle of a session (resume completely empty, `to_judge == {}`,
  `question_target` = the highest-priority empty block) actually produce a
  question.
- **`question_target`/`resume` are optional with `None` defaults** so
  every other caller of `run_completeness_chain` (there are none yet
  outside `phase-6`'s worker, but this keeps the function usable
  standalone/in tests without always constructing a target) keeps working
  unchanged.
