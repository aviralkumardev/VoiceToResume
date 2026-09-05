from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

from app.meeting_room.resume_analysis_pipeline.completeness_status import TERMINAL_STATUSES
from app.meeting_room.resume_analysis_pipeline.config_jsons_definitions.coverage_schema import (
    askable_coverage_schema,
    complete_when_for_target,
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


def _block_is_open(
    resume: Dict[str, Any], field_completeness: Dict[str, Any], block: str, spec: Dict[str, Any]
) -> bool:
    """Whether `block` might still have something worth asking about.

    A list-object block with existing items (`"fields" in spec` and
    `resume[block]` non-empty -- experience/education/projects/
    certifications/courses) is always considered open here: each item gets
    its own separate question thread, so the block's own aggregate verdict
    (e.g. SUFFICIENT because ONE item already cleared a loose "at least one
    is enough" bar) must never hide every OTHER item's still-open fields.
    `_item_level_target` itself is what actually decides there's nothing
    left, once every existing item has no open field of its own remaining
    (filled in, explicitly declined, or given up on after hitting the
    per-round question cap) -- this predicate only controls whether it gets
    a chance to look. A block with no items yet, or with no item breakdown
    at all (skills, achievements, awards, languages,
    additional_information), has no item-level granularity to fall back on
    and keeps using the block's own aggregate `completeness_status` exactly
    as before.
    """
    if "fields" in spec and resume.get(block):
        return True
    return _block_status(field_completeness, block) not in TERMINAL_STATUSES


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
        if _block_is_open(resume, field_completeness, block, spec)
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


def gap_key(block: str, item_id: Optional[str]) -> str:
    """Stable identity for an ordinary coverage-gap candidate -- used by
    `mark_target_given_up`/`compute_candidate_queue` to exclude a
    given-up-on target from every later cycle."""
    return f"gap:{block}:{item_id or ''}"


def _parse_gap_key(key: str) -> Optional[ExcludeKey]:
    if not key.startswith("gap:"):
        return None
    rest = key[len("gap:"):]
    block, _, item_id = rest.partition(":")
    return block, (item_id or None)


def compute_candidate_queue(
    resume: Dict[str, Any],
    coverage: Dict[str, Any],
    field_completeness: Dict[str, Any],
    *,
    excluded_keys: FrozenSet[str] = frozenset(),
) -> List[Dict[str, Any]]:
    """The full ordered candidate list handed to `combined_chain.run_combined_chain`:
    outstanding conflicts (insertion order) -> outstanding unresolved records
    (insertion order) -> ordinary coverage gaps (`compute_next_targets`,
    unchanged priority order). This is Python's authoritative priority
    ordering -- the combined call is instructed to preserve it, and its
    response is re-sorted back to this exact order before being trusted (see
    `combined_chain._validate_queue`).

    This is the new home for what `InterviewDirector._pick_forced_topic` used
    to do, moved here because candidate-list computation now runs from the
    analysis-worker task (`analysis_orchestrator._run_batch`) once per
    combined-call cycle, rather than from `InterviewDirector`, which no
    longer picks targets at all -- it only pops already-worded questions off
    the queue this function produced.

    `excluded_keys` is every candidate key to leave out entirely: the round
    currently open (so it isn't reselected as its own successor), any
    target a prior round gave up on, and any forced topic already given its
    one round this session (`questions.given_up_targets` /
    `questions.forced_topics_spent` -- see `data/crud.py`'s
    `mark_target_given_up`/`mark_forced_topic_spent`).

    Each candidate: `{"kind": "conflict"|"unresolved"|"gap", "key", "block",
    "item_id", "fields", "complete_when"}`, plus the raw resume record under
    `"record"` for `kind != "gap"` -- everything the combined call needs to
    word a question without a separate topic-wording call.
    """
    candidates: List[Dict[str, Any]] = []

    for record in resume.get("conflicts") or []:
        record_id = record.get("id")
        if not record_id:
            continue
        key = f"conflict:{record_id}"
        if key in excluded_keys:
            continue
        field = record.get("field")
        candidates.append({
            "kind": "conflict",
            "key": key,
            "block": record.get("block"),
            "item_id": record.get("item_id"),
            "fields": [field] if field else None,
            "complete_when": None,
            "record": record,
        })

    for record in resume.get("unresolved") or []:
        record_id = record.get("id")
        if not record_id:
            continue
        key = f"unresolved:{record_id}"
        if key in excluded_keys:
            continue
        candidates.append({
            "kind": "unresolved",
            "key": key,
            "block": record.get("block"),
            "item_id": None,
            "fields": None,
            "complete_when": None,
            "record": record,
        })

    gap_exclude: Set[ExcludeKey] = set()
    for key in excluded_keys:
        parsed = _parse_gap_key(key)
        if parsed is not None:
            gap_exclude.add(parsed)

    for target in compute_next_targets(
        resume, coverage, field_completeness, exclude_targets=frozenset(gap_exclude),
    ):
        block, item_id, fields = target["block"], target.get("item_id"), target.get("fields")
        candidates.append({
            "kind": "gap",
            "key": gap_key(block, item_id),
            "block": block,
            "item_id": item_id,
            "fields": fields,
            "complete_when": complete_when_for_target(coverage, target),
        })

    return candidates
