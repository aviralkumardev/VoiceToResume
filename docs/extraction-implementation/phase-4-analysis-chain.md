# Phase 4 — The extraction LLM call

## What this does

Calls the LLM with the phase-3 prompt and gets back a schema-validated JSON
result, including token/cost usage for the running `llm_cost` accumulator
(phase 6).

**Extension**: `EXTRACTION_RESPONSE_SCHEMA` gains three new optional
properties (`unresolved`, `resolved_conflicts`, `resolved_unresolved_ids`),
and a second chain function, `run_resume_final_resolution_chain`, is added
for the session-end final pass, with its own loose response schema.

## How this uses `provider.generate_json()`

`app/services/llm_providers/` (this repo's existing general-purpose LLM
provider abstraction) has a `JSONGenerating.generate_json()` method that
requests structured JSON output and runs its own schema-validation repair
loop — this module calls it directly.

`generate_json()` now returns a `tuple[dict[str, Any], LLMResponse]`: the
parsed, schema-validated dict alongside an `LLMResponse` whose
`.cost`/`.prompt_tokens`/etc. are summed across every repair attempt the
call made internally (`openrouter_provider/provider.py`'s
`OpenRouterProvider.generate_json` and `_merge_attempt_usage`). On exhausted
repair failure it raises `LLMSchemaValidationError`, which also carries a
`last_response` with the same summed usage — so cost is recoverable even
when every attempt failed schema validation.

## File to create

### `app/meeting_room/resume_analysis_pipeline/analysis_chain.py`

```python
"""LLM calls for the resume-extraction pipeline: one per incremental batch,
plus one for the session-end final resolution pass.

Calls OpenRouterProvider.generate_json() directly — it returns both the
schema-validated dict and an LLMResponse with summed usage across any repair
attempts, so the LLMResponse (and its .cost/.prompt_tokens/etc.) survives for
cost accounting in crud.py's llm_cost accumulator, including on a failed
extraction via LLMSchemaValidationError.last_response.
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

from loguru import logger

import app.services.llm_providers.openrouter_provider  # noqa: F401 - registers "openrouter"
from app.core.config import settings
from app.services.llm_providers import (
    LLMMessage,
    LLMProvider,
    LLMProviderError,
    LLMProviderFactory,
    LLMRequestOptions,
    LLMResponse,
    LLMSchemaValidationError,
    SchemaSpec,
)
from app.meeting_room.resume_analysis_pipeline.analysis_prompts import (
    EXTRACTION_SYSTEM_PROMPT,
    FINAL_RESOLUTION_SYSTEM_PROMPT,
    build_extraction_user_prompt,
    build_final_resolution_user_prompt,
)

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
            "status": {"type": "string", "enum": ["extracted", "no_update"]},
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
        cache[cache_key] = LLMProviderFactory.create(provider_name, model=model, temperature=0.0)
    return cache[cache_key]


def _get_extraction_provider() -> LLMProvider:
    return _get_provider(
        _extraction_provider_cache,
        settings.resume_room_extraction_provider,
        settings.resume_room_extraction_model,
    )


def _get_final_pass_provider() -> LLMProvider:
    return _get_provider(
        _final_pass_provider_cache,
        settings.resume_room_final_pass_provider,
        settings.resume_room_final_pass_model,
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
    """Runs one extraction batch. Never raises — on any failure it returns a
    no_update result carrying the whole input back as remaining_text, so the
    orchestrator's caller doesn't lose the unprocessed text."""
    provider = _get_extraction_provider()
    messages = [
        LLMMessage(role="system", content=EXTRACTION_SYSTEM_PROMPT),
        LLMMessage(role="user", content=build_extraction_user_prompt(resume, new_text)),
    ]
    options = LLMRequestOptions(max_output_tokens=settings.resume_room_extraction_max_tokens)

    try:
        parsed, response = await provider.generate_json(messages, EXTRACTION_RESPONSE_SCHEMA, options)
    except LLMSchemaValidationError as exc:
        logger.warning(f"RESUME-EXTRACTION: schema validation failed: {exc.validation_errors}")
        return {
            "reasoning": "", "updates": {}, "unresolved": [], "resolved_conflicts": [],
            "resolved_unresolved_ids": [], "remaining_text": new_text,
            "status": "no_update",
            "_llm_usage": _usage_dict(exc.last_response) if exc.last_response else None,
        }
    except LLMProviderError as exc:
        logger.warning(f"RESUME-EXTRACTION: provider call failed: {exc}")
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
    """Runs the single session-end final-resolution pass. Never raises — on
    any failure it returns an empty-updates result, so the caller just skips
    applying anything rather than crashing session teardown."""
    provider = _get_final_pass_provider()
    messages = [
        LLMMessage(role="system", content=FINAL_RESOLUTION_SYSTEM_PROMPT),
        LLMMessage(role="user", content=build_final_resolution_user_prompt(resume, full_transcript)),
    ]
    options = LLMRequestOptions(max_output_tokens=settings.resume_room_final_pass_max_tokens)

    try:
        parsed, response = await provider.generate_json(messages, FINAL_RESOLUTION_RESPONSE_SCHEMA, options)
    except LLMSchemaValidationError as exc:
        logger.warning(f"RESUME-FINAL-PASS: schema validation failed: {exc.validation_errors}")
        return {
            "reasoning": "", "updates": {},
            "_llm_usage": _usage_dict(exc.last_response) if exc.last_response else None,
        }
    except LLMProviderError as exc:
        logger.warning(f"RESUME-FINAL-PASS: provider call failed: {exc}")
        return {"reasoning": "", "updates": {}, "_llm_usage": None}

    parsed["_llm_usage"] = _usage_dict(response)
    return parsed
```

## Key design points, explained

- **Provider construction**: `import app.services.llm_providers.openrouter_provider`
  is kept purely for its import-time side effect — it calls
  `LLMProviderFactory.register("openrouter", OpenRouterProvider)`. Without
  this import, `LLMProviderFactory.create("openrouter", ...)` would raise
  `ValueError: Unknown LLM provider 'openrouter'`. The `# noqa: F401` marks
  the "unused" import as intentional for linters.
- **`temperature=0.0` set at provider construction**, not per-call — confirmed
  from `OpenRouterProvider.__init__(*, model=None, temperature=None, ...)`
  (`openrouter_provider/provider.py:34-46`) that this is a valid constructor
  kwarg, and `_build_request_kwargs` falls back to `self.temperature` when a
  call's `LLMRequestOptions.temperature` is `None` — so setting it once here
  is sufficient, no need to repeat it in every `LLMRequestOptions`.
- **Two separate provider caches** (`_extraction_provider_cache`,
  `_final_pass_provider_cache`), both keyed by `(provider_name, model)` and
  built through the same `_get_provider` helper — this avoids re-constructing
  an `AsyncOpenAI` client (and its connection pool) on every call, for either
  the frequent incremental batches or the one-off final pass, while letting
  the final pass use a different provider/model/token-limit (phase 7)
  without the two ever colliding in cache.
- **`EXTRACTION_RESPONSE_SCHEMA` is loose** (`"updates": {"type": "object"}`,
  no `additionalProperties: false`) — the JSON-schema check here only
  confirms the outer envelope shape. The real per-field tolerant validation
  happens in phase 2's `merge_updates`, exactly like pitch_room splits this
  same responsibility between a loose envelope check and a stricter
  merge-time check. The three new keys (`unresolved`, `resolved_conflicts`,
  `resolved_unresolved_ids`) are typed as bare arrays for the same reason —
  phase 2's `merge_unresolved`/`apply_resolved_conflicts`/`remove_unresolved`
  do the real per-entry validation.
- **`run_resume_extraction_chain` backfills the three new keys with
  `setdefault`** so callers (phase 5) never have to guard against a missing
  key — a batch where the LLM has nothing to resolve just omits them, and
  they normalize to empty lists here rather than at every call site.
- **Failure handling never raises out of either chain function**: both a
  schema-validation failure and any other provider error (rate limit,
  timeout, auth, etc. — all subclass `LLMProviderError`) are caught and
  turned into a safe empty/no-op result. For the incremental chain, this
  means the *entire* input is carried forward as `remaining_text` so it's
  retried on the next batch, mirroring pitch_room's fallback behavior on
  exhausted repair retries. For the final-resolution chain, a failure just
  means the final pass applies nothing — the resume data stays exactly as it
  was from the incremental batches (conflicts/unresolved will still get
  cleared by `apply_final_resolution` in phase 6, since that reset is
  unconditional regardless of what the final pass's `updates` contained).
- **`run_resume_final_resolution_chain` has no `remaining_text`/`status`
  concept** — the final pass isn't part of the incremental buffer loop, it's
  a single terminal call, so there's nothing to carry forward or gate on.
- **`LLMSchemaValidationError.last_response` still gets folded into `_llm_usage`
  on the `no_update`/empty-updates failure path** — every repair attempt
  `generate_json()` makes is a separately billed call even when the final
  one never validates, so `exc.last_response` (summed across all of them)
  is used instead of dropping usage on the floor. The generic
  `LLMProviderError` path (rate limit, timeout, auth, etc.) still reports
  `_llm_usage: None`, since that exception carries no response at all — this
  only matters if such an error interrupts an already-running repair loop,
  which is rare enough not to warrant carrying usage on that exception too.
