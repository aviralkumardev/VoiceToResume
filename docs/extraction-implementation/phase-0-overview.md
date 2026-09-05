# Resume Extraction — Implementation Overview

## What this is

Ports pitch_room's transcript-to-structured-JSON extraction mechanism
(`WoodenScaleAI/woodenscale-ai/backend/app/pitch_room/pitch_analysis_pipeline/`)
into meeting_room, adapted to build a **resume** instead of a pitch deck.

As the candidate speaks, their final (non-interim) transcript lines accumulate
in a buffer. Once the buffer crosses a character threshold, an LLM call
extracts whatever resume facts the new text supports, and the result is merged
field-by-field into a `resume_data` structure kept on the session row.

## Confirmed scope and design decisions

- **List-block merge**: repeatable resume sections (experience, education,
  projects, certifications, courses) are matched by an `id` the LLM echoes
  back from the current state it was shown; omitting/mismatching the id means
  "this is a new item."
- **Transcript source**: only the candidate's own speech feeds the extraction
  buffer — never the bot's own TTS text.
- **Out of scope**: no completeness grading, no Flow-2-style
  interrupt/follow-up questioning. Extraction + merge only.
- **No optimistic-locking/CAS**: pitch_room needs version+retry because
  Supabase is shared across multiple web workers. meeting_room's CRUD
  (`InMemoryResumeRoomCRUD`) is in-memory, single-process, and already
  guarded by one `asyncio.Lock` — so a lock-guarded mutation gives the same
  atomicity with far less code. This is a deliberate simplification, not a
  missing piece.

## Why it isn't a line-for-line port

Pitch deck blocks are all fixed singular objects, so pitch_room's merge is
pure field-overwrite. Resume blocks are a mix:

- **Singular** (`personal`, `summary`, `preferences`) — same field-overwrite
  semantics as pitch_room.
- **List-of-object** (`experience`, `education`, `projects`, `certifications`,
  `courses`) — someone has multiple jobs/degrees, so these need the
  id-match-or-append addressing scheme above. Pitch_room has no analog for
  this.
- **List-of-string** (`skills`, `achievements`, `awards`, `languages`,
  `additional_information`) — plain arrays, appended with dedup. Another
  category pitch_room doesn't have.

Also, because only candidate speech is ever queued (no interviewer lines),
the extraction prompt doesn't need pitch_room's "which speaker's words count
as evidence" disambiguation at all.

## Files touched

**New files** (all under `app/meeting_room/`):
1. `resume_analysis_pipeline/config_jsons_definitions/resume_schema.py`
2. `resume_analysis_pipeline/merge.py`
3. `resume_analysis_pipeline/analysis_prompts.py`
4. `resume_analysis_pipeline/analysis_chain.py`
5. `resume_analysis_pipeline/analysis_orchestrator.py`
6. `resume_analysis_pipeline/__init__.py` and
   `resume_analysis_pipeline/config_jsons_definitions/__init__.py` (both
   currently missing — needed since these are now real Python packages, not
   just a folder of JSON files)

**Modified files**:
6. `data/crud_interfaces.py` — new Protocol method
7. `data/crud.py` — new `resume_data`/`llm_cost` row fields, new mutation method
8. `app/core/config.py` (repo root: `app/core/config.py`) — new
   `resume_room_extraction_*` settings
9. `room_orchestrator.py` — spawn/track/tear-down the new worker task
10. `stt_tts_pipeline/pipeline.py` — feed the worker's queue from `persist()`
11. `routes.py` — a dev-only debug endpoint to observe `resume_data` growing

## Build order

Each phase file below is self-contained and can be typed in independently, in
this order (later phases import from earlier ones):

| Phase file | What it adds |
| --- | --- |
| `phase-1-resume-schema.md` | Schema module — the foundation everything else imports |
| `phase-2-merge.md` | `merge_updates()` — depends on phase 1 |
| `phase-3-analysis-prompts.md` | Prompt construction — depends on phase 1 |
| `phase-4-analysis-chain.md` | The LLM call itself — depends on phase 3 |
| `phase-5-analysis-orchestrator.md` | The buffer/trigger loop — depends on phases 2 and 4, and the CRUD method from phase 6 |
| `phase-6-crud-changes.md` | `resume_data` storage + `apply_resume_update` — depends on phases 1 and 2 |
| `phase-7-config.md` | New settings — no dependencies |
| `phase-8-wiring.md` | Spawns the worker, feeds it from the bot pipeline — depends on phases 5 and 6 |
| `phase-9-debug-and-verification.md` | Observability + manual end-to-end test steps — depends on phase 6 |

You can type phases 1–4 and 7 in any order relative to each other, but don't
wire phase 8 in until phases 5 and 6 both exist, or `room_orchestrator.py`
will import a module that doesn't exist yet.

## Reference mechanism (pitch_room), for context

- **Trigger**: a character-count buffer (`pitch_room_extraction_trigger_chars`,
  360) accumulated in the analysis worker's own loop. Crossing it fires a
  batch and resets the buffer. The LLM can also return a `remaining_text`
  "incomplete sentence" carry that prepends the next batch.
- **Prompt**: sends only the new chunk (never the full transcript) plus the
  full schema description plus a *sparse* view of already-populated fields.
- **Response**: `{"reasoning", "updates": {...}, "remaining_text", "status"}` —
  a partial payload, never a full replacement.
- **Merge**: schema-validated field-level overwrite, run inside an
  optimistic-lock retry callback (needed there because Supabase is shared
  across workers — not needed here).
- **Provider**: a dedicated task-keyed provider/model, decoupled from the
  conversational bot's own LLM.
