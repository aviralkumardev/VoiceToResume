# Phase 9 — Debug endpoint and end-to-end verification

## What this does

There's no dashboard for `resume_data` today, and this repo has no
structured logger like pitch_room's `get_logger`/`log_with_context`
(confirmed via grep — it doesn't exist here). This phase adds a minimal
dev-only inspection endpoint plus relies on the `loguru.logger` calls already
added in phases 4–5, so you can actually watch this feature work.

No changes needed to the endpoint itself for the conflict/unresolved/final-pass
extension — `resume_data` already includes `conflicts` and `unresolved` as
ordinary top-level keys (seeded by phase 1's `empty_resume()`), so they show
up automatically in the existing debug response. The verification steps below
are extended with new scenarios to exercise.

## File to modify: `app/meeting_room/routes.py`

Current file in full (for reference):

```python
from fastapi import APIRouter, Depends, HTTPException

from app.meeting_room.models import StartSessionResponse, StopSessionResponse
from app.meeting_room.room_orchestrator import ResumeRoomOrchestrator, get_orchestrator_instance


router = APIRouter(
    prefix="/resume-room",
    tags=["resume-room"],
    responses={404: {"description": "Not found"}},
)


@router.post("/start", response_model=StartSessionResponse)
async def start_session(
    orchestrator: ResumeRoomOrchestrator = Depends(get_orchestrator_instance)
):
    try:
        return await orchestrator.start_session()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail="RESUME ROOM: Failed to start the session.") from exc


@router.post("/stop/{room_name}", response_model=StopSessionResponse)
async def stop_session(
    room_name: str,
    orchestrator: ResumeRoomOrchestrator = Depends(get_orchestrator_instance)
):
    try:
        return await orchestrator.stop_session(room_name)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail="RESUME ROOM: Failed to stop the session.") from exc
```

Add this new route anywhere after the existing two:

```python
@router.get("/debug/{session_id}")
async def debug_session(
    session_id: str,
    orchestrator: ResumeRoomOrchestrator = Depends(get_orchestrator_instance),
):
    """DEV-ONLY: inspects a session's extracted resume_data and llm_cost.
    No auth on this route — do not expose it on a public deployment as-is."""
    session = await orchestrator._crud.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Unknown session_id")
    return {
        "resume_data": session.get("resume_data"),
        "llm_cost": session.get("llm_cost"),
        "transcript_lines": len(session.get("transcript", [])),
    }
```

`session_id` isn't returned by `/resume-room/start` today (only `roomUrl`,
`token`, `roomName`) — for local testing, grab it from the server log lines
added in phases 4–5 (they log `session=<id>`), or from
`await orchestrator._crud.list_active()` if you're poking at it from a REPL.

## Manual end-to-end test steps

1. Start the backend, `POST /resume-room/start`, join the returned room as
   the candidate.
2. Speak several sentences describing your background, e.g.: *"I worked at
   Google as a backend software engineer from 2020 to 2022, where I built
   Python services. I also have a computer science degree from Stanford."*
3. Watch the server console — once roughly 360+ characters of candidate
   speech accumulate, the phase-5 log line should fire:
   `RESUME-EXTRACTION: session=... input_chars=... status=... accepted=... rejected=...`
4. `GET /resume-room/debug/{session_id}` and confirm:
   - `resume_data.experience` has one item with an `id`, `company: "Google"`,
     `role`, `start_date`/`end_date` populated.
   - `resume_data.education` has one item with `college: "Stanford"`.
   - `llm_cost.calls >= 1` and `llm_cost.cost_usd > 0`.
5. Speak a follow-up that adds detail to the *same* role, e.g. *"There I
   built backend services in Python."* Confirm via the debug endpoint that
   the existing `experience` item was updated **in place** (same `id`, no
   duplicate item), and that `responsibilities` accumulated the new item
   rather than replacing what was there.
6. **Conflict test**: mention a differing value for an already-captured
   field, e.g. having earlier said "graduated in 2020," now say "actually I
   graduated in 2019." Confirm via the debug endpoint that
   `resume_data.conflicts` gains one record with `field: "end_date"`,
   `existing_value: "2020"`, `candidates: ["2019"]`, and that
   `education[].end_date` is **unchanged** (still 2020) — no silent
   overwrite. Watch for the `RESUME-EXTRACTION:` log line's
   `new_unresolved=`/`resolved_conflicts=` counters to confirm the batch that
   created it.
7. **Incremental resolution test**, same session: speak a clarifying
   follow-up, e.g. *"To be clear, 2019 is the correct graduation year, not
   2020."* Confirm the conflict record disappears from
   `resume_data.conflicts` and `education[].end_date` updates to `2019`
   **before** the session ends — this should show up as
   `resolved_conflicts=1` in that batch's log line.
8. **Unresolved test**: describe a fact that's ambiguous between two existing
   `experience` items (e.g. two separate roles at the same company) without
   clarifying which one it belongs to. Confirm via the debug endpoint that it
   lands in `resume_data.unresolved` (with a `block`/`text`/`note`) rather
   than being guessed onto either item — this should show up as
   `new_unresolved=1` in that batch's log line.
9. `POST /resume-room/stop/{room_name}` — confirm the **final resolution
   pass** runs: watch for a `RESUME-FINAL-PASS:` log line (distinct from
   `RESUME-EXTRACTION:`) reporting `transcript_chars=`/`accepted=`/
   `rejected=`. Immediately call the debug endpoint again and confirm:
   - `resume_data.conflicts` and `resume_data.unresolved` are both `[]`.
   - Any conflict/unresolved item left outstanding from steps 6/8 (if you
     didn't resolve it incrementally in step 7) is now correctly reflected
     directly in the resume fields — e.g. `education[].end_date` holds
     whichever value the full transcript actually supports, and the
     ambiguous experience fact from step 8 is now attached to the correct
     item's `id`.

## Things to double-check while implementing (not fully verified by research)

- **`resume_room_extraction_model`/`resume_room_final_pass_model` slugs**
  (phase 7) — `"anthropic/claude-sonnet-4.5"` is a placeholder for both;
  confirm the exact model slug(s) your OpenRouter account can actually call
  before relying on them.
- **`app/services/llm_providers` package `__init__.py` exports** — phase 4's
  imports (`LLMMessage`, `LLMProvider`, `LLMProviderError`,
  `LLMProviderFactory`, `LLMRequestOptions`, `LLMResponse`,
  `LLMSchemaValidationError`, `SchemaSpec`) were all confirmed present in
  `app/services/llm_providers/__init__.py`'s `__all__` list at the time this
  was written (`validate_against_schema` is no longer imported directly by
  phase 4 — that validation now happens inside `generate_json()` itself) —
  if that package has since changed, re-check the import line compiles.
- **`__init__.py` files** — `resume_analysis_pipeline/` and
  `config_jsons_definitions/` currently have no `__init__.py` at all (they
  only held JSON files before this feature). Phase 1 calls for adding empty
  ones; don't skip this or the new imports may behave unexpectedly depending
  on how Python resolves namespace packages in your environment.
