# Phase 4 — Completeness LLM Chain

## What this does

The actual network call, following the exact same shape as
`analysis_chain.py`'s `run_resume_extraction_chain`/
`run_resume_final_resolution_chain`: a module-level response `SchemaSpec`,
a `(provider_name, model)`-keyed provider cache plus a `_get_provider`
helper, and a `run_completeness_chain(...)` function with the same
try/except-`LLMSchemaValidationError`-before-`LLMProviderError` fail-soft
shape the rest of the pipeline uses — including calling
`provider.generate_json(...)`, which returns a **`(parsed, response)`
tuple**, not a single response object.

**One difference from the extraction chains**: this one is skipped entirely
if there's nothing to judge. `phase-9`'s worker never calls it with an empty
`to_judge` — but the guard is duplicated here too, defensively, since a
free network call for an empty payload is pure waste and this function may
end up called from more than one place over time.

**Response schema is loose (`from_dict`, not `from_pydantic`)** — same
reasoning as the existing chains' choice for their own dynamic-shaped
responses: the exact set of blocks/fields present in any given response
varies per session and per silence event, so a fixed Pydantic model would
have to be all-Optional everywhere anyway.

## New file: `backend/app/meeting_room/resume_analysis_pipeline/completeness_chain.py`

```python
from typing import Any, Dict, Tuple

import app.services.llm_providers.openrouter_provider
from app.meeting_room.resume_analysis_pipeline.completeness_prompts import (
    SYSTEM_PROMPT,
    build_completeness_user_prompt,
)
from app.services.llm_providers.data_models import LLMMessage, LLMRequestOptions, LLMResponse
from app.services.llm_providers.json_schema_validation import SchemaSpec
from app.services.llm_providers.provider_exceptions import LLMProviderError, LLMSchemaValidationError
from app.services.llm_providers.provider_factory import LLMProviderFactory
from app.services.llm_providers.provider_interface import LLMProvider

from app.core.config import settings

COMPLETENESS_RESPONSE_SCHEMA = SchemaSpec.from_dict(
    {
        "type": "object",
        "properties": {
            "reasoning": {"type": "string"},
            "blocks": {"type": "object"},
        },
        "required": ["blocks"],
    },
    name="completeness_result",
    strict=False,
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
    """Returns a dict with `blocks` (the fresh verdicts, keyed same as
    `to_judge`) and `_llm_usage` (present on success, None on failure or
    when there was nothing to judge). Callers can always merge the result
    the same way regardless of success -- consistent with the extraction/
    final-resolution chains' fail-soft posture.
    """
    if not to_judge:
        return {"reasoning": "", "blocks": {}, "_llm_usage": None}

    provider = _get_completeness_provider()
    messages = [
        LLMMessage(role="system", content=SYSTEM_PROMPT),
        LLMMessage(role="user", content=build_completeness_user_prompt(to_judge, coverage)),
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

## Key design points, explained

- **`provider.generate_json(...)` returns a `(parsed, response)` tuple**,
  not a single object with a `.parsed` attribute — `parsed` is the
  already-validated dict matching `COMPLETENESS_RESPONSE_SCHEMA`, `response`
  is the raw `LLMResponse` carrying `model`/`prompt_tokens`/
  `completion_tokens`/`total_tokens`/`cost` directly as attributes (not
  nested under a `usage` dict). Unpack both, exactly as
  `run_resume_extraction_chain` does.
- **Messages are `LLMMessage(role=..., content=...)` instances**, not plain
  `{"role": ..., "content": ...}` dicts — `LLMMessage` comes from
  `app.services.llm_providers.data_models`, the same module `LLMRequestOptions`
  and `LLMResponse` live in.
- **Import paths matter and are split across several small modules**, not
  one `base` module: `SchemaSpec` is in `json_schema_validation`,
  `LLMProviderError`/`LLMSchemaValidationError` in `provider_exceptions`,
  `LLMProviderFactory` in `provider_factory`, the `LLMProvider` type in
  `provider_interface`. Getting any of these wrong is an `ImportError` at
  startup, not a subtle runtime bug — worth double-checking against
  `analysis_chain.py`'s own import block if anything here looks off after
  transcription.
- **`import app.services.llm_providers.openrouter_provider` for its
  registration side-effect** mirrors exactly how `analysis_chain.py` makes
  sure the `"openrouter"` provider name is registered with
  `LLMProviderFactory` before `.create(...)` is ever called — omitting this
  import is a real, easy-to-hit mistake (the factory raises on an
  unregistered provider name), not a stylistic nicety.
- **The provider cache is keyed by the `(provider_name, model)` tuple**,
  via the same small `_get_provider(cache, provider_name, model)` helper
  `analysis_chain.py` defines — duplicated here rather than imported from
  `analysis_chain.py`, since it's a trivial private helper and the two
  chain modules aren't meant to depend on each other.
- **`SchemaSpec.from_dict(..., name=..., strict=False)`** — the `name`
  argument isn't optional style, `analysis_chain.py` always passes one
  (`"resume_extraction_result"`, `"resume_final_resolution_result"`); this
  chain's schema is named `"completeness_result"` to match that pattern.
- **`run_completeness_chain` always returns a `blocks` key and an
  `_llm_usage` key, even on total failure or an empty `to_judge`** —
  `phase-9`'s worker can always do
  `merge_completeness(already_decided, result.get("blocks", {}), coverage)`
  and `result.get("_llm_usage")` unconditionally, without a separate
  success/failure branch at the call site. A failed or skipped call
  effectively means "nothing new got judged this round" —
  `merge_completeness` with an empty `llm_blocks` just carries
  `already_decided` straight through, which is the correct fail-soft
  behavior (never worse than not having tried).
