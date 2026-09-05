from typing import Any, Dict, FrozenSet, Optional

from app.meeting_room.resume_analysis_pipeline.completeness_status import TERMINAL_STATUSES
from app.meeting_room.resume_analysis_pipeline.config_jsons_definitions.coverage_schema import (
    askable_coverage_schema,
)


def _block_status(field_completeness: Dict[str, Any], block: str) -> Optional[str]:
    node = (field_completeness or {}).get(block)
    if not isinstance(node, dict):
        return None
    status = node.get("completeness_status")
    return status if isinstance(status, str) else None


def find_required_gap(
    field_completeness: Dict[str, Any],
    coverage: Dict[str, Any],
    *,
    exclude: FrozenSet[str] = frozenset(),
) -> Optional[Dict[str, Any]]:
    """The Python safety net that keeps the interview from ending while a
    `required` block is still genuinely open.

    Works off each block's own top-level `completeness_status` only -- the
    rubric's own bar for a block is already "at least one X sufficiently
    covered", so there's no need to descend into `fields`/`items` the way
    the old selection machinery did. A block with no verdict at all yet
    (never graded) counts as open too, same as MISSING everywhere else in
    this pipeline.

    Ordered by the schema's own `objective_priority`, so the first gap found
    is always the highest-priority one. `exclude` carries `"gap:<block>"`
    keys already spent this session (see InterviewDirector._forced_topics_spent)
    -- a block whose forced round already ran once is not asked about again
    even if Task A hasn't caught up on grading it yet. Returns None once
    every required block is terminal or excluded.
    """
    askable = askable_coverage_schema(coverage or {})
    required_blocks = sorted(
        (block for block, spec in askable.items() if spec.get("importance") == "required"),
        key=lambda block: askable[block].get("objective_priority", 999),
    )

    for block in required_blocks:
        forced_topic = f"gap:{block}"
        if forced_topic in exclude:
            continue
        status = _block_status(field_completeness, block)
        if status in TERMINAL_STATUSES:
            continue
        return {
            "block": block,
            "complete_when": askable[block].get("complete_when", ""),
            "forced_topic": forced_topic,
        }

    return None
