# Phase 11 — Cleanup and Verification

## What this does

Retires the module this whole feature replaces, and lays out the manual
end-to-end verification steps — there's no automated test suite in this
repo (`CLAUDE.md`'s testing-approach convention), so this is the checklist
to actually run through by hand after transcribing every phase.

## File to delete: `backend/app/meeting_room/resume_analysis_pipeline/field_status.py`

Safe to delete once `phase-5`'s Change 1 (dropping `crud.py`'s import of
`compute_field_status`) has landed — that import was field_status.py's
**only** caller anywhere in the codebase. Its output was the thing this
whole feature replaces (a naive presence-only signal explicitly called
"wrong"), not something to keep alongside the new real signal.

Before deleting, it's worth a quick sanity grep to confirm nothing else
picked up a dependency on it in the meantime:

```
grep -rn "field_status" backend/app --include=*.py
```

Once `phase-5` is applied, the only remaining hits should be inside
`field_status.py` itself — delete the file once that's confirmed.

## Manual verification steps

1. **Start the backend and frontend locally**, join a session as the
   candidate, matching the existing "run it end-to-end" testing approach
   this repo already relies on for pipeline changes.

2. **Exercise the MISSING → TO_JUDGE → SUFFICIENT/PARTIAL → CARRIED path
   across two or more silence events**, since a single round can't exercise
   the carry-forward logic at all:
   - Say your name and email, then go silent for 2+ seconds without
     speaking again for at least a few more seconds (past the LLM call's
     own turnaround time). Confirm `field_completeness.personal` shows up
     with `name`/`email` judged and `phone`/`location`/etc. still `MISSING`.
   - Speak again, add your phone number, go silent again. Confirm the
     *second* run's LLM payload (temporarily add a debug log inside
     `_run_one_cycle` printing `to_judge`, or inspect provider request
     logs if any exist) shows `name`/`email` **absent** from
     `fields_to_judge` if they were already judged `SUFFICIENT` the first
     time, or still present if the first pass judged them `PARTIAL`.

3. **Exercise both cancellation points directly**:
   - Speak, then pause for under 2 seconds, then speak again — confirm
     `field_completeness` is completely unchanged (the hardbound wait
     itself was cancelled; no LLM call should have fired at all).
   - Speak, pause for 2+ seconds to trigger the LLM call, then speak again
     *immediately* (before the call could plausibly have returned — a
     `resume_room_completeness_max_tokens` cap of 3000 and a real model
     call should take at least a second). Confirm the resulting
     `field_completeness` for that round is unchanged from before the
     pause (the whole judgment was discarded).
   - Speak, pause for 2+ seconds, then wait long enough for the LLM call to
     plausibly have completed before speaking again. Confirm the verdict
     from that round **is** present — this is the "commit stands even
     though the user interrupted afterward" case, the one the user
     explicitly called out as the most important behavior to get right.

4. **Inspect `{session_id}_status.json`** in the debug export directory
   (`JSON_EXPORT_DIR`, i.e. `backend/json/`) after a session — it should now
   contain the real `field_completeness` structure (block-level and
   per-field/per-item verdicts with `reason`/`confidence`), not the old flat
   MISSING/SUFFICIENT-only shape `field_status.py` used to produce.

5. **Confirm block-level aggregate verdicts behave sensibly** — e.g. an
   `experience` entry with `company`/`role`/`start_date` filled but no
   `responsibilities` should judge the block-level verdict as `PARTIAL`
   (a required field, `responsibilities`, is in that item's `missing_fields`),
   even though the individual filled-in fields might each be judged
   `SUFFICIENT` on their own.

6. **Cross-file consistency spot-check** (catches transcription slips,
   not logic bugs): `coverage_schema.py`'s block/field names must be an
   exact subset of `resume_schema.RESUME_SCHEMA`'s — a stray leftover
   `personal_projects` key or a typo'd field name would silently make
   `completeness_status.py` skip that block/field forever (`coverage.items()`
   would produce a key `resume_schema.block_kind(block)` can't resolve,
   raising a `KeyError` at the very first silence event of any session — an
   easy way to catch this immediately rather than it lurking un-triggered).

## Explicitly deferred, not part of this feature

- **Question generation from `PARTIAL`/`MISSING` verdicts** — flagged
  repeatedly since `phase-0`, this reads `field_completeness` as an input
  but has its own not-yet-designed schema and storage; nothing in this
  implementation should be adjusted in anticipation of it.
- **Re-validating an edited already-`SUFFICIENT` field** in isolation
  (`phase-0`/`phase-2`'s documented limitation) — acceptable for now; if it
  becomes a real problem, the fix is narrow (compare the stored value the
  verdict was computed against, not just the verdict's status, when
  deciding CARRIED vs TO_JUDGE) and can be layered on without touching the
  rest of this design.
