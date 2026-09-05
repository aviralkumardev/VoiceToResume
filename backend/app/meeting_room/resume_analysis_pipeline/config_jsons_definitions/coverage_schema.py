from typing import Any, Dict, FrozenSet, Optional


COVERAGE_SCHEMA: Dict[str, Dict[str, Any]] = {
    "personal": {
        "importance": "required",
        "not_applicable": True,
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
        "not_applicable": True,
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


def askable_coverage_schema(
    coverage: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Dict[str, Any]]:
    """`coverage` (defaults to `COVERAGE_SCHEMA`) with every not_applicable
    block removed -- the set of topics the interview director may ever ask
    a spoken question about. `personal`/`summary` are captured elsewhere in
    the product, never through a spoken interview question."""
    source = COVERAGE_SCHEMA if coverage is None else coverage
    return {block: spec for block, spec in source.items() if not spec.get("not_applicable")}


ASKABLE_COVERAGE_SCHEMA: Dict[str, Dict[str, Any]] = askable_coverage_schema()