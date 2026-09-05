# Phase 2 — Pruning and Merge Logic

## What this does

The pure-Python core of the feature: given the live `resume_data`, the
static `COVERAGE_SCHEMA` (`phase-1`), and whatever `field_completeness` was
stored last time, decide (a) which leaves need a fresh LLM verdict this
round, (b) which leaves are already settled and should just be carried
forward untouched, and (c) how to stitch a fresh LLM response back together
with the carried-forward parts into the new stored state.

No I/O, no `asyncio`, no LLM calls here — this module is deliberately
"just data in, data out" so `phase-9`'s worker and any test can exercise it
without a network.

**Three-way partition per leaf** (field or list-object item-field):
- **MISSING** — no value in `resume_data` at all right now. Decided by
  plain code, current value only — never sent to the LLM, and the previous
  status is irrelevant (the resume is ground truth: if a value that used to
  exist got removed, it's `MISSING` again regardless of what it was judged
  as before).
- **CARRIED** — has a value, and the previously stored verdict for this
  exact leaf was already `SUFFICIENT` — not sent to the LLM again; the old
  verdict object (`completeness_status`/`reason`/`confidence`) is carried
  forward verbatim.
- **TO_JUDGE** — has a value, and the previous verdict was `PARTIAL` or
  doesn't exist yet — sent to the LLM this round.

**Block-level verdicts** follow the same idea, but a block's own aggregate
bar (e.g. "at least one experience is sufficiently covered") can only be
judged correctly by seeing the block's *whole* current picture — so a block
is entirely excluded from the LLM payload (its full stored node carried
forward verbatim) only when its own last verdict was already `SUFFICIENT`
**and** nothing under it is `TO_JUDGE`. Otherwise, it's included, and the
payload for it carries both the leaves that need a fresh verdict and the
already-`SUFFICIENT` leaves as read-only context (so the LLM can judge the
aggregate correctly without being asked to re-verdict them).

**A block that's entirely empty right now** (no items, or every field
`MISSING`) short-circuits straight to block-level `MISSING` — it never
enters the LLM payload at all, same reasoning as a `MISSING` leaf.

**`missing_fields` note in the payload**: since `MISSING` leaves are never
shown to the LLM as values, a block that's missing a `required` field
entirely (e.g. `personal.phone`) would otherwise judge its own aggregate
bar with no idea that field is absent. Whenever a block/item is sent to the
LLM, its currently-missing field names are listed alongside (names only —
the coverage rubric already carries their `importance`/`complete_when`, so
the LLM can cross-reference) so the aggregate verdict can correctly stay
`PARTIAL` while a required field is unaddressed.

**Known limitation** (carried over from `phase-0`): if a candidate edits an
already-`SUFFICIENT` field's value later, it won't be re-validated until
something else in the same block also becomes `TO_JUDGE`. Accepted
tradeoff, not a bug to chase.

## New file: `backend/app/meeting_room/resume_analysis_pipeline/completeness_status.py`

```python
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from app.meeting_room.resume_analysis_pipeline.config_jsons_definitions.resume_schema import (
    block_kind,
    item_array_field_keys,
    item_field_keys,
)

STATUS_MISSING = "MISSING"
STATUS_PARTIAL = "PARTIAL"
STATUS_SUFFICIENT = "SUFFICIENT"
STATUS_NOT_APPLICABLE = "NOT_APPLICABLE"  # reserved -- never produced by this phase

_MISSING_LEAF: Dict[str, Any] = {"completeness_status": STATUS_MISSING, "reason": None, "confidence": None}


def _scalar_value(entry: Any) -> Optional[str]:
    if isinstance(entry, dict) and entry.get("value"):
        return entry["value"]
    return None


def _array_value(entry: Any) -> Optional[List[str]]:
    return entry if isinstance(entry, list) and entry else None


def _is_sufficient(node: Optional[Dict[str, Any]]) -> bool:
    return bool(node) and node.get("completeness_status") == STATUS_SUFFICIENT


def _leaf_verdict(node: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "completeness_status": node.get("completeness_status"),
        "reason": node.get("reason"),
        "confidence": node.get("confidence"),
    }


def prune_for_judgment(
    resume: Dict[str, Any],
    coverage: Dict[str, Any],
    previous_status: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Splits every block covered by `coverage` into:

      - `already_decided`: the parts of the final result that need no LLM
        call this round -- MISSING leaves and CARRIED (already-SUFFICIENT)
        leaves, in final-storage shape. A block that's fully settled or
        fully empty appears here WHOLE and is entirely absent from
        `to_judge`.
      - `to_judge`: the payload for the single batched LLM call -- one
        entry per block that needs any fresh judgment this round.

    Returns (already_decided, to_judge).
    """
    already_decided: Dict[str, Any] = {}
    to_judge: Dict[str, Any] = {}

    for block, spec in coverage.items():
        kind = block_kind(block)
        prev_block = previous_status.get(block)

        if kind == "list_object":
            _prune_list_object_block(resume, block, spec, prev_block, already_decided, to_judge)
        elif "fields" in spec:
            _prune_singular_block(resume, block, spec, prev_block, already_decided, to_judge)
        else:
            _prune_atomic_block(resume, block, kind, prev_block, already_decided, to_judge)

    return already_decided, to_judge


def _prune_atomic_block(resume, block, kind, prev_block, already_decided, to_judge):
    """Blocks with no field breakdown: every list_string block (skills,
    achievements, awards, languages, additional_information)."""
    value = _array_value(resume.get(block))

    if value is None:
        already_decided[block] = dict(_MISSING_LEAF)
        return

    if _is_sufficient(prev_block):
        already_decided[block] = _leaf_verdict(prev_block)
        return

    to_judge[block] = {"value": value}


def _prune_singular_block(resume, block, spec, prev_block, already_decided, to_judge):
    """Blocks with a flat field breakdown: personal, summary."""
    target = resume.get(block) or {}
    prev_fields = (prev_block or {}).get("fields", {})

    if not any(_scalar_value(target.get(field)) for field in spec["fields"]):
        already_decided[block] = dict(_MISSING_LEAF)
        return

    needs_verdict, context_only, decided_fields, missing_names = {}, {}, {}, []
    for field in spec["fields"]:
        value = _scalar_value(target.get(field))
        if value is None:
            decided_fields[field] = dict(_MISSING_LEAF)
            missing_names.append(field)
            continue
        prev_field = prev_fields.get(field)
        if _is_sufficient(prev_field):
            decided_fields[field] = _leaf_verdict(prev_field)
            context_only[field] = value
        else:
            needs_verdict[field] = value

    if not needs_verdict:
        # Every populated field is already carried SUFFICIENT and nothing
        # new appeared -- nothing has changed since the block-level verdict
        # was last computed, so carry it forward as-is instead of re-asking.
        already_decided[block] = {**_leaf_verdict(prev_block), "fields": decided_fields}
        return

    already_decided[block] = {"fields": decided_fields}
    payload: Dict[str, Any] = {"fields_to_judge": needs_verdict}
    if context_only:
        payload["already_sufficient"] = context_only
    if missing_names:
        payload["missing_fields"] = missing_names
    to_judge[block] = payload


def _prune_list_object_block(resume, block, spec, prev_block, already_decided, to_judge):
    """List-object blocks: experience, education, projects, certifications, courses."""
    items: List[Dict[str, Any]] = resume.get(block) or []
    if not items:
        already_decided[block] = dict(_MISSING_LEAF)
        return

    field_specs = spec.get("fields", {})
    scalar_keys = item_field_keys(block)
    array_keys = item_array_field_keys(block)
    prev_items_by_id = {it["id"]: it for it in (prev_block or {}).get("items", []) if it.get("id")}

    decided_items, judge_items, context_items = [], [], []
    any_needs_verdict = False

    for item in items:
        item_id = item.get("id")
        prev_item = prev_items_by_id.get(item_id) or {}
        prev_fields = prev_item.get("fields", {})

        needs_verdict, context_only, decided_fields, missing_names = {}, {}, {}, []
        for field in field_specs:
            if field in scalar_keys:
                value = _scalar_value(item.get(field))
            elif field in array_keys:
                value = _array_value(item.get(field))
            else:
                continue
            if value is None:
                decided_fields[field] = dict(_MISSING_LEAF)
                missing_names.append(field)
                continue
            prev_field = prev_fields.get(field)
            if _is_sufficient(prev_field):
                decided_fields[field] = _leaf_verdict(prev_field)
                context_only[field] = value
            else:
                needs_verdict[field] = value

        if decided_fields:
            decided_items.append({"id": item_id, "fields": decided_fields})

        if needs_verdict:
            entry = {"id": item_id, "fields_to_judge": needs_verdict}
            if missing_names:
                entry["missing_fields"] = missing_names
            judge_items.append(entry)
            any_needs_verdict = True
        elif context_only:
            entry = {"id": item_id, "fields": context_only}
            if missing_names:
                entry["missing_fields"] = missing_names
            context_items.append(entry)

    if not any_needs_verdict:
        if _is_sufficient(prev_block):
            already_decided[block] = {**_leaf_verdict(prev_block), "items": decided_items}
            return
        if not context_items:
            # Nothing populated anywhere in this block and it's never been
            # judged sufficient -- treat the whole block as settled-empty.
            already_decided[block] = {"items": decided_items} if decided_items else dict(_MISSING_LEAF)
            return

    if decided_items:
        already_decided[block] = {"items": decided_items}
    payload: Dict[str, Any] = {}
    if judge_items:
        payload["items_to_judge"] = judge_items
    if context_items:
        payload["items_context"] = context_items
    to_judge[block] = payload


def merge_completeness(
    already_decided: Dict[str, Any],
    llm_blocks: Dict[str, Any],
    coverage: Dict[str, Any],
) -> Dict[str, Any]:
    """Combines the carried-forward/MISSING parts with a fresh LLM response
    into the new `field_completeness` state to store. `llm_blocks` is the
    `"blocks"` dict from `COMPLETENESS_RESPONSE_SCHEMA` (phase-4) -- absent
    or empty when nothing needed judging this round.
    """
    result: Dict[str, Any] = {}
    for block in coverage:
        settled = already_decided.get(block)
        llm_node = (llm_blocks or {}).get(block)

        if llm_node is None:
            result[block] = settled if settled is not None else dict(_MISSING_LEAF)
            continue

        node: Dict[str, Any] = {
            "completeness_status": llm_node.get("completeness_status", STATUS_PARTIAL),
            "reason": llm_node.get("reason"),
            "confidence": llm_node.get("confidence"),
        }

        decided_fields = (settled or {}).get("fields", {})
        llm_fields = llm_node.get("fields", {})
        if decided_fields or llm_fields:
            node["fields"] = {**decided_fields, **llm_fields}

        decided_items = (settled or {}).get("items", [])
        llm_items = llm_node.get("items", [])
        if decided_items or llm_items:
            node["items"] = _merge_items(decided_items, llm_items)

        result[block] = node
    return result


def _merge_items(decided_items: List[Dict[str, Any]], llm_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_id: Dict[str, Dict[str, Any]] = {}
    for item in decided_items:
        by_id.setdefault(item["id"], {"id": item["id"], "fields": {}})["fields"].update(item.get("fields", {}))
    for item in llm_items:
        by_id.setdefault(item["id"], {"id": item["id"], "fields": {}})["fields"].update(item.get("fields", {}))
    return list(by_id.values())
```

## Key design points, explained

- **`already_decided` and `to_judge` are keyed the same way for a
  partially-settled block.** A block being sent to the LLM this round still
  gets an `already_decided[block]` entry holding its carried/`MISSING`
  fields — `merge_completeness` needs both halves to reassemble the full
  field/item set, since the LLM response only ever contains the leaves it
  was actually asked to judge.
- **`fields_to_judge` and `items_to_judge` name exactly the leaves the LLM
  must return a verdict for.** `already_sufficient`/`items_context`/
  `missing_fields` are read-only — `phase-3`'s prompt explicitly forbids
  emitting verdicts for those, and `merge_completeness` never looks at the
  LLM response for a leaf it didn't ask about anyway, so nothing bad happens
  even if the LLM ever slipped and echoed one back.
- **On the very first silence event, `previous_status` is `{}`.** Every
  populated leaf is `TO_JUDGE`, `context_only`/`already_sufficient` are
  always empty, and the whole rubric that has *any* value gets judged once.
  From the second event on, only what's still `PARTIAL` or newly populated
  gets re-sent.
- **`_prune_list_object_block`'s "nothing needs a verdict but the block
  isn't already SUFFICIENT" branch** covers the case where every populated
  item-field is carried-`SUFFICIENT` but the block's own aggregate bar
  (e.g. "at least one experience is sufficiently covered") was last
  `PARTIAL` — nothing changed since then, so that `PARTIAL` verdict is
  carried forward rather than re-asked with no new information.
