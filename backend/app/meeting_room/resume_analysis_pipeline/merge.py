from typing import Any, Dict, List, Optional, Tuple
import uuid

from app.meeting_room.resume_analysis_pipeline.answer_evidence import evidence_tokens
from app.meeting_room.resume_analysis_pipeline.config_jsons_definitions.resume_schema import (
    RESUME_SCHEMA,
    block_kind,
    item_array_field_keys,
    item_field_keys,
    singular_field_keys,
)

SOURCE = "MEETING"

_PLACEHOLDER_VALUES: frozenset[str] = frozenset({
    "not specified", "not stated", "not mentioned", "not provided",
    "not applicable", "n/a", "na", "unknown", "tbd", "none", "null",
})


def _is_placeholder(value: str) -> bool:
    return value.strip().lower().rstrip(".") in _PLACEHOLDER_VALUES

def merge_updates(
    resume: Dict[str, Any],
    updates: Any,
    *,
    force_overwrite: bool = False
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


def _merge_singular_block(
    resume: Dict[str, Any],
    block: str,
    payload: Any,
    kind: str,
    force_overwrite: bool,
    accepted: List[str],
    rejected: List[str]
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
    resume: Dict[str, Any],
    block: str,
    payload: Any,
    force_overwrite:bool,
    accepted: List[str],
    rejected: List[str]
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
                if force_overwrite:
                    replacement: List[str] = []
                    _append_dedup(replacement, values)
                    target[field] = replacement
                    accepted.append(f"{block}.{field}")
                elif _append_dedup(target.setdefault(field, []), values):
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
        if _is_placeholder(value):
            return None
    return value or None


def _extract_string_list(field_payload: Any) -> List[str]:
    if isinstance(field_payload, dict):
        field_payload = field_payload.get("value")
    if not isinstance(field_payload, list):
        return []
    cleaned = []
    for item in field_payload:
        if isinstance(item, str) and item.strip() and not _is_placeholder(item):
            cleaned.append(item.strip())
    return cleaned


def _same_labeled_entry(a: str, b: str) -> bool:
    """True if both strings share the same "Label: ..." prefix (e.g. two
    "Interests: ..." lines), so one is likely a restatement/expansion of the
    other rather than a genuinely distinct fact. Unlabeled strings (no colon)
    never match this way, so short atomic tokens like "Java" vs "JavaScript"
    are never conflated."""
    a_label, _, a_rest = a.partition(":")
    b_label, _, b_rest = b.partition(":")
    if not a_rest or not b_rest:
        return False
    return a_label.strip() == b_label.strip()


def _append_dedup(existing: List[str], new_values: List[str]) -> bool:
    changed = False
    for value in new_values:
        value = value.strip()
        key = value.lower()
        if not key:
            continue

        matched = False
        for i, existing_value in enumerate(existing):
            existing_key = existing_value.strip().lower()
            if existing_key == key:
                matched = True
                break
            if _same_labeled_entry(existing_key, key):
                if len(value) > len(existing_value.strip()):
                    existing[i] = value
                    changed = True
                matched = True
                break

        if not matched:
            existing.append(value)
            changed = True

    return changed


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


def _collect_accepted_texts(updates: Any, accepted: List[str]) -> List[str]:
    """Flattens the field values this same `updates` payload actually wrote
    (per `accepted`, e.g. "experience.location") into a flat list of
    strings -- used to catch the LLM dual-attributing one fact to both
    "updates" and "unresolved" in a single extraction response."""
    if not isinstance(updates, dict) or not accepted:
        return []
    accepted_set = set(accepted)
    texts: List[str] = []
    for block, payload in updates.items():
        if isinstance(payload, list) and payload and not isinstance(payload[0], dict):
            if block in accepted_set:
                texts.extend(v for v in payload if isinstance(v, str))
            continue

        if isinstance(payload, dict):
            entries = [payload]
        elif isinstance(payload, list):
            entries = [e for e in payload if isinstance(e, dict)]
        else:
            continue

        for entry in entries:
            for field, field_payload in entry.items():
                if field == "id" or f"{block}.{field}" not in accepted_set:
                    continue
                value = _extract_value(field_payload)
                if isinstance(value, str):
                    texts.append(value)
                elif isinstance(value, list):
                    texts.extend(v for v in value if isinstance(v, str))
    return texts


def is_redundant_with_accepted_update(
    text: str, updates: Any, accepted: List[str], *, min_shared_tokens: int = 3
) -> bool:
    """True when an "unresolved" entry's text shares substantial evidence
    with a fact this same extraction response just confidently wrote into
    "updates" -- i.e. the same fact was dual-attributed to two places in one
    response. Reuses `answer_evidence`'s tokenization so this stays
    consistent with the other evidence/overlap checks in the pipeline."""
    text_tokens = set(evidence_tokens(text))
    if len(text_tokens) < min_shared_tokens:
        return False
    for candidate in _collect_accepted_texts(updates, accepted):
        candidate_tokens = set(evidence_tokens(candidate))
        if len(candidate_tokens & text_tokens) >= min_shared_tokens:
            return True
    return False


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