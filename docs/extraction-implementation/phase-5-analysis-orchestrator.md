# Phase 5 — The buffer/trigger worker loop

## What this does

The character-count buffer loop, mirroring pitch_room's
`run_analysis_worker`/`_run_batch`/`_safe_run_batch`/`_cap_carry`, scoped down
to just extraction (no completeness worker, no section-change flush —
meeting_room has no slide/section concept to flush on).

This file depends on phase 4 (`run_resume_extraction_chain`,
`run_resume_final_resolution_chain`) and on the `apply_resume_update`/
`apply_final_resolution` CRUD methods added in phase 6 — it won't run
correctly until both exist, though it can be typed in beforehand.

**Extension**: the old "flush whatever's left in the buffer" behavior on
session end is replaced outright by a dedicated **final resolution pass** —
it doesn't just flush the trailing partial buffer, it re-reads the
candidate's *entire* transcript and force-resolves every outstanding
conflict/unresolved item using complete context. `_run_batch` also now
passes through the three new response fields (`unresolved`,
`resolved_conflicts`, `resolved_unresolved_ids`) to `apply_resume_update`.

## File to create

### `app/meeting_room/resume_analysis_pipeline/analysis_orchestrator.py`

```python
"""Buffers candidate speech and runs periodic extraction batches, plus a
final full-transcript resolution pass when the session ends.

Mirrors pitch_room's run_analysis_worker, scoped down: no completeness
worker, no section-change flush — just buffer -> extract -> merge, plus a
guaranteed final resolution pass (not just a leftover-buffer flush) at
session end.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from loguru import logger

from app.core.config import settings
from app.meeting_room.data.crud_interfaces import ResumeRoomCRUD
from app.meeting_room.resume_analysis_pipeline.analysis_chain import (
    run_resume_extraction_chain,
    run_resume_final_resolution_chain,
)


def _cap_carry(text: str) -> str:
    """Caps the 'incomplete sentence' carry the LLM can hand back, so a
    pathological response can't grow the carry unboundedly across batches."""
    max_chars = (
        settings.resume_room_extraction_trigger_chars
        * settings.resume_room_extraction_max_carry_multiple
    )
    if len(text) > max_chars:
        return text[-max_chars:]
    return text


async def _safe_run_batch(
    session_id: str, accumulated_text: str, remaining_text: str, crud: ResumeRoomCRUD,
) -> str:
    """Wraps _run_batch so a bug or transient failure inside it can never
    crash the worker loop or silently drop the accumulated text."""
    try:
        return await _run_batch(session_id, accumulated_text, remaining_text, crud)
    except Exception:
        logger.exception(f"RESUME-EXTRACTION: batch failed for session {session_id}")
        return _cap_carry(remaining_text + accumulated_text)


async def _run_batch(
    session_id: str, accumulated_text: str, remaining_text: str, crud: ResumeRoomCRUD,
) -> str:
    input_text = remaining_text + accumulated_text
    session = await crud.get_session(session_id)
    if session is None:
        return ""

    resume = session.get("resume_data", {})
    result = await run_resume_extraction_chain(resume, input_text)

    updates = result.get("updates") if result.get("status") == "extracted" else None
    accepted, rejected = await crud.apply_resume_update(
        session_id,
        updates,
        unresolved=result.get("unresolved"),
        resolved_conflicts=result.get("resolved_conflicts"),
        resolved_unresolved_ids=result.get("resolved_unresolved_ids"),
        llm_usage=result.get("_llm_usage"),
    )

    logger.info(
        f"RESUME-EXTRACTION: session={session_id} input_chars={len(input_text)} "
        f"status={result.get('status')} accepted={accepted} rejected={rejected} "
        f"new_unresolved={len(result.get('unresolved') or [])} "
        f"resolved_conflicts={len(result.get('resolved_conflicts') or [])} "
        f"resolved_unresolved={len(result.get('resolved_unresolved_ids') or [])}"
    )

    return _cap_carry(result.get("remaining_text") or "")


async def _safe_run_final_pass(session_id: str, crud: ResumeRoomCRUD) -> None:
    """Wraps _run_final_pass so a bug or transient failure inside it can
    never crash the worker loop during session teardown."""
    try:
        await _run_final_pass(session_id, crud)
    except Exception:
        logger.exception(f"RESUME-FINAL-PASS: failed for session {session_id}")


async def _run_final_pass(session_id: str, crud: ResumeRoomCRUD) -> None:
    session = await crud.get_session(session_id)
    if session is None:
        return

    full_transcript = "\n".join(
        line["text"] for line in session.get("transcript", []) if line.get("role") == "user"
    )
    if not full_transcript.strip():
        logger.info(f"RESUME-FINAL-PASS: session={session_id} no candidate transcript, skipping")
        return

    resume = session.get("resume_data", {})
    result = await run_resume_final_resolution_chain(resume, full_transcript)

    accepted, rejected = await crud.apply_final_resolution(
        session_id, result.get("updates"), llm_usage=result.get("_llm_usage"),
    )

    logger.info(
        f"RESUME-FINAL-PASS: session={session_id} transcript_chars={len(full_transcript)} "
        f"accepted={accepted} rejected={rejected}"
    )


async def run_resume_analysis_worker(
    session_id: str,
    queue: "asyncio.Queue[Optional[str]]",
    crud: ResumeRoomCRUD,
) -> None:
    """Consumes candidate-only transcript chunks pushed by
    room_orchestrator.enqueue_transcript (see phase 8). Exits cleanly on a
    None sentinel (session end -> runs the final resolution pass instead of
    one more incremental batch) or on cancellation (session force-stopped)."""
    accumulated_text = ""
    remaining_text = ""
    trigger = settings.resume_room_extraction_trigger_chars

    logger.info(f"RESUME-EXTRACTION: worker starting for session {session_id}")

    try:
        while True:
            chunk = await queue.get()

            if chunk is None:
                await _safe_run_final_pass(session_id, crud)
                break

            accumulated_text += chunk + "\n"
            if len(accumulated_text) >= trigger:
                remaining_text = await _safe_run_batch(session_id, accumulated_text, remaining_text, crud)
                accumulated_text = ""
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception(f"RESUME-EXTRACTION: worker crashed for session {session_id}")
    finally:
        logger.info(f"RESUME-EXTRACTION: worker exiting for session {session_id}")
```

## Key design points, explained

- **Trigger loop** mirrors pitch_room exactly in shape: accumulate into
  `accumulated_text`, and once it crosses `resume_room_extraction_trigger_chars`
  (phase 7), run a batch and reset. The LLM's `remaining_text` (an
  incomplete trailing sentence) prepends the *next* batch via
  `remaining_text + accumulated_text` in `_run_batch`.
- **No section-change flush**: pitch_room has a second trigger path for
  slide/section transitions. meeting_room has no slides, so the only two
  ways a batch fires are (a) the character threshold, and (b) the final
  resolution pass below.
- **`_run_batch` reads `resume_data` before the LLM call** just to build the
  prompt's sparse-state view (and, now, the outstanding conflicts/unresolved
  lists). This read can be slightly stale relative to a concurrent write —
  harmless, since it's only used as *context* for the LLM, not as the target
  of the mutation. The actual mutation goes through `crud.apply_resume_update`
  (phase 6), which holds the CRUD's lock for the whole merge — that's the
  only place correctness matters, and using a simple lock there (rather than
  pitch_room's optimistic-version-retry pattern) is safe precisely because
  this read-then-mutate split means the worker never needs to retry a stale
  merge against a fresher row: single process, single lock, no other writer
  can ever race it.
- **`_safe_run_batch`** exists so that a bug in the LLM call, JSON parsing, or
  merge doesn't kill the whole worker task silently — every batch always
  returns *some* valid carry text, even on total failure (the whole
  unprocessed input, capped).

### The final-pass replacement, explained

- **The `None` sentinel now triggers `_safe_run_final_pass` instead of one
  more incremental batch on the leftover buffer.** This is a deliberate
  behavior change from the original (pre-extension) design: a full-transcript
  re-derivation is a strict superset of "flush whatever's left in
  `accumulated_text`" — the entire candidate transcript already includes
  every character that was ever queued (since `persist()` in phase 8 writes
  to both the transcript and the queue), so there's no need to also run a
  small incremental batch first. Any text still sitting in `accumulated_text`
  at session end is covered by the final pass's full-transcript read.
- **`_run_final_pass` reconstructs the candidate's complete transcript from
  `session["transcript"]`**, filtering to `role == "user"` lines and joining
  them in order — no new storage is needed, this data was already being
  durably appended by `crud.append_transcript_line` on every final
  transcription throughout the session.
- **Guarded against an empty transcript**: if somehow no candidate speech was
  ever recorded (e.g. the candidate never unmuted), the final pass logs and
  returns early rather than making a pointless LLM call with an empty
  transcript.
- **`crud.apply_final_resolution`** (phase 6) is a distinct CRUD method from
  `apply_resume_update` — it force-overwrites via
  `merge_updates(..., force_overwrite=True)` and unconditionally resets
  `conflicts`/`unresolved` to `[]` afterward, which `apply_resume_update`
  must never do (that would erase conflict/unresolved tracking on every
  ordinary incremental batch, not just at the end).
- **`_safe_run_final_pass`** exists for the same reason `_safe_run_batch`
  does: this call happens during session teardown (phase 8's `_close_out`
  awaits the worker task with a timeout), and a bug here must not prevent the
  rest of teardown (marking the session finished, deleting the Daily room)
  from completing.
