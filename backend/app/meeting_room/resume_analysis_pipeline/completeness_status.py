from typing import Any, Dict, List, Optional, Tuple

from app.meeting_room.resume_analysis_pipeline.config_jsons_definitions.resume_schema import block_kind, item_array_field_keys, item_field_keys


STATUS_MISSING = "MISSING"
STATUS_PARTIAL = "PARTIAL"
STATUS_SUFFICIENT = "SUFFICIENT"
STATUS_NOT_APPLICABLE ="NOT_APPLICABLE"
STATUS_UNABLE_TO_ANSWER = "UNABLE_TO_ANSWER"

TERMINAL_STATUSES = frozenset({STATUS_SUFFICIENT, STATUS_UNABLE_TO_ANSWER, STATUS_NOT_APPLICABLE})

_MISSING_LEAF: Dict[str, Any] = {"completeness_status": STATUS_MISSING, "reason": None, "confidence": None}


def _is_not_applicable(spec: Dict[str, Any]) -> bool:
    """True when a coverage block or field entry has ``not_applicable: True``.

    This is the single choke-point for all NA checks.  A missing or falsy
    key is treated as False so existing entries need no changes.
    """
    return bool(spec.get("not_applicable"))


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


def _carry_unavailable(node: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """UNABLE_TO_ANSWER and NOT_APPLICABLE are both terminal and value-free by
    definition, so the usual "no value in resume_data means MISSING" rule would
    wipe them out every cycle.

    Carry such verdicts forward verbatim instead.
    """
    if node and node.get("completeness_status") in (STATUS_UNABLE_TO_ANSWER, STATUS_NOT_APPLICABLE):
        return _leaf_verdict(node)
    return None


def prune_for_judgment(
    resume: Dict[str, Any],
    coverage: Dict[str, Any],
    previous_status: Dict[str, Any]
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    already_decided: Dict[str, Any] = {}
    to_judge: Dict[str, Any] = {}

    for block, spec in coverage.items():
        if _is_not_applicable(spec):
            # Short-circuit: never send NA blocks to the LLM or ask about them.
            # Storing NOT_APPLICABLE in already_decided means merge_completeness
            # carries it through and merge_status_preserving_terminal (via
            # TERMINAL_STATUSES) prevents the batched grader from overwriting it.
            already_decided[block] = {
                "completeness_status": STATUS_NOT_APPLICABLE,
                "reason": "block marked not_applicable in coverage schema",
                "confidence": 1.0,
            }
            continue

        kind = block_kind(block)
        prev_block = previous_status.get(block)

        if kind == "list_object":
            _prune_list_object_block(resume, block, spec, prev_block, already_decided, to_judge)
        elif "fields" in spec:
            _prune_singular_block(resume, block, spec, prev_block, already_decided, to_judge)
        else:
            _prune_atomic_block(resume, block, prev_block, already_decided, to_judge)

    return already_decided, to_judge


def _prune_atomic_block(resume, block, prev_block, already_decided, to_judge):
    """Blocks with no field breakdown: every list_string block (skills,
    achievements, awards, languages, additional_information)."""
    value = _array_value(resume.get(block))

    if value is None:
        carried = _carry_unavailable(prev_block)
        if carried is not None:
            already_decided[block] = carried
            return
        # Still open (never terminal) -- keep showing it to JOB 2 every cycle
        # instead of short-circuiting straight to MISSING, so an explicit
        # "I don't have any X" declaration made anywhere in the transcript
        # (not just inside a targeted round about this block) has a chance
        # to be caught. See combined_prompts.py JOB 2's UNABLE_TO_ANSWER verdict.
        to_judge[block] = {"value": []}
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
        carried = _carry_unavailable(prev_block)
        if carried is not None:
            already_decided[block] = carried
            return
        # Still open -- see _prune_atomic_block for why this no longer
        # short-circuits to MISSING unconditionally.
        to_judge[block] = {"fields_to_judge": {}, "missing_fields": list(spec["fields"])}
        return

    needs_verdict, context_only, decided_fields, missing_names = {}, {}, {}, []
    for field in spec["fields"]:
        value = _scalar_value(target.get(field))
        if value is None:
            carried = _carry_unavailable(prev_fields.get(field))
            if carried is not None:
                decided_fields[field] = carried
                continue
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
        carried = _carry_unavailable(prev_block)
        if carried is not None:
            already_decided[block] = carried
            return
        # Still open -- see _prune_atomic_block for why this no longer
        # short-circuits to MISSING unconditionally.
        to_judge[block] = {"items_to_judge": []}
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
                carried = _carry_unavailable(prev_fields.get(field))
                if carried is not None:
                    decided_fields[field] = carried
                    continue
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

        if _leaf_status(settled) == STATUS_UNABLE_TO_ANSWER:
            result[block] = settled
            continue

        if not isinstance(llm_node, dict):
            # COMPLETENESS_RESPONSE_SCHEMA declares blocks as a bare
            # {"type": "object"} with strict=False, so NOTHING below the top
            # level is validated -- a block can come back as a string, a list,
            # anything. Treat a malformed block as "the LLM said nothing about
            # this one" rather than letting it take down the whole cycle.
            llm_node = None

        if llm_node is None:
            result[block] = settled if settled is not None else dict(_MISSING_LEAF)
            continue

        node: Dict[str, Any] = {
            "completeness_status": llm_node.get("completeness_status", STATUS_PARTIAL),
            "reason": llm_node.get("reason"),
            "confidence": llm_node.get("confidence"),
        }

        decided_fields = _as_leaf_map((settled or {}).get("fields"))
        llm_fields = _as_leaf_map(llm_node.get("fields"))
        if decided_fields or llm_fields:
            node["fields"] = {**decided_fields, **llm_fields}

        decided_items = (settled or {}).get("items")
        llm_items = llm_node.get("items")
        if decided_items or llm_items:
            merged_items = _merge_items(decided_items, llm_items)
            if merged_items:
                node["items"] = merged_items

        if block_kind(block) == "list_object" and node.get("items"):
            node = _recompute_list_object_status(node, coverage.get(block, {}).get("fields") or {})

        result[block] = node
    return result


def _recompute_list_object_status(node: Dict[str, Any], field_specs: Dict[str, Any]) -> Dict[str, Any]:
    """Overrides a list-object block's own top-level `completeness_status`
    with a Python-derived verdict, mirroring `next_target._item_level_target`'s
    own per-item exclusion rule exactly: SUFFICIENT only once EVERY existing
    item has no open field left (every one of `field_specs` resolved --
    filled in, explicitly declined, or given up on after the per-round
    question cap), PARTIAL otherwise.

    The LLM's own holistic verdict for these blocks used a looser "at least
    one item is good enough" bar (see `coverage_schema.py`'s old wording),
    which meant the label could say SUFFICIENT while another item still had
    fields the interview would go on to skip asking about entirely, since
    `next_target.py` used to trust this same label to decide whether the
    whole block was worth looking at. Deriving it here in Python instead
    keeps the label truthful and in permanent lockstep with what
    `next_target.py` actually does, rather than depending on the model's
    aggregate judgment matching a mechanical per-field rule it's never
    perfectly reliable at applying consistently across every item.

    A pure re-labeling function: never touches `node["items"]` itself, and
    is only ever called when `node["items"]` is non-empty (an empty-items
    block's status is JOB 2's own call to make -- including a legitimate
    UNABLE_TO_ANSWER for a spontaneous whole-block decline).
    """
    items = node["items"]
    all_terminal = all(
        (item.get("fields") or {}).get(field, {}).get("completeness_status") in TERMINAL_STATUSES
        for item in items
        for field in field_specs
    )
    node = dict(node)
    if all_terminal:
        node["completeness_status"] = STATUS_SUFFICIENT
        node["reason"] = "every captured item has no open field left"
        node["confidence"] = 1.0
    else:
        node["completeness_status"] = STATUS_PARTIAL
        node["reason"] = "at least one captured item still has an open field"
        node["confidence"] = 1.0
    return node


def _as_leaf_map(fields: Any) -> Dict[str, Any]:
    """A `fields` mapping with every non-dict leaf dropped. Same reasoning as
    the block guard in merge_completeness: the response schema doesn't police
    this shape, and a string where a verdict belongs would blow up every later
    reader of field_completeness, not just this merge."""
    if not isinstance(fields, dict):
        return {}
    return {
        name: leaf for name, leaf in fields.items()
        if isinstance(name, str) and isinstance(leaf, dict)
    }


def _items_by_id(items: Any) -> Dict[str, Dict[str, Any]]:
    """An `items` list indexed by item id, skipping anything malformed."""
    if not isinstance(items, list):
        return {}
    return {
        item["id"]: item for item in items
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]
    }


def _merge_items(decided_items: Any, llm_items: Any) -> List[Dict[str, Any]]:
    by_id: Dict[str, Dict[str, Any]] = {}
    for source in (decided_items, llm_items):
        if not isinstance(source, list):
            continue
        for item in source:
            # An item the LLM returned as a bare string (the id alone, say)
            # carries no verdicts, so there is nothing to merge from it.
            if not isinstance(item, dict):
                continue
            item_id = item.get("id")
            if not isinstance(item_id, str) or not item_id:
                continue
            entry = by_id.setdefault(item_id, {"id": item_id, "fields": {}})
            entry["fields"].update(_as_leaf_map(item.get("fields")))
    return list(by_id.values())


def _keep_terminal(existing: Optional[Dict[str, Any]], incoming: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Pick between two verdicts for the same leaf: a terminal verdict already
    on record wins over a non-terminal replacement.

    SUFFICIENT and UNABLE_TO_ANSWER are decisions taken from the conversation
    itself (the candidate answered the question, or said they can't). The
    batched grader only sees resume_data, where a decline leaves no value at
    all, so it would re-derive MISSING and the question would be asked again.
    """
    if _leaf_status(existing) in TERMINAL_STATUSES and _leaf_status(incoming) not in TERMINAL_STATUSES:
        return existing
    return incoming if incoming is not None else existing


def merge_status_preserving_terminal(
    existing: Dict[str, Any],
    incoming: Dict[str, Any],
) -> Dict[str, Any]:
    """Fold a freshly computed field_completeness onto the stored one without
    destroying terminal verdicts.

    `apply_field_completeness` used to assign `incoming` wholesale, which meant
    every batched grading cycle could clobber a verdict the interview director
    had just committed per-answer -- most visibly re-asking for something the
    candidate had already declined. This keeps the fresh result as the base
    (it's the authority on everything still in motion) and only holds back
    leaves whose existing verdict is terminal.

    Pure: neither argument is mutated.
    """
    result: Dict[str, Any] = {}

    for block in set(existing or {}) | set(incoming or {}):
        old_node = (existing or {}).get(block)
        new_node = (incoming or {}).get(block)

        # Same defensive posture as merge_completeness: a leaf that isn't a
        # dict has no verdict to preserve, so let the other side win outright.
        if not isinstance(new_node, dict):
            if isinstance(old_node, dict):
                result[block] = old_node
            continue
        if not isinstance(old_node, dict):
            result[block] = new_node
            continue

        # The block's own verdict triple, chosen independently of its children.
        node = {**new_node, **_leaf_verdict(_keep_terminal(old_node, new_node) or {})}

        old_fields = _as_leaf_map(old_node.get("fields"))
        new_fields = _as_leaf_map(new_node.get("fields"))
        if old_fields or new_fields:
            node["fields"] = {
                name: _keep_terminal(old_fields.get(name), new_fields.get(name))
                for name in set(old_fields) | set(new_fields)
            }

        old_items = _items_by_id(old_node.get("items"))
        new_items = _items_by_id(new_node.get("items"))
        if old_items or new_items:
            merged_items = []
            # Item order follows the fresh result; anything only the stored
            # state still knows about is appended rather than dropped.
            for item_id in list(new_items) + [i for i in old_items if i not in new_items]:
                old_item_fields = _as_leaf_map(old_items.get(item_id, {}).get("fields"))
                new_item_fields = _as_leaf_map(new_items.get(item_id, {}).get("fields"))
                merged_items.append({
                    "id": item_id,
                    "fields": {
                        name: _keep_terminal(old_item_fields.get(name), new_item_fields.get(name))
                        for name in set(old_item_fields) | set(new_item_fields)
                    },
                })
            node["items"] = merged_items

        result[block] = node

    return result


def _leaf_status(node: Optional[Dict[str, Any]]) -> str:
    # Non-dict tolerated on purpose: this is the single choke point every
    # status read goes through, and a malformed leaf should read as "no
    # verdict" rather than raise deep inside a live grading cycle.
    if not isinstance(node, dict):
        return STATUS_MISSING
    status = node.get("completeness_status")
    return status if isinstance(status, str) else STATUS_MISSING


def build_unable_to_answer_patch(
    field_completeness: Dict[str, Any], target: Dict[str, Any]
) -> Dict[str, Any]:
    """A single-block `field_completeness` patch marking every field named in
    `target["fields"]` UNABLE_TO_ANSWER, for the one thing the batched
    grader can never detect on its own: the candidate explicitly declining
    something (a decline leaves no `resume_data` value, so
    `prune_for_judgment` has nothing to ever judge). `target` is
    `{"block", "item_id", "fields"}` (`item_id` optional, `fields` an
    optional list) -- see `InterviewDirector._open_round`/`_finish_answer`
    for how it's produced. A single declined question
    commonly names several fields at once (Round 3's "consolidate, don't
    drip-feed" rule), so every field in the list is committed in one patch,
    not just the first.

    Copies whatever's already stored for `block` forward and overwrites only
    the leaves `target` identifies, rather than constructing a sparse new
    node -- `merge_status_preserving_terminal`'s block-level
    `_keep_terminal` compare reads the block's own top-level
    `completeness_status`, and a patch missing that key entirely would read
    as MISSING and could clobber an already-SUFFICIENT block verdict this
    call never meant to touch.

    Returns `{}` (a no-op patch) if `target` has no usable `block`.
    """
    block = target.get("block")
    if not isinstance(block, str) or not block:
        return {}

    existing_node = field_completeness.get(block)
    node: Dict[str, Any] = dict(existing_node) if isinstance(existing_node, dict) else {}

    item_id = target.get("item_id")
    raw_fields = target.get("fields")
    fields = [f for f in raw_fields if isinstance(f, str)] if isinstance(raw_fields, list) else []
    leaf = {
        "completeness_status": STATUS_UNABLE_TO_ANSWER,
        "reason": "candidate explicitly declined this during the interview",
        "confidence": 1.0,
    }

    if isinstance(item_id, str) and fields:
        items = [dict(item) for item in (node.get("items") or []) if isinstance(item, dict)]
        for item in items:
            if item.get("id") == item_id:
                item_fields = dict(item.get("fields") or {})
                for field in fields:
                    item_fields[field] = leaf
                item["fields"] = item_fields
                break
        else:
            items.append({"id": item_id, "fields": {field: leaf for field in fields}})
        node["items"] = items
    elif fields:
        node_fields = dict(node.get("fields") or {})
        for field in fields:
            node_fields[field] = leaf
        node["fields"] = node_fields
    else:
        node.update(leaf)

    return {block: node}
