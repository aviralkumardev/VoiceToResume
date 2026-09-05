# Phase 2 — Merge logic

## What this does

Takes the LLM's proposed `updates` payload and merges it into the persisted
`resume_data` dict, validated against the phase-1 schema. Never raises —
anything malformed is recorded in `rejected` and skipped, so one bad field
from the LLM can't cost the rest of a batch's genuine facts.

This is the piece that most diverges from pitch_room's `merge.py`, because of
the singular/list-object/list-string split described in the overview.

**Extension**: this phase now also owns conflict-tracking and
unresolved-fact-tracking — the two safety nets pitch_room has no equivalent
of:

- `merge_updates(..., force_overwrite=False)` — when a scalar field (singular
  block field, or list-object item scalar field) already holds a non-null
  value that **differs** from the new value, the new value is diverted into
  a `conflicts` record instead of overwriting, unless `force_overwrite=True`
  (used only by the final pass, phase 5/6).
- `merge_unresolved(resume, unresolved_items)` — appends LLM-flagged
  ambiguous fragments into `resume["unresolved"]`, deduped.
- `apply_resolved_conflicts(resume, resolved)` — force-applies a previously
  conflicted field's now-clarified value, and removes the conflict record.
- `remove_unresolved(resume, ids)` — drops unresolved records once their fact
  has been folded into `updates` with proper attribution.

## File to create

### `app/meeting_room/resume_analysis_pipeline/merge.py`

```python
"""Merges LLM-proposed resume updates into the persisted resume_data dict.

Mirrors pitch_room's app/pitch_room/pitch_analysis_pipeline/merge.py, adapted
for a schema that mixes singular objects (personal/summary/preferences) with
id-addressed list-of-object blocks (experience/education/...) and plain
string-list blocks (skills/achievements/...) — categories pitch_room's fixed
deck-block schema never needed.

merge_updates() never raises on malformed input; anything invalid is recorded
in `rejected` and skipped, so a single bad field from the LLM can't lose the
rest of a batch's genuine facts.

Extension over pitch_room: a scalar field that already holds a differing
non-null value is NOT silently overwritten. Instead it's diverted into a
flat resume["conflicts"] record (unless force_overwrite=True, used only by
the final full-transcript pass). A parallel resume["unresolved"] list holds
LLM-flagged facts that couldn't be confidently attributed to an existing
list-object item.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional, Tuple

from app.meeting_room.resume_analysis_pipeline.config_jsons_definitions.resume_schema import (
    RESUME_SCHEMA,
    block_kind,
    item_array_field_keys,
    item_field_keys,
    singular_field_keys,
)

SOURCE = "MEETING"


def merge_updates(
    resume: Dict[str, Any], updates: Any, *, force_overwrite: bool = False,
) -> Tuple[Dict[str, Any], List[str], List[str]]:
    accepted: List[str] = []
    rejected: List[str] = []

    if not isinstance(updates, dict):
        return resume, accepted, rejected

    for block, payload in updates.items():
        if block not in RESUME_SCHEMA:
            rejected.append(f"{block}.*")
            continue

        kind = block_kind(block)
        try:
            if kind in ("singular", "singular_freeform"):
                _merge_singular_block(resume, block, payload, kind, force_overwrite, accepted, rejected)
            elif kind == "list_object":
                _merge_list_object_block(resume, block, payload, force_overwrite, accepted, rejected)
            elif kind == "list_string":
                _merge_list_string_block(resume, block, payload, accepted, rejected)
        except Exception:
            rejected.append(f"{block}.*")

    return resume, accepted, rejected


def _set_or_conflict(
    resume: Dict[str, Any], block: str, field: str, item_id: Optional[str],
    target: Dict[str, Any], value: Any, force_overwrite: bool,
) -> bool:
    """Applies value to target[field], unless it conflicts with an existing
    differing value and force_overwrite is False — in which case it's
    diverted to a conflict record and the field is left untouched. Returns
    True if the field was actually written (used by callers to mark
    `accepted`; a diverted conflict is not counted as accepted)."""
    existing = target.get(field)
    existing_value = existing.get("value") if isinstance(existing, dict) else None

    if not force_overwrite and existing_value is not None:
        if str(existing_value).strip().lower() != str(value).strip().lower():
            _add_conflict(resume, block, field, item_id, existing_value, value)
            return False
        # exact re-confirmation of the same value — nothing to do
        return False

    target[field] = {"value": value, "source": SOURCE}
    return True


def _add_conflict(
    resume: Dict[str, Any], block: str, field: str, item_id: Optional[str],
    existing_value: Any, candidate_value: Any,
) -> None:
    conflicts: List[Dict[str, Any]] = resume.setdefault("conflicts", [])
    record = next(
        (
            c for c in conflicts
            if c["block"] == block and c["field"] == field and c.get("item_id") == item_id
        ),
        None,
    )
    if record is None:
        record = {
            "id": uuid.uuid4().hex[:8],
            "block": block,
            "field": field,
            "item_id": item_id,
            "existing_value": existing_value,
            "candidates": [],
        }
        conflicts.append(record)

    candidate_str = str(candidate_value).strip()
    seen = {str(c).strip().lower() for c in record["candidates"]}
    if candidate_str.lower() not in seen:
        record["candidates"].append(candidate_str)


def merge_unresolved(resume: Dict[str, Any], unresolved_items: Any) -> None:
    if not isinstance(unresolved_items, list):
        return
    unresolved: List[Dict[str, Any]] = resume.setdefault("unresolved", [])
    for item in unresolved_items:
        if not isinstance(item, dict):
            continue
        block = item.get("block")
        text = item.get("text")
        if not block or not text or block not in RESUME_SCHEMA:
            continue
        text_norm = str(text).strip()
        if not text_norm:
            continue
        already = any(
            u["block"] == block and u["text"].strip().lower() == text_norm.lower()
            for u in unresolved
        )
        if already:
            continue
        unresolved.append({
            "id": uuid.uuid4().hex[:8],
            "block": block,
            "text": text_norm,
            "note": str(item.get("note") or "").strip(),
        })


def apply_resolved_conflicts(resume: Dict[str, Any], resolved: Any) -> None:
    if not isinstance(resolved, list):
        return
    conflicts: List[Dict[str, Any]] = resume.setdefault("conflicts", [])
    for entry in resolved:
        if not isinstance(entry, dict):
            continue
        conflict_id = entry.get("id")
        value = entry.get("value")
        if not conflict_id or value is None:
            continue
        record = next((c for c in conflicts if c["id"] == conflict_id), None)
        if record is None:
            continue

        block, field, item_id = record["block"], record["field"], record.get("item_id")
        kind = block_kind(block)
        if kind in ("singular", "singular_freeform"):
            target = resume.setdefault(block, {})
        else:
            items: List[Dict[str, Any]] = resume.setdefault(block, [])
            target = next((it for it in items if it.get("id") == item_id), None)
            if target is None:
                continue
        target[field] = {"value": str(value).strip(), "source": SOURCE}
        conflicts.remove(record)


def remove_unresolved(resume: Dict[str, Any], ids: Any) -> None:
    if not isinstance(ids, list) or not ids:
        return
    ids_set = set(ids)
    unresolved: List[Dict[str, Any]] = resume.setdefault("unresolved", [])
    resume["unresolved"] = [u for u in unresolved if u["id"] not in ids_set]


def _merge_singular_block(
    resume: Dict[str, Any], block: str, payload: Any, kind: str, force_overwrite: bool,
    accepted: List[str], rejected: List[str],
) -> None:
    if not isinstance(payload, dict):
        rejected.append(f"{block}.*")
        return

    valid_fields = singular_field_keys(block) if kind == "singular" else None
    target = resume.setdefault(block, {})

    for field, field_payload in payload.items():
        if valid_fields is not None and field not in valid_fields:
            rejected.append(f"{block}.{field}")
            continue
        value = _extract_value(field_payload)
        if value is None:
            rejected.append(f"{block}.{field}")
            continue
        if _set_or_conflict(resume, block, field, None, target, value, force_overwrite):
            accepted.append(f"{block}.{field}")


def _merge_list_object_block(
    resume: Dict[str, Any], block: str, payload: Any, force_overwrite: bool,
    accepted: List[str], rejected: List[str],
) -> None:
    if not isinstance(payload, list):
        rejected.append(f"{block}.*")
        return

    scalar_fields = item_field_keys(block)
    array_fields = item_array_field_keys(block)
    items: List[Dict[str, Any]] = resume.setdefault(block, [])

    for entry in payload:
        if not isinstance(entry, dict):
            rejected.append(f"{block}.*")
            continue

        entry_id = entry.get("id")
        target = None
        if entry_id:
            target = next((it for it in items if it.get("id") == entry_id), None)
        if target is None:
            target = {"id": entry_id or uuid.uuid4().hex[:8]}
            items.append(target)

        for field, field_payload in entry.items():
            if field == "id":
                continue
            if field in scalar_fields:
                value = _extract_value(field_payload)
                if value is None:
                    rejected.append(f"{block}.{field}")
                    continue
                if _set_or_conflict(resume, block, field, target["id"], target, value, force_overwrite):
                    accepted.append(f"{block}.{field}")
            elif field in array_fields:
                values = _extract_string_list(field_payload)
                if not values:
                    rejected.append(f"{block}.{field}")
                    continue
                if _append_dedup(target.setdefault(field, []), values):
                    accepted.append(f"{block}.{field}")
            else:
                rejected.append(f"{block}.{field}")


def _merge_list_string_block(
    resume: Dict[str, Any], block: str, payload: Any, accepted: List[str], rejected: List[str]
) -> None:
    values = _extract_string_list(payload)
    if not values:
        rejected.append(f"{block}.*")
        return
    if _append_dedup(resume.setdefault(block, []), values):
        accepted.append(block)


def _extract_value(field_payload: Any) -> Any:
    """A field payload is normally {"value": ...}; tolerate a bare scalar too."""
    if isinstance(field_payload, dict):
        value = field_payload.get("value")
    else:
        value = field_payload
    if isinstance(value, str):
        value = value.strip()
    return value or None


def _extract_string_list(field_payload: Any) -> List[str]:
    if isinstance(field_payload, dict):
        field_payload = field_payload.get("value")
    if not isinstance(field_payload, list):
        return []
    cleaned = []
    for item in field_payload:
        if isinstance(item, str) and item.strip():
            cleaned.append(item.strip())
    return cleaned


def _append_dedup(existing: List[str], new_values: List[str]) -> bool:
    seen = {v.strip().lower() for v in existing}
    changed = False
    for value in new_values:
        key = value.strip().lower()
        if key and key not in seen:
            existing.append(value)
            seen.add(key)
            changed = True
    return changed
```

## Key design points, explained

- **Dispatch by `kind`**: `merge_updates` looks up each incoming block's kind
  in `RESUME_SCHEMA` and routes to one of three handlers. An unknown block
  name is rejected outright.
- **Singular blocks** (`_merge_singular_block`): identical spirit to
  pitch_room — `resume[block][field] = {"value": ..., "source": "MEETING"}`.
  `personal`/`summary` validate field names against the schema;
  `preferences` accepts any field name (free-form). Every write now routes
  through `_set_or_conflict` instead of writing directly.
- **List-object blocks** (`_merge_list_object_block`) — the id-addressing
  scheme: for each incoming item, look for an existing item with a matching
  `id`. If found, merge into it. If not (no id given, or an id that doesn't
  match anything), create a brand-new item with a freshly generated 8-char
  hex id and append it.
  - Scalar item fields (`company`, `role`, etc.) also route through
    `_set_or_conflict`, keyed by `(block, field, item_id)`.
  - **Array item fields** (`responsibilities`, `skills`, etc.) intentionally
    **append with dedup instead of overwriting**. This is the one place this
    merge deliberately differs from pitch_room's "always overwrite" rule:
    pitch_room's fields are all scalars, so overwrite is always safe. Here,
    the extraction prompt tells the LLM to only mention *newly heard* items
    per batch (to keep prompts small) — if a later batch only says
    `["Deployed to production"]`, a blind overwrite of `responsibilities`
    would silently erase everything captured in earlier batches. Appending
    with case/whitespace-insensitive dedup avoids that data loss. Array
    fields are never conflict-diverted — appending is always safe, there's
    nothing to disagree about.
- **List-string blocks** (`_merge_list_string_block`): same append-with-dedup
  logic, just at the block level instead of inside an item. Also never
  conflict-diverted, for the same reason.
- **`_extract_value`/`_extract_string_list`**: tolerate the LLM sending either
  the expected `{"value": ...}` wrapper or a bare scalar/list — cheap
  defensive parsing, since a stray formatting slip shouldn't reject an
  otherwise-good fact.

### The conflict/unresolved extension

- **`_set_or_conflict`** is the single choke point every scalar write goes
  through. If the field is currently empty (`None`), or `force_overwrite` is
  `True` (final pass only), it writes normally. Otherwise, if the new value
  differs from the existing one (case/whitespace-insensitive compare), it
  calls `_add_conflict` instead of writing, and returns `False` so the caller
  doesn't count it as `accepted`. An exact re-confirmation of the same value
  is silently a no-op — not a conflict, nothing to record.
- **`_add_conflict`** dedups by the triple `(block, field, item_id)` — a
  second differing candidate for the same field appends into that record's
  `candidates` list (also deduped), rather than creating a second record.
  `item_id` is `None` for singular-block fields and the item's real id for
  list-object fields, which is exactly what distinguishes "conflicting
  `personal.name`" from "conflicting `experience[e7f8].end_date`" from
  "conflicting `experience[a1b2].end_date`" as three independent records.
- **`merge_unresolved`** is the LLM-facing counterpart: the chain (phase 4)
  passes through whatever the LLM reports in its `unresolved` response key,
  and this function dedups by exact `(block, text)` match before appending —
  a candidate re-mentioning the same ambiguous fact in a later batch doesn't
  pile up duplicate records.
- **`apply_resolved_conflicts`** is how an incremental batch (or, in
  practice, mostly the final pass) permanently resolves a conflict: given
  `{"id", "value"}`, it looks up the conflict record, finds the real
  `(block, field, item_id)` it points at, force-writes the value there
  (bypassing `_set_or_conflict` entirely — this call *is* the resolution),
  and removes the record from `conflicts`.
- **`remove_unresolved`** just filters `unresolved` down to exclude the given
  ids — called after the orchestrator has already folded that fact into
  `updates` with a real, confident `(block, item_id)` attribution.
- **`force_overwrite=True`** is only ever passed by the final-pass code path
  (phase 5's `_run_final_pass` → `crud.apply_final_resolution`, phase 6). It
  makes every scalar write in that one call succeed unconditionally, since
  the final pass has the complete transcript and is trusted to make the
  final call — there's no next batch to defer to.
