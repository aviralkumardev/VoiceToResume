# Phase 1 — Resume schema module

## What this does

Adds a static, in-repo schema description for every resume block: which kind
of structure it is (singular object / free-form object / list-of-objects /
list-of-strings), field descriptions for prompting, and helper functions the
merge and prompt-construction code need.

Unlike pitch_room's `deck_schema.py`, this is **not** reloaded from a
database — there's no DB-backed config system in this repo, so it's a plain
Python literal, populated once at import time.

The six existing JSON files
(`extraction.json`, `experience.json`, `education.json`, `project.json`,
`certification.json`, `course.json`) stay exactly as they are — they remain
the record of which *keys* exist. This module only adds descriptions and
classification on top; it doesn't replace them or read them at runtime (the
key lists below were copied directly from those files, so they need to stay
in sync if the JSON files ever change).

**Extension**: `empty_resume()` now also seeds two flat top-level keys —
`conflicts` and `unresolved` — that live alongside the 12 schema blocks but
are not schema blocks themselves. They hold the conflict-tracking and
unresolved-fact records described in phase 2.

## Files to create

### `app/meeting_room/resume_analysis_pipeline/__init__.py`

Empty file (package marker — this directory currently has no `__init__.py`,
which needs fixing now that it holds real Python modules, not just JSON).

### `app/meeting_room/resume_analysis_pipeline/config_jsons_definitions/__init__.py`

Also empty, same reason.

### `app/meeting_room/resume_analysis_pipeline/config_jsons_definitions/resume_schema.py`

```python
"""Schema definitions for the resume extraction pipeline.

Static, in-repo schema description used to (a) render the schema into the
extraction prompt so the LLM knows every valid block/field, and (b) validate
merge updates against a known set of keys. Unlike pitch_room's
deck_schema.py, this is NOT reloaded from a database — there is no DB-backed
config system in this repo, so RESUME_SCHEMA is a plain Python literal.

The JSON files in this directory (extraction.json, experience.json,
education.json, project.json, certification.json, course.json) remain the
source of truth for which KEYS exist on each block/item. This module adds
descriptions and a `kind` classification on top of those keys.
"""

from __future__ import annotations

from typing import Any, Dict, FrozenSet, List

# --- kinds ----------------------------------------------------------------
# singular            -> resume[block] is a dict of {field: {"value", "source"}}
# singular_freeform    -> same shape as singular, but any field key is accepted
# list_object          -> resume[block] is a list of dicts, each with an "id"
#                         plus scalar/array fields; matched by id in merge.py
# list_string          -> resume[block] is a flat list[str]

RESUME_SCHEMA: Dict[str, Dict[str, Any]] = {
    "personal": {
        "kind": "singular",
        "description": "The candidate's contact/identity details.",
        "fields": {
            "name": "Full name of the candidate.",
            "email": "Email address.",
            "phone": "Phone number.",
            "location": "City/region the candidate is based in.",
            "linkedin": "LinkedIn profile URL.",
            "github": "GitHub profile URL.",
            "portfolio": "Personal portfolio/website URL.",
        },
    },
    "summary": {
        "kind": "singular",
        "description": "A short professional summary/objective statement.",
        "fields": {
            "text": "The summary paragraph itself, in the candidate's own words, cleaned up.",
        },
    },
    "preferences": {
        "kind": "singular_freeform",
        "description": (
            "Any stated job preferences (desired role, location, remote/onsite, "
            "salary expectations, notice period, etc). Free-form: use a short "
            "snake_case key that names the preference."
        ),
        "fields": {},
    },
    "experience": {
        "kind": "list_object",
        "description": "Work experience, one item per role held.",
        "item_fields": {
            "company": "Employer name.",
            "role": "Job title.",
            "start_date": "Start date (any format the candidate gave, e.g. 'Jan 2020', '2020').",
            "end_date": "End date, or 'Present' if current.",
            "location": "Where this role was based.",
        },
        "item_array_fields": {
            "responsibilities": "What the candidate did in this role, one item per distinct responsibility.",
            "skills": "Skills/technologies used in this specific role.",
            "projects": "Named projects worked on in this role.",
            "achievements": "Notable achievements in this role.",
            "awards": "Awards received for this role.",
        },
    },
    "education": {
        "kind": "list_object",
        "description": "Educational history, one item per degree/program.",
        "item_fields": {
            "degree": "Degree name, e.g. 'Bachelor of Science'.",
            "field": "Field of study, e.g. 'Computer Science'.",
            "college": "Institution name.",
            "start_date": "Start date.",
            "end_date": "End date, or expected graduation.",
            "location": "Where the institution is located.",
            "grade": "GPA/grade/percentage, if mentioned.",
        },
        "item_array_fields": {},
    },
    "projects": {
        "kind": "list_object",
        "description": "Personal or academic projects (outside formal employment).",
        "item_fields": {
            "name": "Project name.",
            "description": "One or two sentence description of the project.",
            "github": "GitHub repo URL, if mentioned.",
            "deployed": "Live/deployed URL, if mentioned.",
        },
        "item_array_fields": {
            "responsibilities": "What the candidate specifically did on this project.",
            "skills": "Skills/technologies used in this project.",
            "achievements": "Notable outcomes of this project.",
        },
    },
    "certifications": {
        "kind": "list_object",
        "description": "Professional certifications.",
        "item_fields": {
            "name": "Certification name.",
            "issuer": "Issuing organization.",
            "date": "Date obtained.",
            "credential_url": "Verification/credential URL, if mentioned.",
        },
        "item_array_fields": {},
    },
    "courses": {
        "kind": "list_object",
        "description": "Standalone courses (not degrees).",
        "item_fields": {
            "name": "Course name.",
            "provider": "Course provider, e.g. 'Coursera'.",
            "date": "Date completed.",
        },
        "item_array_fields": {
            "skills": "Skills/technologies covered by this course.",
        },
    },
    "skills": {
        "kind": "list_string",
        "description": "Flat list of skills not already tied to a specific role/project.",
    },
    "achievements": {
        "kind": "list_string",
        "description": "Flat list of general achievements not already tied to a specific role/project.",
    },
    "awards": {
        "kind": "list_string",
        "description": "Flat list of general awards not already tied to a specific role.",
    },
    "languages": {
        "kind": "list_string",
        "description": "Spoken/written languages the candidate knows.",
    },
    "additional_information": {
        "kind": "list_string",
        "description": "Anything else worth capturing that doesn't fit another block.",
    },
}


def block_keys() -> FrozenSet[str]:
    return frozenset(RESUME_SCHEMA.keys())


def block_kind(block: str) -> str:
    return RESUME_SCHEMA[block]["kind"]


def singular_field_keys(block: str) -> FrozenSet[str]:
    return frozenset(RESUME_SCHEMA[block].get("fields", {}).keys())


def item_field_keys(block: str) -> FrozenSet[str]:
    return frozenset(RESUME_SCHEMA[block].get("item_fields", {}).keys())


def item_array_field_keys(block: str) -> FrozenSet[str]:
    return frozenset(RESUME_SCHEMA[block].get("item_array_fields", {}).keys())


def empty_resume() -> Dict[str, Any]:
    """The starting shape for a brand-new session's resume_data.

    "conflicts" and "unresolved" are flat top-level lists, not schema blocks —
    they never appear in RESUME_SCHEMA, so merge.py's block-name check in
    merge_updates() already rejects any attempt by the LLM to target them as
    if they were an ordinary block. They are only ever written to by the
    dedicated helpers in merge.py (_add_conflict, merge_unresolved,
    apply_resolved_conflicts, remove_unresolved)."""
    resume: Dict[str, Any] = {}
    for block, spec in RESUME_SCHEMA.items():
        resume[block] = {} if spec["kind"] in ("singular", "singular_freeform") else []
    resume["conflicts"] = []
    resume["unresolved"] = []
    return resume


def render_schema_for_prompt() -> str:
    """Human-readable block/field/kind listing embedded in the extraction prompt."""
    lines: List[str] = []
    for block, spec in RESUME_SCHEMA.items():
        lines.append(f"- {block} ({spec['kind']}): {spec['description']}")
        if spec["kind"] == "singular":
            for field, desc in spec["fields"].items():
                lines.append(f"    - {field}: {desc}")
        elif spec["kind"] == "singular_freeform":
            lines.append("    - (any snake_case key naming the preference)")
        elif spec["kind"] == "list_object":
            lines.append(
                "    - id: stable identifier for this item — echo an existing id "
                "to edit it, omit to create a new one"
            )
            for field, desc in spec["item_fields"].items():
                lines.append(f"    - {field}: {desc}")
            for field, desc in spec["item_array_fields"].items():
                lines.append(f"    - {field} (list): {desc}")
        # list_string blocks need no extra field listing — they're just strings
    return "\n".join(lines)
```

## Why these shapes

- `summary` is modeled as a one-field singular block (`{"text": {...}}`)
  rather than a bare string, so every block in `resume_data` has the uniform
  `block -> field -> {"value", "source"}` shape that `merge.py` (phase 2)
  expects for singular blocks. Whatever eventually renders the final resume
  document just needs to read `resume_data["summary"]["text"]["value"]`.
- `preferences` accepts any key (no fixed field list) because no such list
  exists anywhere in the current codebase.
- The `item_array_fields` split (e.g. `experience.responsibilities`) matters
  for phase 2's merge logic: these get **appended with dedup**, not
  overwritten, because a later batch's sparse-state view may only mention
  new items — see phase 2 for why blind overwrite there would lose data.
- `conflicts`/`unresolved` are **not** schema blocks with a `kind` — they're
  plumbing for the extension described in phase 2, deliberately kept outside
  `RESUME_SCHEMA` so nothing in the merge dispatch or prompt-schema rendering
  needs a special case for them; they're addressed directly by name in the
  small set of functions that own them.
