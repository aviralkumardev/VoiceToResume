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