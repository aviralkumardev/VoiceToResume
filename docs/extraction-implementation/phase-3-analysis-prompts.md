# Phase 3 — Prompt construction

## What this does

Builds the system prompt and per-batch user prompt sent to the extraction
LLM: the schema (from phase 1), a sparse view of resume facts already
captured, and the new excerpt of candidate speech.

Unlike pitch_room's prompt, this one doesn't need any "which speaker's lines
count as evidence" disambiguation, since only candidate speech is ever queued
into the buffer (see phase 8 for where that's enforced).

**Extension**: every incremental batch's prompt now also shows the currently
outstanding `conflicts` and `unresolved` records (with their ids), and the
system prompt instructs the LLM on how to resolve them inline via two new
response keys (`resolved_conflicts`, `resolved_unresolved_ids`), in addition
to normal extraction. A second prompt builder,
`build_final_resolution_user_prompt`, is added for the session-end final
pass — it gets the *entire* candidate transcript plus the full (not sparse)
`resume_data`, and asks for one definitive resolution of everything still
outstanding.

## File to create

### `app/meeting_room/resume_analysis_pipeline/analysis_prompts.py`

```python
"""Prompt construction for the resume-extraction LLM call."""

from __future__ import annotations

import json
from typing import Any, Dict

from app.meeting_room.resume_analysis_pipeline.config_jsons_definitions.resume_schema import (
    render_schema_for_prompt,
)

EXTRACTION_SYSTEM_PROMPT = """You extract resume facts from a candidate's own spoken words during a live \
mock interview. You will be given the resume schema, the resume data already \
captured so far, any outstanding conflicts/unresolved fragments, and a new \
excerpt of the candidate's speech.

Rules:
- Only extract facts the excerpt actually supports. Do not guess or invent.
- For list-of-object blocks (experience, education, projects, certifications, \
courses): each item in CURRENT RESUME STATE shows an "id". If this excerpt \
adds detail to an item already shown there, include that item's "id" in your \
update so it gets merged into the same item. If this excerpt describes a \
role/degree/project/certification/course not already shown, omit "id" (or \
use one that doesn't match) so a new item gets created.
- If a fact clearly belongs to SOME existing item in a list-of-object block \
but you are not confident WHICH one (e.g. the candidate has two roles at the \
same company and it's unclear which role this refers to), do NOT guess an \
id. Instead report it in "unresolved" as {"block": "...", "text": "...", \
"note": "..."} describing why it's ambiguous, and leave it out of "updates".
- For list-of-object array fields (e.g. experience[].responsibilities), only \
include NEWLY mentioned items in this excerpt — do not repeat items already \
visible in CURRENT RESUME STATE, they are kept automatically.
- For plain list blocks (skills, achievements, awards, languages, \
additional_information), only include NEWLY mentioned items, not the full list.
- OUTSTANDING CONFLICTS shows fields where a prior excerpt's value disagreed \
with an earlier one, so neither was applied. If THIS excerpt clarifies which \
value is correct, report it in "resolved_conflicts" as {"id": "<conflict \
id>", "value": "<the correct value>"}. Do not also repeat that field in \
"updates" — resolving it is enough.
- OUTSTANDING UNRESOLVED shows facts from a prior excerpt that couldn't be \
confidently attributed to an item. If THIS excerpt clarifies which item one \
of them belongs to, include a normal update in "updates" with the correct \
item "id" AND list that item's id in "resolved_unresolved_ids".
- If nothing in the excerpt supports any update, resolution, or new \
unresolved fact, set "status" to "no_update" and leave "updates" empty.
- If the excerpt ends mid-sentence, put the trailing incomplete fragment in \
"remaining_text" so it can be combined with the next excerpt. Otherwise \
leave "remaining_text" empty.
- Return ONLY JSON. No prose, no markdown code fences.
"""


def _render_resume_state(resume: Dict[str, Any]) -> str:
    populated = {
        block: content
        for block, content in resume.items()
        if block not in ("conflicts", "unresolved") and content
    }
    return json.dumps(populated, indent=2)


def _render_conflicts(resume: Dict[str, Any]) -> str:
    conflicts = resume.get("conflicts") or []
    if not conflicts:
        return "(none)"
    return json.dumps(conflicts, indent=2)


def _render_unresolved(resume: Dict[str, Any]) -> str:
    unresolved = resume.get("unresolved") or []
    if not unresolved:
        return "(none)"
    return json.dumps(unresolved, indent=2)


def build_extraction_user_prompt(resume: Dict[str, Any], new_text: str) -> str:
    return f"""RESUME SCHEMA — the only valid block/field keys:
{render_schema_for_prompt()}

CURRENT RESUME STATE (only populated blocks are shown; list-of-object items show their "id"):
{_render_resume_state(resume)}

OUTSTANDING CONFLICTS (fields where an earlier value disagreed with a later one — resolve via "resolved_conflicts" if this excerpt clarifies one):
{_render_conflicts(resume)}

OUTSTANDING UNRESOLVED (facts not yet attributed to a specific item — resolve via a normal update + "resolved_unresolved_ids" if this excerpt clarifies one):
{_render_unresolved(resume)}

NEW CANDIDATE SPEECH (this excerpt is entirely the candidate's own words — no \
interviewer speech is ever included here):
{new_text}

Extract any resume facts this excerpt supports, and resolve any outstanding \
conflicts/unresolved items it clarifies. Return JSON only, matching:
{{"reasoning": "...", "updates": {{...}}, "unresolved": [...], \
"resolved_conflicts": [...], "resolved_unresolved_ids": [...], \
"remaining_text": "...", "status": "extracted"|"no_update"}}
"""


FINAL_RESOLUTION_SYSTEM_PROMPT = """You are doing a final, one-time pass over a candidate's ENTIRE mock-interview \
transcript, at the end of the session. You will be given the resume schema, \
the full resume data captured so far (including every outstanding conflict \
and unresolved fragment), and the candidate's complete transcript.

Your job: re-derive and correct the resume using the complete context now \
available. Specifically:
- For every entry in OUTSTANDING CONFLICTS, decide the correct final value \
using the full transcript and include it in "updates" for that exact \
block/field/item — this pass force-overwrites, so just state the correct \
value directly, no need to reference the conflict's id.
- For every entry in OUTSTANDING UNRESOLVED, decide which existing item (or \
a new one) it belongs to using the full transcript, and include it in \
"updates" with the correct "id" (or a new item if genuinely new).
- Also capture anything else the full transcript supports that earlier \
partial excerpts may have missed.
- If a conflict or unresolved fragment genuinely cannot be resolved even \
with the full transcript, leave it out of "updates" — it will be cleared \
regardless, since this is the last chance to resolve it.
- Return ONLY JSON. No prose, no markdown code fences.
"""


def build_final_resolution_user_prompt(resume: Dict[str, Any], full_transcript: str) -> str:
    full_resume = {k: v for k, v in resume.items()}
    return f"""RESUME SCHEMA — the only valid block/field keys:
{render_schema_for_prompt()}

FULL RESUME DATA CAPTURED SO FAR (including every block, even empty ones, and every outstanding conflict/unresolved item):
{json.dumps(full_resume, indent=2)}

COMPLETE CANDIDATE TRANSCRIPT (every line the candidate spoke this session, in order):
{full_transcript}

Re-derive and correct the resume using this complete context. Return JSON only, matching:
{{"reasoning": "...", "updates": {{...}}}}
"""
```

## Key design points, explained

- **`_render_resume_state` drops empty blocks/lists** with a single
  `if content` filter — this correctly skips both `{}` (empty singular block)
  and `[]` (empty list block), matching pitch_room's token-saving trick of
  never sending 12 empty objects on every batch. It also now explicitly
  excludes `conflicts`/`unresolved` from this rendering — those get their own
  dedicated sections (`_render_conflicts`/`_render_unresolved`) so they're
  visually distinct from ordinary resume facts in the prompt.
- **`EXTRACTION_SYSTEM_PROMPT` is a plain string constant**, not loaded from a
  database. pitch_room's prompts are DB-backed via a `PromptLoader`/
  `prompt_ids` system that doesn't exist in this repo — building one just for
  this would be disproportionate to the task.
- The system prompt explicitly spells out the id-addressing rule for
  list-object blocks and the "only new items" rule for array/list-string
  blocks, since both are load-bearing for phase 2's merge to behave correctly
  (an LLM that doesn't follow these will cause duplicate items or overwritten
  history — phase 2's dedup logic is a safety net, not a substitute for the
  prompt getting this right most of the time).
- **The unresolved-vs-guess instruction** is the prompt-side half of the
  conflict/unresolved extension: the LLM is explicitly told that "ambiguous
  which item" should become an `unresolved` entry, not a guessed `id` —
  guessing wrong would silently corrupt the wrong item, which is exactly what
  this whole mechanism exists to prevent.
- **`_render_conflicts`/`_render_unresolved` return `"(none)"` when empty**
  rather than an empty JSON array — slightly easier for the model to parse as
  "nothing to resolve here" than `[]` sitting in the middle of a prompt.
- **`FINAL_RESOLUTION_SYSTEM_PROMPT` and `build_final_resolution_user_prompt`**
  are a separate prompt pair (not a variant of the incremental one) because
  the final pass has a fundamentally different shape: full (not sparse)
  resume dump, the *entire* transcript instead of one excerpt, force-overwrite
  semantics instead of conflict-diversion, and no `remaining_text`/`status`/
  resolution-id bookkeeping — it's a one-shot "make it all correct now" call,
  not another incremental step.
