# Phase 2 — Deterministic Target Selection

## What this does

The pure-Python core of "which one thing should we ask about next" — two
new **public** functions in `completeness_status.py` (same home as
`prune_for_judgment`/`merge_completeness`: no I/O, no `asyncio`, so
`phase-9`'s tests and `phase-6`'s worker can both use it directly):

- **`target_status(target_path, status_dict)`** — looks up the current
  `completeness_status` at a path (`"block"`, `"block.field"`, or
  `"block.item_id.field"`) inside any `field_completeness`-shaped dict,
  defaulting to `MISSING` if nothing's there yet. Generic on purpose: the
  worker (`phase-6`) reuses it both *before* a cycle (against
  `previous_status`, inside `select_focus_target`) and *after* one
  (against the freshly `merge_completeness`d result, to decide whether the
  just-asked-about target resolved).
- **`select_focus_target(resume, coverage, previous_status, sticky_path)`**
  — the priority/sticky-focus algorithm from `phase-0`:
  1. If `sticky_path` isn't yet `SUFFICIENT`, keep targeting it.
  2. Otherwise, blocks with *some* content are walked in
     `objective_priority` order, and the first one with an open field (by
     that field's own `importance`: required → recommended → optional)
     wins.
  3. Only once every started block is fully `SUFFICIENT` do completely
     empty blocks become eligible, in `objective_priority` order, each
     producing a whole-block target.
  4. `None` once there's nothing left to ask about.

Both functions return/consume the same small shape:
`{"target_type": "BLOCK" | "FIELD", "target_path": str, "complete_when": str}`
— this is exactly what `phase-3`'s prompt builder needs, and the backend
(not the LLM) owns every field of it.

## File to modify: `backend/app/meeting_room/resume_analysis_pipeline/completeness_status.py`

Current file (for reference — this is what exists today, from
`docs/silence-detection-processing-implementation/phase-2-completeness-status.md`,
unchanged since):

```python
from typing import Any, Dict, List, Optional, Tuple

from app.meeting_room.resume_analysis_pipeline.config_jsons_definitions.resume_schema import block_kind, item_array_field_keys, item_field_keys


STATUS_MISSING = "MISSING"
STATUS_PARTIAL = "PARTIAL"
STATUS_SUFFICIENT = "SUFFICIENT"
STATUS_NOT_APPLICABLE ="NOT_APPLICABLE"

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
        "confidence": node.get("confidence")
    }


def prune_for_judgment(...):
    ...  # unchanged, see docs/silence-detection-processing-implementation/phase-2

def _prune_atomic_block(...):
    ...  # unchanged

def _prune_singular_block(...):
    ...  # unchanged

def _prune_list_object_block(...):
    ...  # unchanged

def merge_completeness(...):
    ...  # unchanged

def _merge_items(decided_items, llm_items):
    by_id: Dict[str, Dict[str, Any]] = {}
    for item in decided_items:
        by_id.setdefault(item["id"], {"id": item["id"], "fields": {}})["fields"].update(item.get("fields", {}))
    for item in llm_items:
        by_id.setdefault(item["id"], {"id": item["id"], "fields": {}})["fields"].update(item.get("fields", {}))
    return list(by_id.values())
```

(`prune_for_judgment`/`_prune_atomic_block`/`_prune_singular_block`/
`_prune_list_object_block`/`merge_completeness` bodies are exactly as in
`docs/silence-detection-processing-implementation/phase-2-completeness-status.md`
— nothing about them changes in this phase, elided above only for length.)

**Change 1** — add one new constant, right after `_MISSING_LEAF`:

```python
_MISSING_LEAF: Dict[str, Any] = {"completeness_status": STATUS_MISSING, "reason": None, "confidence": None}

FIELD_IMPORTANCE_ORDER: Dict[str, int] = {"required": 0, "recommended": 1, "optional": 2}
```

**Change 2** — append everything below to the **end of the file**, after
`_merge_items`:

```python
def target_status(target_path: str, status_dict: Dict[str, Any]) -> str:
    """Looks up the current completeness_status at `target_path` (a block
    name, "block.field", or "block.item_id.field") inside a
    field_completeness-shaped dict. Defaults to MISSING for anything not
    yet present -- the same "no verdict yet means MISSING" convention
    prune_for_judgment already uses. Works against BOTH `previous_status`
    (the state going into a cycle) and a freshly merge_completeness'd
    result (the state coming out of one) -- callers choose which snapshot
    to check.
    """
    parts = target_path.split(".")
    block_node = (status_dict or {}).get(parts[0])

    if len(parts) == 1:
        return _leaf_status(block_node)

    if len(parts) == 2:
        field_node = (block_node or {}).get("fields", {}).get(parts[1])
        return _leaf_status(field_node)

    if len(parts) == 3:
        _, item_id, field = parts
        for item in (block_node or {}).get("items", []):
            if item.get("id") == item_id:
                return _leaf_status(item.get("fields", {}).get(field))
        return STATUS_MISSING

    return STATUS_MISSING


def _leaf_status(node: Optional[Dict[str, Any]]) -> str:
    return (node or {}).get("completeness_status", STATUS_MISSING)


def _target_info(target_path: str, coverage: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Builds {target_type, target_path, complete_when} purely from the
    schema -- no resume/status lookups. Returns None if target_path
    doesn't resolve against `coverage` (defensive only; a path this module
    itself produced always resolves)."""
    parts = target_path.split(".")
    block = parts[0]
    spec = coverage.get(block)
    if spec is None:
        return None

    if len(parts) == 1:
        return {"target_type": "BLOCK", "target_path": block, "complete_when": spec["complete_when"]}

    field_specs = spec.get("fields") or {}
    field = parts[-1]
    field_spec = field_specs.get(field)
    if field_spec is None:
        return None

    return {"target_type": "FIELD", "target_path": target_path, "complete_when": field_spec["complete_when"]}


def _block_has_content(resume: Dict[str, Any], block: str) -> bool:
    value = resume.get(block)
    if isinstance(value, list):
        return bool(value)
    if isinstance(value, dict):
        return any(_scalar_value(v) for v in value.values())
    return False


def _sorted_priority_blocks(coverage: Dict[str, Any]) -> List[str]:
    return sorted(coverage.keys(), key=lambda b: coverage[b].get("objective_priority", 999))


def _first_open_target(
    block: str,
    resume: Dict[str, Any],
    coverage: Dict[str, Any],
    previous_status: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """The highest-priority still-open field/item-field inside a block that
    already has some content, or a BLOCK-level target for an atomic
    list_string block that's populated but not yet SUFFICIENT.

    Fields with NO value at all in `resume` always win over fields that
    have a value but simply aren't yet verdicted SUFFICIENT in
    `previous_status` -- a field with a value is likely just awaiting this
    same round's normal grading (it's in `to_judge`), so asking about it
    again would be redundant; a field with nothing at all is an
    unambiguous, genuine gap. Within each of those two tiers, fields are
    ordered by FIELD_IMPORTANCE_ORDER. None if nothing in this block is
    open.
    """
    spec = coverage[block]
    field_specs = spec.get("fields")

    if not field_specs:
        if target_status(block, previous_status) != STATUS_SUFFICIENT:
            return _target_info(block, coverage)
        return None

    ordered_fields = sorted(
        field_specs.keys(),
        key=lambda f: FIELD_IMPORTANCE_ORDER.get(field_specs[f].get("importance", "optional"), 2),
    )
    is_list_object = block_kind(block) == "list_object"
    array_keys = item_array_field_keys(block) if is_list_object else frozenset()

    missing_candidates: List[str] = []
    partial_candidates: List[str] = []

    def _consider(path: str, has_value: bool) -> None:
        if target_status(path, previous_status) == STATUS_SUFFICIENT:
            return
        (missing_candidates if not has_value else partial_candidates).append(path)

    if is_list_object:
        for item in resume.get(block) or []:
            item_id = item.get("id")
            for field in ordered_fields:
                if field in array_keys:
                    has_value = _array_value(item.get(field)) is not None
                else:
                    has_value = _scalar_value(item.get(field)) is not None
                _consider(f"{block}.{item_id}.{field}", has_value)
    else:
        container = resume.get(block) or {}
        for field in ordered_fields:
            has_value = _scalar_value(container.get(field)) is not None
            _consider(f"{block}.{field}", has_value)

    chosen = missing_candidates[0] if missing_candidates else (partial_candidates[0] if partial_candidates else None)
    return _target_info(chosen, coverage) if chosen else None


def select_focus_target(
    resume: Dict[str, Any],
    coverage: Dict[str, Any],
    previous_status: Dict[str, Any],
    sticky_path: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Deterministically picks the ONE block/field the next completeness
    cycle should write a follow-up question for. Pure, no I/O.

    1. Sticky focus: if `sticky_path` is set and isn't SUFFICIENT yet in
       `previous_status`, keep targeting it.
    2. Otherwise, blocks with *some* content are exhausted first (their
       highest-priority open field), in objective_priority order.
    3. Only once every started block is fully SUFFICIENT does a completely
       empty block become eligible (whole-block question), in
       objective_priority order.
    4. None once nothing is left to ask about.
    """
    if sticky_path and target_status(sticky_path, previous_status) != STATUS_SUFFICIENT:
        info = _target_info(sticky_path, coverage)
        if info is not None:
            return info
        # Sticky path no longer resolves against the schema -- fall through
        # and pick a fresh target instead of getting stuck.

    started, empty = [], []
    for block in _sorted_priority_blocks(coverage):
        (started if _block_has_content(resume, block) else empty).append(block)

    for block in started:
        gap = _first_open_target(block, resume, coverage, previous_status)
        if gap is not None:
            return gap

    for block in empty:
        return _target_info(block, coverage)

    return None
```

No new imports are needed — `block_kind` is already imported at the top of
this file and used by `prune_for_judgment`; `Optional`/`Dict`/`List`/`Any`
are already imported from `typing`.

## Key design points, explained

- **`target_status` is the single source of truth for "is this thing done
  yet"**, reused in three different places across this feature:
  `select_focus_target`'s sticky/gap checks (against `previous_status`),
  `phase-6`'s worker (against the freshly merged result, to decide whether
  the question just asked resolved), and `phase-9`'s tests. One path-
  parsing implementation, not three.
- **A block with *some* content always wins over a completely empty
  block, regardless of `objective_priority` numbers.** This is the
  "Education and Skills filled in, everything else empty → target
  Education/Skills' remaining gaps first, not a fully-empty block ranked
  higher" rule from the design discussion. `objective_priority` only
  breaks ties *within* the started tier and *within* the empty tier
  separately — it never lets an empty block jump ahead of a started one.
- **A list_object block with every item-field `SUFFICIENT` but no field
  gap left returns `None` from `_first_open_target`**, even if that
  block's own aggregate verdict happens to lag a round behind (a known,
  accepted edge case — the completeness LLM call itself will very likely
  also independently converge the aggregate to `SUFFICIENT` immediately
  after, since every field it can see is already good; not worth a
  redundant "tell me about your experience again" question over).
- **Sticky-path resolution failure is handled by falling through**, not
  raising — `_target_info` returning `None` (schema no longer has this
  path) just means `select_focus_target` picks a fresh target the normal
  way, rather than the worker crashing on a stale pointer. In practice
  this can't currently happen (paths this module produces always resolve
  against the same static `COVERAGE_SCHEMA`), but it's a one-line guard
  for free.
- **Field ordering within a block ties on `FIELD_IMPORTANCE_ORDER`, then
  falls back to the field's declaration order in `COVERAGE_SCHEMA`** —
  Python's `sorted` is stable, so two `required` fields keep whatever
  order they're written in the schema dict, no explicit tiebreak code
  needed.
