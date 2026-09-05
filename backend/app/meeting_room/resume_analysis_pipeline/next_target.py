from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from app.meeting_room.resume_analysis_pipeline.completeness_status import TERMINAL_STATUSES
from app.meeting_room.resume_analysis_pipeline.config_jsons_definitions.coverage_schema import (
    askable_coverage_schema,
)

ExcludeKey = Tuple[str, Optional[str]]


def _block_status(field_completeness: Dict[str, Any], block: str) -> Optional[str]:
    node = (field_completeness or {}).get(block)
    if not isinstance(node, dict):
        return None
    status = node.get("completeness_status")
    return status if isinstance(status, str) else None


def _whole_block_target(block: str, exclude_targets: FrozenSet[ExcludeKey]) -> Optional[Dict[str, Any]]:
    if (block, None) in exclude_targets:
        return None
    return {"block": block, "item_id": None, "fields": None}


def _item_level_target(
    block: str,
    spec: Dict[str, Any],
    resume: Dict[str, Any],
    field_completeness: Dict[str, Any],
    exclude_targets: FrozenSet[ExcludeKey],
) -> Optional[Dict[str, Any]]:
    items = resume.get(block) or []
    if not items:
        return _whole_block_target(block, exclude_targets)

    field_names = list(spec.get("fields", {}).keys())
    items_by_id = {
        entry["id"]: entry
        for entry in (field_completeness.get(block) or {}).get("items", [])
        if entry.get("id")
    }

    for item in items:
        item_id = item.get("id")
        if (block, item_id) in exclude_targets:
            continue
        item_fields = items_by_id.get(item_id, {}).get("fields", {})
        open_fields = [
            field
            for field in field_names
            if item_fields.get(field, {}).get("completeness_status", "MISSING") not in TERMINAL_STATUSES
        ]
        if open_fields:
            return {"block": block, "item_id": item_id, "fields": open_fields}

    return None


def compute_next_targets(
    resume: Dict[str, Any],
    coverage: Dict[str, Any],
    field_completeness: Dict[str, Any],
    *,
    exclude_targets: FrozenSet[ExcludeKey] = frozenset(),
) -> List[Dict[str, Any]]:
    """The deterministic, priority-ordered list of everything left to ask
    about across the whole (askable) coverage rubric -- touched blocks
    (some data already captured) before untouched ones, each group sorted by
    `objective_priority` ascending (1 = highest priority). This is the
    Python-authoritative replacement for letting the fused
    `question_chain.run_question_chain` call invent its own next-question
    target: the caller hands the ENTIRE list returned here to that same
    call, and the model only picks the first entry not already resolved by
    the live conversation -- it never gets to choose a block on its own.

    `field_completeness` can lag the live conversation by a turn or more (a
    separate background worker); a block/item with no verdict yet is
    treated as open, same convention `required_gap.py` used before this
    module replaced it.

    `exclude_targets` is a set of `(block, item_id)` pairs to skip entirely
    -- used by the caller for (a) the round currently being graded, so it
    isn't immediately reselected as its own successor, and (b) any
    block/item a prior round gave up on (hit `max_questions_per_round`
    while still non-terminal) so the interview doesn't loop on a stuck
    topic forever.

    Returns the full list (not truncated) -- an empty list means nothing
    candidate-worthy remains across every askable block, i.e. the interview
    is genuinely done.
    """
    askable = askable_coverage_schema(coverage)

    open_blocks = [
        (block, spec)
        for block, spec in askable.items()
        if _block_status(field_completeness, block) not in TERMINAL_STATUSES
    ]
    touched = sorted(
        (entry for entry in open_blocks if resume.get(entry[0])),
        key=lambda entry: entry[1].get("objective_priority", 999),
    )
    untouched = sorted(
        (entry for entry in open_blocks if not resume.get(entry[0])),
        key=lambda entry: entry[1].get("objective_priority", 999),
    )

    targets: List[Dict[str, Any]] = []
    for block, spec in touched + untouched:
        if "fields" in spec:
            target = _item_level_target(block, spec, resume, field_completeness, exclude_targets)
        else:
            target = _whole_block_target(block, exclude_targets)
        if target:
            targets.append(target)

    return targets
