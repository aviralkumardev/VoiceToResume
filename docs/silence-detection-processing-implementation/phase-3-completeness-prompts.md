# Phase 3 — Completeness Prompts

## What this does

Builds the system and user prompts for the single batched completeness
call. Follows the same split as the existing extraction/final-resolution
chains: a static `SYSTEM_PROMPT` constant plus a `build_completeness_user_prompt(...)`
function that renders the per-run payload.

**Only the rubric entries for blocks/fields actually present in `to_judge`
are rendered** — not the full static `COVERAGE_SCHEMA` every time. This is
the same "don't spend tokens on things that don't need judging" reasoning
already applied to `MISSING` fields; a block excluded from `to_judge`
(carried forward whole) needs no rubric text sent alongside it.

The prompt is explicit that `already_sufficient` / `items_context` /
`missing_fields` are **read-only context**, never something to produce a
fresh verdict for — this is what keeps `merge_completeness` (`phase-2`)
safe: the LLM is only ever asked to fill in exactly the keys named under
`fields_to_judge` / `items_to_judge` / `value`.

## New file: `backend/app/meeting_room/resume_analysis_pipeline/completeness_prompts.py`

```python
from __future__ import annotations

import json
from typing import Any, Dict


SYSTEM_PROMPT = """You are grading how complete a candidate's resume information is, \
block by block, against a fixed rubric -- not extracting or rewriting anything.

For every block in the input, you will be given:
- `complete_when` (and per-field/per-item `complete_when` where relevant): the bar
  that must be met for a SUFFICIENT verdict, and `importance`
  (required/recommended/optional) for context on how much a gap should weigh
  against the block's own aggregate verdict.
- `fields_to_judge` / `items_to_judge` (or a bare `value` for list-type blocks):
  the ONLY things you must produce a fresh verdict for.
- `already_sufficient` / `items_context`: prior information already judged
  SUFFICIENT, shown ONLY so you can judge the block's own aggregate bar with
  the full picture. Do NOT produce a verdict for anything listed here.
- `missing_fields`: field names with no value at all right now. Do NOT produce
  a verdict for these either -- factor their absence into the block's own
  aggregate verdict only (a block missing a `required` field cannot be
  SUFFICIENT overall, even if everything else you were asked to judge looks good).

For every block you are given, respond with exactly one of two verdicts:
- SUFFICIENT: the `complete_when` bar is clearly met.
- PARTIAL: there is some relevant content, but the bar is not yet met.
Never respond MISSING or NOT_APPLICABLE -- those are decided outside this call.

Your response's `fields`/`items` keys must exactly mirror the `fields_to_judge`/
`items_to_judge` keys you were given for that block -- no more, no fewer. Every
block you were given also needs its own top-level completeness_status/reason/
confidence, judging that block's own complete_when bar as a whole.

Be strict but fair: base every verdict only on the content actually given to
you, never on assumptions about what a "typical" candidate would have."""


def build_completeness_user_prompt(to_judge: Dict[str, Any], coverage: Dict[str, Any]) -> str:
    rubric = {block: coverage[block] for block in to_judge if block in coverage}
    payload = {"rubric": rubric, "blocks": to_judge}
    return (
        "Grade the following blocks against their rubric entries. Return a "
        "verdict only for the keys listed under fields_to_judge/items_to_judge/"
        "value in each block, plus that block's own top-level verdict.\n\n"
        f"{json.dumps(payload, indent=2, ensure_ascii=False)}"
    )
```

## Key design points, explained

- **The rubric is nested inside the same user-prompt JSON blob as the
  payload**, rather than sent as a separate message — keeps the "grade
  exactly this against exactly that" relationship unambiguous for the model,
  and matches the single-JSON-blob style `analysis_prompts.py` already uses
  for the extraction/final-resolution chains.
- **`rubric` is built by filtering `coverage` down to `to_judge`'s own
  keys** — a one-line dict comprehension, not a separate pruning function,
  since `to_judge`'s keys are already exactly the blocks that need rubric
  text.
- **The system prompt never mentions `MISSING`** as a producible verdict —
  it's stated as always decided outside the call, so there's no ambiguity
  about whether an LLM-emitted `MISSING` should ever be trusted (it
  shouldn't; `phase-4`'s response schema doesn't even allow it as an enum
  value for `completeness_status`).
