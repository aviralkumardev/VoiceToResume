# Phase 10 — Per-session resume JSON export to disk

## What this does

meeting_room's `resume_data` currently lives only in
`InMemoryResumeRoomCRUD`'s in-process dict — there's no database, so there's
no durable artifact and no easy way to just open a file and watch the resume
fill in live. This phase writes each session's full `resume_data` (all 12
schema blocks plus the `conflicts`/`unresolved` bookkeeping lists) to a
per-session JSON file:

- Created the moment the session starts (holding the empty schema shape).
- Rewritten every time `resume_data` changes — every incremental batch
  (`apply_resume_update`) and the final resolution pass
  (`apply_final_resolution`).
- Located at `backend/json/<session_id>.json` — a new top-level directory,
  separate from the checked-in schema-definition JSONs under
  `resume_analysis_pipeline/config_jsons_definitions/`.

This depends on phases 1, 2, and 6 already being in place
(`empty_resume()`, `merge_updates`, and the `apply_resume_update`/
`apply_final_resolution` methods this phase adds writes to).

## File to modify: `app/meeting_room/data/crud.py`

This picks up from phase 6's final version of this file (imports,
`create_session`, `apply_resume_update`, `apply_final_resolution` as
described there). Three changes:

### Change 1 — imports

Add `json` and `pathlib.Path` to the existing import block:

```python
import asyncio
import json
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.meeting_room.data.crud_interfaces import STATUS_ACTIVE
from app.meeting_room.resume_analysis_pipeline.config_jsons_definitions.resume_schema import empty_resume
from app.meeting_room.resume_analysis_pipeline.merge import (
    apply_resolved_conflicts,
    merge_unresolved,
    merge_updates,
    remove_unresolved,
)

# backend/app/meeting_room/data/crud.py -> parents[3] is backend/
JSON_EXPORT_DIR = Path(__file__).resolve().parents[3] / "json"
```

### Change 2 — write helper, added anywhere in the class (e.g. right before `create_session`)

```python
    def _write_resume_json_sync(self, session_id: str, resume_data: Dict[str, Any]) -> None:
        JSON_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        path = JSON_EXPORT_DIR / f"{session_id}.json"
        with path.open("w", encoding="utf-8") as f:
            json.dump(resume_data, f, indent=2, ensure_ascii=False)

    async def _write_resume_json(self, session_id: str, resume_data: Dict[str, Any]) -> None:
        await asyncio.to_thread(self._write_resume_json_sync, session_id, resume_data)
```

### Change 3 — three call sites

**`create_session`** — write the initial (empty) file right after the row
is stored, still inside the existing lock:

```python
    async def create_session(self, *, room_name: str, room_url: str) -> Dict[str, Any]:
        session_id = str(uuid.uuid4())
        row = {
            "session_id": session_id,
            "room_name": room_name,
            "room_url": room_url,
            "status": STATUS_ACTIVE,
            "transcript": [],
            "resume_data": empty_resume(),
            "llm_cost": {
                "calls": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "cost_usd": 0.0,
            },
            "session_duration": 0,
            "started_at": now_iso(),
            "ended_at": None,
            "created_at": now_iso(),
        }
        async with self._lock:
            self._sessions[session_id] = row
            await self._write_resume_json(session_id, row["resume_data"])
        return row
```

**`apply_resume_update`** — write at the end, after every merge/resolution/
unresolved step, right before returning:

```python
    async def apply_resume_update(
        self,
        session_id: str,
        updates: Optional[Dict[str, Any]],
        *,
        unresolved: Optional[List[Dict[str, Any]]] = None,
        resolved_conflicts: Optional[List[Dict[str, Any]]] = None,
        resolved_unresolved_ids: Optional[List[str]] = None,
        llm_usage: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[str], List[str]]:
        async with self._lock:
            row = self._sessions.get(session_id)
            if row is None:
                return [], []

            resume = row["resume_data"]
            accepted: List[str] = []
            rejected: List[str] = []
            if updates:
                _, accepted, rejected = merge_updates(resume, updates)

            if resolved_conflicts:
                apply_resolved_conflicts(resume, resolved_conflicts)
            if resolved_unresolved_ids:
                remove_unresolved(resume, resolved_unresolved_ids)
            if unresolved:
                merge_unresolved(resume, unresolved)

            self._fold_llm_usage(row, llm_usage)
            await self._write_resume_json(session_id, resume)
            return accepted, rejected
```

**`apply_final_resolution`** — write at the end, after the force-overwrite
and the `conflicts`/`unresolved` reset:

```python
    async def apply_final_resolution(
        self,
        session_id: str,
        updates: Optional[Dict[str, Any]],
        *,
        llm_usage: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[str], List[str]]:
        async with self._lock:
            row = self._sessions.get(session_id)
            if row is None:
                return [], []

            resume = row["resume_data"]
            accepted: List[str] = []
            rejected: List[str] = []
            if updates:
                _, accepted, rejected = merge_updates(resume, updates, force_overwrite=True)

            resume["conflicts"] = []
            resume["unresolved"] = []

            self._fold_llm_usage(row, llm_usage)
            await self._write_resume_json(session_id, resume)
            return accepted, rejected
```

Nothing else in `crud.py` changes — `get_session`, `append_transcript_line`,
`mark_finished`, `list_active`, `get_active_by_room_name`, `count_active`,
and `get_resume_room_crud` are untouched.

## Key design points, explained

- **Directory computed from `crud.py`'s own path, not `cwd`**:
  `Path(__file__).resolve().parents[3]` walks up from
  `backend/app/meeting_room/data/crud.py` through `data/` → `meeting_room/` →
  `app/` → `backend/`, so `JSON_EXPORT_DIR` always resolves to `backend/json/`
  regardless of the directory the server process happens to be launched
  from. No new `app/core/config.py` setting was added for this — the
  location is fixed and obvious, matching this repo's general preference for
  simple hard-coded defaults over configurability nothing currently needs.
- **`JSON_EXPORT_DIR.mkdir(parents=True, exist_ok=True)` runs on every
  write**, not just once at import time — it's a cheap no-op once the
  directory exists, and doing it this way means there's no import-order
  concern about creating the directory before it's needed, and it
  self-heals if the directory is ever deleted while the server is running.
- **`asyncio.to_thread` for the actual file write**: `json.dump` to a local
  file is blocking I/O; offloading it keeps the event loop free, consistent
  with this general principle elsewhere in the pitch_room/meeting_room
  codebases of not letting blocking work sit directly in an async method
  that's on a hot path (here, every extraction batch).
- **All three call sites are already inside `async with self._lock:`** —
  no new locking was introduced. The write happens with the same
  `resume_data` reference the merge/reset just finished mutating, so the
  file always reflects a fully-consistent post-merge state, never a
  half-written one.
- **Writes happen unconditionally**, even when a batch's `updates` was empty
  (a `no_update` result) or nothing was actually accepted. This is
  deliberately the simplest correct behavior: tracking "did anything
  actually change this call" just to skip a redundant (and harmless)
  rewrite would be an extra bit of state for no real benefit — the file
  write is cheap and idempotent.
- **The file is never deleted** — it's left on disk as the durable artifact
  after the session ends. No cleanup/expiry was requested, and this repo
  already caps concurrent sessions via `resume_room_max_sessions`, so nothing
  unbounded accumulates in a way that wasn't already true of `transcript`
  growing in memory during a session.
- **`ensure_ascii=False`** on `json.dump` so any non-ASCII characters in a
  candidate's spoken name/location/etc. are written as literal UTF-8 rather
  than `\uXXXX`-escaped — purely a readability choice, since the whole point
  of this file is to be human-opened.
