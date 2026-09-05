# Phase 1 — `objective_priority` in the Coverage Schema

## What this does

Adds one new key, `"objective_priority"` (int, 1 = highest), alongside the
existing `"importance"`/`"complete_when"` on every **top-level** block in
`COVERAGE_SCHEMA`. Per-field entries are untouched — field-level ordering
within a block is decided separately in `phase-2` by each field's existing
`"importance"` (required → recommended → optional), not by a new number.

This is the data `select_focus_target` (`phase-2`) sorts blocks by when
there's no sticky focus to continue. Deliberately plain data, not derived
from anything — easy to re-order later by editing one integer per block.

Default order (personal-first, then the two "meat" required blocks, then
required skills, then everything else in roughly resume-conventional
order):

| Block | `objective_priority` |
| --- | --- |
| `personal` | 1 |
| `summary` | 2 |
| `experience` | 3 |
| `education` | 4 |
| `skills` | 5 |
| `projects` | 6 |
| `certifications` | 7 |
| `courses` | 8 |
| `achievements` | 9 |
| `awards` | 10 |
| `languages` | 11 |
| `additional_information` | 12 |

## File to modify: `backend/app/meeting_room/resume_analysis_pipeline/config_jsons_definitions/coverage_schema.py`

Only the block-level `{"importance": ..., "complete_when": ...}` headers
change — every `"fields"` sub-dict, and everything else in the file, is
untouched. Here is the **full updated file**, ready to paste over the
existing one:

```python
from typing import Any, Dict, FrozenSet


COVERAGE_SCHEMA: Dict[str, Dict[str, Any]] = {
    "personal": {
        "importance": "required",
        "objective_priority": 1,
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
        "objective_priority": 2,
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
        "objective_priority": 3,
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
        "objective_priority": 4,
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
        "objective_priority": 6,
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
        "objective_priority": 7,
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
        "objective_priority": 8,
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
        "objective_priority": 5,
        "complete_when": (
            "A meaningful, specific list of the candidate's core skills/"
            "technologies has been captured -- not just one or two generic terms."
        ),
    },
    "achievements": {
        "importance": "optional",
        "objective_priority": 9,
        "complete_when": "Achievements listed are specific and attributable to the candidate, not generic claims.",
    },
    "awards": {
        "importance": "optional",
        "objective_priority": 10,
        "complete_when": "Named awards include what they were awarded for.",
    },
    "languages": {
        "importance": "optional",
        "objective_priority": 11,
        "complete_when": "Languages the candidate knows have been listed, if relevant.",
    },
    "additional_information": {
        "importance": "optional",
        "objective_priority": 12,
        "complete_when": "Anything materially relevant that doesn't fit another block has been captured.",
    },
}


def coverage_block_keys() -> FrozenSet[str]:
    return frozenset(COVERAGE_SCHEMA.keys())
```

## Key design points, explained

- **`objective_priority` numbers are not contiguous with `importance`** —
  e.g. `skills` is `importance: required` but has `objective_priority: 5`,
  after `experience`/`education` which are also `required`. The two fields
  answer different questions: `importance` says how much a gap should
  weigh against a block's own aggregate verdict (used unchanged by the
  existing completeness-grading LLM call); `objective_priority` says
  *which block to ask about first* when several are open at once (new,
  used only by `phase-2`'s `select_focus_target`). Don't conflate them or
  try to derive one from the other.
- **No field-level priority number is added.** Within one block, `phase-2`
  orders fields to probe by their existing per-field `"importance"`
  (`required` → `recommended` → `optional`), then by dict-declaration
  order as the tiebreak — reusing data that's already there rather than
  adding a second priority axis at the field level.
- **Numbers are free-standing ints, not required to be contiguous or
  unique** — `select_focus_target`'s sort is a plain `sorted(..., key=...)`,
  so two blocks could share a priority (whichever comes first in the
  dict's own key order wins the tie) if that's ever wanted later; the
  default set above just happens to be a clean 1-12 permutation.
