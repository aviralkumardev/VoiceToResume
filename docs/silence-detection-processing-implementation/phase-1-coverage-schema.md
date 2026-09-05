# Phase 1 — Coverage Rubric

## What this does

Adds the static rubric that field-level and block-level completeness get
judged against: for every block/field covered, an `importance`
(`required`/`recommended`/`optional`) and a `complete_when` bar — a
natural-language description of what "good enough" looks like, written for
an LLM judge to apply, not for a human to eyeball presence/absence.

This mirrors `resume_schema.py`'s own convention: a plain Python module
under `config_jsons_definitions/`, not a literal `.json` file on disk.

**Block/field names here match `RESUME_SCHEMA` exactly** — this is a hard
requirement, since `completeness_status.py` (`phase-2`) walks `RESUME_SCHEMA`
and looks up the matching key in `COVERAGE_SCHEMA` directly. Two
consequences of that match:
- The user's original draft named this block `personal_projects`; it's
  renamed to `projects` here to match `RESUME_SCHEMA`'s actual block name.
- `preferences` is intentionally **absent** — it's a freeform block in
  `RESUME_SCHEMA` (`singular_freeform`, arbitrary keys), so there's nothing
  fixed to write a rubric against. It stays ungated by this feature.

**Flagged assumption**: the user's draft rubric had per-field `complete_when`
entries for `personal`, but every block needs its own block-level bar too
(that's what the block's own aggregate verdict gets judged against — see
`phase-2`). The `personal` block-level line below is synthesized, not
something the user dictated — treat it as a first draft to edit freely.
Likewise, `certifications`/`courses`/`awards`/`languages` block-level bars
are synthesized as low-stakes/optional since the user's draft didn't cover
every block explicitly — adjust importance/wording to taste.

## New file: `backend/app/meeting_room/resume_analysis_pipeline/config_jsons_definitions/coverage_schema.py`

```python
from __future__ import annotations

from typing import Any, Dict, FrozenSet

# Every block/field name below must match a key in resume_schema.RESUME_SCHEMA
# exactly -- completeness_status.py looks blocks/fields up by name across the
# two schemas. `preferences` is intentionally absent (freeform, ungated).

COVERAGE_SCHEMA: Dict[str, Dict[str, Any]] = {
    "personal": {
        "importance": "required",
        "complete_when": (
            "The candidate can be identified and reached through at least "
            "one reliable channel, and their full name is known."
        ),
        "fields": {
            "name": {
                "importance": "required",
                "complete_when": "The candidate's full name has been captured.",
            },
            "email": {
                "importance": "required",
                "complete_when": "A usable email address has been captured.",
            },
            "phone": {
                "importance": "required",
                "complete_when": "A usable phone number has been captured.",
            },
            "location": {
                "importance": "recommended",
                "complete_when": "The candidate's city/region is known.",
            },
            "linkedin": {
                "importance": "recommended",
                "complete_when": "A LinkedIn profile URL has been captured.",
            },
            "github": {
                "importance": "optional",
                "complete_when": "A GitHub profile URL has been captured.",
            },
            "portfolio": {
                "importance": "optional",
                "complete_when": "A personal portfolio/website URL has been captured.",
            },
        },
    },
    "summary": {
        "importance": "required",
        "complete_when": (
            "A short, coherent professional summary (roughly 2-4 sentences) "
            "capturing who the candidate is and what they're looking for has "
            "been captured -- a single fragment or one-liner is not enough."
        ),
        "fields": {
            "text": {
                "importance": "required",
                "complete_when": (
                    "The summary text is a real paragraph, not just a "
                    "restated job title or a single sentence fragment."
                ),
            },
        },
    },
    "experience": {
        "importance": "required",
        "complete_when": (
            "At least one relevant work experience is sufficiently covered "
            "(company, role, dates, and real responsibilities) -- it's fine "
            "if other experience entries are still thin, as long as one is solid."
        ),
        "fields": {
            "company": {
                "importance": "required",
                "complete_when": "The employer name has been captured.",
            },
            "role": {
                "importance": "required",
                "complete_when": "The job title has been captured.",
            },
            "start_date": {
                "importance": "required",
                "complete_when": "A start date (any granularity) has been captured.",
            },
            "end_date": {
                "importance": "recommended",
                "complete_when": "An end date, or 'Present' for a current role, has been captured.",
            },
            "location": {
                "importance": "optional",
                "complete_when": "Where the role was based has been captured.",
            },
            "responsibilities": {
                "importance": "required",
                "complete_when": (
                    "At least 2-3 concrete responsibilities or contributions "
                    "have been described -- not just the job title restated."
                ),
            },
            "skills": {
                "importance": "recommended",
                "complete_when": "Skills/technologies actually used in this role have been named.",
            },
            "projects": {
                "importance": "optional",
                "complete_when": "Named projects worked on in this role have been captured.",
            },
            "achievements": {
                "importance": "optional",
                "complete_when": "Notable achievements in this role have been captured.",
            },
            "awards": {
                "importance": "optional",
                "complete_when": "Awards received for this role have been captured.",
            },
        },
    },
    "education": {
        "importance": "required",
        "complete_when": (
            "At least one educational qualification is sufficiently covered "
            "(degree, field of study, and institution)."
        ),
        "fields": {
            "degree": {
                "importance": "required",
                "complete_when": "The degree name has been captured.",
            },
            "field": {
                "importance": "required",
                "complete_when": "The field of study has been captured.",
            },
            "college": {
                "importance": "required",
                "complete_when": "The institution name has been captured.",
            },
            "start_date": {
                "importance": "recommended",
                "complete_when": "A start date has been captured.",
            },
            "end_date": {
                "importance": "recommended",
                "complete_when": "An end date, or expected graduation, has been captured.",
            },
            "location": {
                "importance": "optional",
                "complete_when": "Where the institution is located has been captured.",
            },
            "grade": {
                "importance": "optional",
                "complete_when": "A GPA/grade/percentage has been captured, if the candidate mentioned one.",
            },
        },
    },
    "projects": {
        "importance": "recommended",
        "complete_when": (
            "At least one project is sufficiently covered -- what it is and "
            "what the candidate specifically did on it."
        ),
        "fields": {
            "name": {
                "importance": "required",
                "complete_when": "The project name has been captured.",
            },
            "description": {
                "importance": "required",
                "complete_when": "A one-or-two sentence description of what the project does has been captured.",
            },
            "github": {
                "importance": "optional",
                "complete_when": "A GitHub repo URL has been captured, if one exists.",
            },
            "deployed": {
                "importance": "optional",
                "complete_when": "A live/deployed URL has been captured, if one exists.",
            },
            "responsibilities": {
                "importance": "recommended",
                "complete_when": "What the candidate specifically did on this project has been described.",
            },
            "skills": {
                "importance": "recommended",
                "complete_when": "Skills/technologies used in this project have been named.",
            },
            "achievements": {
                "importance": "optional",
                "complete_when": "Notable outcomes of this project have been captured.",
            },
        },
    },
    "certifications": {
        "importance": "optional",
        "complete_when": "Any mentioned certification has both a name and an issuing organization.",
        "fields": {
            "name": {
                "importance": "required",
                "complete_when": "The certification name has been captured.",
            },
            "issuer": {
                "importance": "recommended",
                "complete_when": "The issuing organization has been captured.",
            },
            "date": {
                "importance": "optional",
                "complete_when": "The date obtained has been captured.",
            },
            "credential_url": {
                "importance": "optional",
                "complete_when": "A verification/credential URL has been captured, if one exists.",
            },
        },
    },
    "courses": {
        "importance": "optional",
        "complete_when": "Any mentioned course has both a name and a provider.",
        "fields": {
            "name": {
                "importance": "required",
                "complete_when": "The course name has been captured.",
            },
            "provider": {
                "importance": "recommended",
                "complete_when": "The course provider has been captured.",
            },
            "date": {
                "importance": "optional",
                "complete_when": "The date completed has been captured.",
            },
            "skills": {
                "importance": "optional",
                "complete_when": "Skills/technologies covered by the course have been named.",
            },
        },
    },
    "skills": {
        "importance": "required",
        "complete_when": (
            "A meaningful, specific list of the candidate's core skills/"
            "technologies has been captured -- not just one or two generic terms."
        ),
    },
    "achievements": {
        "importance": "optional",
        "complete_when": "Achievements listed are specific and attributable to the candidate, not generic claims.",
    },
    "awards": {
        "importance": "optional",
        "complete_when": "Named awards include what they were awarded for.",
    },
    "languages": {
        "importance": "optional",
        "complete_when": "Languages the candidate knows have been listed, if relevant.",
    },
    "additional_information": {
        "importance": "optional",
        "complete_when": "Anything materially relevant that doesn't fit another block has been captured.",
    },
}


def coverage_block_keys() -> FrozenSet[str]:
    return frozenset(COVERAGE_SCHEMA.keys())
```

## Key design points, explained

- **No `kind` field here.** `COVERAGE_SCHEMA` doesn't repeat `singular` /
  `list_object` / `list_string` — `completeness_status.py` always asks
  `resume_schema.block_kind(block)` for that, so the two schemas can never
  disagree about a block's shape.
- **List-object blocks use one flat `fields` dict**, not separate
  `item_fields`/`item_array_fields` like `RESUME_SCHEMA` does — the coverage
  rubric doesn't care whether a field is scalar or array, only whether it's
  covered well; `completeness_status.py` cross-references `RESUME_SCHEMA`'s
  `item_field_keys()`/`item_array_field_keys()` when it needs to know how to
  read a given field's current value out of `resume_data`.
- **List-string blocks have no `fields` key at all** — they're judged as one
  atomic unit (there's no per-skill completeness, only "is the skills list
  as a whole good enough").
- **`coverage_block_keys()`** exists for `phase-11`'s verification step, to
  assert it's a subset of `resume_schema.block_keys()`.
