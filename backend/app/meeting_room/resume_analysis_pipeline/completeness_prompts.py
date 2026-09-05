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


def build_completeness_user_prompt(
    to_judge: Dict[str, Any],
    coverage: Dict[str, Any],
) -> str:
    rubric = {block: coverage[block] for block in to_judge if block in coverage}
    payload: Dict[str, Any] = {"rubric": rubric, "blocks": to_judge}

    return (
        "Grade the following blocks against their rubric entries. Return a "
        "verdict only for the keys listed under fields_to_judge/items_to_judge/"
        "value in each block, plus that block's own top-level verdict.\n\n"
        f"{json.dumps(payload, indent=2, ensure_ascii=False)}"
    )


