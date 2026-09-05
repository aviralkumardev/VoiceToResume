# Phase 3 — Prompt Changes

## What this does

Extends the existing single completeness-grading call to also ask for a
question, when the backend (`phase-2`) has picked a target:

- `build_completeness_user_prompt` gains two optional params —
  `question_target` (the `{target_type, target_path, complete_when}` dict
  `select_focus_target` produced) and `resume` (needed to build grounding
  context for the target) — and, when `question_target` is given, adds a
  `question_target` key to the JSON payload sent to the LLM, with a
  `context` sub-key: whatever's already known about that same block/item,
  so the wording can be specific rather than generic.
- `SYSTEM_PROMPT` gains one new paragraph telling the LLM its second job:
  write a `question` string when `question_target` is present. The LLM
  never chooses `target_type`/`target_path` — those are already fixed by
  the backend; it only supplies the wording.

## File to modify: `backend/app/meeting_room/resume_analysis_pipeline/completeness_prompts.py`

Current file (for reference — this is what exists today):

```python
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

**Change 1** — `Optional` import:

```python
import json
from typing import Any, Dict, Optional
```

**Change 2** — append a new paragraph to `SYSTEM_PROMPT`, right before the
closing `"""`:

```python
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
you, never on assumptions about what a "typical" candidate would have.

If the input also includes a `question_target` object ({target_type,
target_path, complete_when, context}), you have one more job: write a
top-level `question` string -- ONE concise, conversational follow-up
question, suitable to be spoken aloud in a live voice interview, aimed
specifically at getting the information described by
question_target.complete_when. You do NOT choose target_type or
target_path -- those are already fixed by the caller; you only write the
wording. Use question_target.context (whatever is already known about that
same block/item, if anything) to make the question feel natural and
specific rather than generic. If target_type is "BLOCK", ask one broad
opening question about that whole topic (e.g. "Tell me about your work
experience -- where, what was your role, and what did you do?"), not a
checklist of every field inside it. If target_type is "FIELD", ask
specifically for what's missing for that one detail, and don't re-ask
about things `context` already shows are known. Return `question: null`
only if `question_target` is absent from the input, or in the rare case
nothing sensible can be asked from what you were given."""
```

**Change 3** — new helper, placed right before `build_completeness_user_prompt`:

```python
def _target_context(target_path: str, resume: Dict[str, Any]) -> Dict[str, Any]:
    """Whatever's already known about the same block/item as `target_path`,
    minus the target field itself -- grounds the LLM's question wording
    without asking it to re-derive anything from raw resume_data shape."""
    parts = target_path.split(".")
    block = parts[0]

    if len(parts) == 1:
        return {}

    if len(parts) == 2:
        container = resume.get(block) or {}
        exclude = parts[1]
    else:
        item_id = parts[1]
        exclude = parts[2]
        container = next(
            (item for item in (resume.get(block) or []) if item.get("id") == item_id),
            {},
        )

    context: Dict[str, Any] = {}
    for key, value in container.items():
        if key in (exclude, "id"):
            continue
        if isinstance(value, dict) and value.get("value"):
            context[key] = value["value"]
        elif isinstance(value, list) and value:
            context[key] = value
    return context
```

**Change 4** — `build_completeness_user_prompt` gains the two new params
and the `question_target` payload key:

```python
def build_completeness_user_prompt(
    to_judge: Dict[str, Any],
    coverage: Dict[str, Any],
    question_target: Optional[Dict[str, Any]] = None,
    resume: Optional[Dict[str, Any]] = None,
) -> str:
    rubric = {block: coverage[block] for block in to_judge if block in coverage}
    payload: Dict[str, Any] = {"rubric": rubric, "blocks": to_judge}

    if question_target is not None:
        target_block = question_target["target_path"].split(".")[0]
        if target_block in coverage and target_block not in rubric:
            rubric[target_block] = coverage[target_block]
        payload["question_target"] = {
            **question_target,
            "context": _target_context(question_target["target_path"], resume or {}),
        }

    return (
        "Grade the following blocks against their rubric entries. Return a "
        "verdict only for the keys listed under fields_to_judge/items_to_judge/"
        "value in each block, plus that block's own top-level verdict. If "
        "question_target is present, also return a top-level `question` "
        "string as instructed.\n\n"
        f"{json.dumps(payload, indent=2, ensure_ascii=False)}"
    )
```

## Key design points, explained

- **`question_target`'s block gets added to `rubric` even when it's not
  in `to_judge`.** This matters for the whole-block-MISSING case: a
  completely empty block never appears in `to_judge` at all (it's
  code-decided, per `phase-0`/the original `phase-1`), so without this the
  LLM would be asked to write a question about a block's `complete_when`
  bar it was never shown. `rubric` already exists purely to carry
  `complete_when`/`importance` context (see the original
  `completeness_status.md`), so folding the target's block into it is the
  natural extension, not a new payload shape.
- **`_target_context` intentionally excludes the target field/item id
  itself** — showing the LLM the value it's trying to elicit back to
  itself as "context" would be nonsensical (and for a MISSING field, there
  is no value anyway). `"id"` is also always excluded since it's an
  internal bookkeeping key, never something worth mentioning in a spoken
  question.
- **For a whole-block `BLOCK` target, `_target_context` returns `{}`
  unconditionally** (the `len(parts) == 1` branch) — an entirely empty
  block has nothing to ground the question in anyway, so there's no point
  descending into `resume` at all.
- **The LLM is never asked to validate or echo back `target_path`** — the
  prompt spreads `question_target` (including `target_path`) into the
  payload only so the LLM has the full picture (and can sanity-check its
  own wording against `complete_when`), but the response schema
  (`phase-4`) only asks it to return a `question` string, not the whole
  target object. This is what makes the backend's ownership of
  `target_type`/`target_path` (`phase-0`'s core decision) actually hold —
  there's no LLM-authored path to validate or reject.
