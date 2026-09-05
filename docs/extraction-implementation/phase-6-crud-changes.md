# Phase 6 — CRUD changes (`resume_data` storage + merge entry points)

## What this does

Adds a `resume_data` field (and a small `llm_cost` accumulator) to the
in-memory session row, and two new methods — `apply_resume_update` (every
incremental batch) and `apply_final_resolution` (the one-time session-end
final pass) — that run phase 2's merge helpers under the CRUD's existing
`asyncio.Lock`.

This is the entire persistence layer for this feature. No version field, no
compare-and-swap, no retry loop — `InMemoryResumeRoomCRUD` is single-process
and already serializes all mutations through one lock, so holding that lock
for the whole merge gives the same safety pitch_room gets from optimistic
locking against a shared Supabase table, at a fraction of the code.

**Extension**: `apply_resume_update` now also threads through
`unresolved`/`resolved_conflicts`/`resolved_unresolved_ids`, applying them via
phase 2's `merge_unresolved`/`apply_resolved_conflicts`/`remove_unresolved`.
A new `apply_final_resolution` method force-overwrites via
`merge_updates(..., force_overwrite=True)` and unconditionally clears
`conflicts`/`unresolved` afterward.

**See also `phase-10-json-export.md`**: after this phase's changes are in
place, phase 10 adds a small additional diff on top of this same file
(`crud.py`) that writes each session's `resume_data` to a per-session JSON
file on disk (`backend/json/<session_id>.json`), created at
`create_session` and rewritten at the end of both `apply_resume_update` and
`apply_final_resolution`. Apply phase 10's diff after this phase's.

## File to modify: `app/meeting_room/data/crud_interfaces.py`

Current file (for reference — this is what exists today):

```python
from typing import Any, Dict, List, Optional, Protocol

STATUS_ACTIVE = "active"
STATUS_ENDED = "ended"
STATUS_FAILED = "failed"
STATUS_TIMED_OUT = "timed_out"


class ResumeRoomCRUD(Protocol):
    """Structural interface the orchestrator depends on — any object with these
    methods works, in-memory or otherwise."""

    async def create_session(self, *, room_name: str, room_url: str) -> Dict[str, Any]:
        """Create a new active session row and return it."""
        ...

    async def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a session row by its id, or None if it doesn't exist."""
        ...

    async def append_transcript_line(self, session_id: str, role: str, text: str) -> None:
        """Append one transcript line to the session's transcript."""
        ...

    async def mark_finished(self, session_id: str, status: str, error: Optional[str] = None) -> None:
        """Transition an active session to a terminal status, no-op if already terminal."""
        ...

    async def list_active(self) -> List[Dict[str, Any]]:
        """Return every session row currently in the active status."""
        ...

    async def get_active_by_room_name(self, room_name: str) -> Optional[Dict[str, Any]]:
        """Fetch the active session row for a given Daily room name, if any."""
        ...

    async def count_active(self) -> int:
        """Count how many sessions are currently active."""
        ...
```

**Change 1** — the `typing` import gains `Tuple`:

```python
from typing import Any, Dict, List, Optional, Protocol, Tuple
```

**Change 2** — add these two methods to the `ResumeRoomCRUD` Protocol,
anywhere inside the class body (e.g. right after `append_transcript_line`):

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
        """Merge `updates` into the session's resume_data (if truthy), apply
        any resolved_conflicts/resolved_unresolved_ids, append any newly
        flagged unresolved items, and fold llm_usage into the running cost
        accumulator (regardless of whether updates is truthy — a no_update
        batch still cost tokens). Returns (accepted, rejected) field-path
        lists from the merge."""
        ...

    async def apply_final_resolution(
        self,
        session_id: str,
        updates: Optional[Dict[str, Any]],
        *,
        llm_usage: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[str], List[str]]:
        """Force-applies `updates` from the session-end final resolution pass
        (bypassing conflict-diversion), then unconditionally clears
        resume_data's conflicts and unresolved lists, then folds llm_usage
        the same way as apply_resume_update. Returns (accepted, rejected)."""
        ...
```

## File to modify: `app/meeting_room/data/crud.py`

Current file (for reference — this is what exists today):

```python
import asyncio
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Dict, List, Optional

from app.meeting_room.data.crud_interfaces import STATUS_ACTIVE


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_session_duration(started_at: Optional[str]) -> int:
    if not started_at:
        return 0
    try:
        started_dt = datetime.fromisoformat(started_at)
        if started_dt.tzinfo is None:
            started_dt = started_dt.replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - started_dt).total_seconds()))
    except (ValueError, TypeError):
        return 0


class InMemoryResumeRoomCRUD:
    def __init__(self) -> None:
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def create_session(self, *, room_name: str, room_url: str) -> Dict[str, Any]:
        session_id = str(uuid.uuid4())
        row = {
            "session_id": session_id,
            "room_name": room_name,
            "room_url": room_url,
            "status": STATUS_ACTIVE,
            "transcript": [],
            "session_duration": 0,
            "started_at": now_iso(),
            "ended_at": None,
            "created_at": now_iso(),
        }
        async with self._lock:
            self._sessions[session_id] = row
        return row

    async def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        return self._sessions.get(session_id)

    async def append_transcript_line(self, session_id: str, role: str, text: str) -> None:
        async with self._lock:
            row = self._sessions.get(session_id)
            if row is None:
                return
            row["transcript"].append({"role": role, "text": text, "ts": now_iso()})

    async def mark_finished(self, session_id: str, status: str, error: Optional[str] = None) -> None:
        async with self._lock:
            row = self._sessions.get(session_id)
            if row is None or row["status"] != STATUS_ACTIVE:
                return
            row["status"] = status
            row["ended_at"] = now_iso()
            row["session_duration"] = compute_session_duration(row.get("started_at"))

    async def list_active(self) -> List[Dict[str, Any]]:
        return [row for row in self._sessions.values() if row["status"] == STATUS_ACTIVE]

    async def get_active_by_room_name(self, room_name: str) -> Optional[Dict[str, Any]]:
        for row in self._sessions.values():
            if row["room_name"] == room_name and row["status"] == STATUS_ACTIVE:
                return row
        return None

    async def count_active(self) -> int:
        return len(await self.list_active())


@lru_cache()
def get_resume_room_crud() -> InMemoryResumeRoomCRUD:
    return InMemoryResumeRoomCRUD()
```

**Change 1** — imports: add `Tuple` to the `typing` import, and add imports
for the schema/merge helpers from phases 1–2 (one-directional dependency —
`resume_analysis_pipeline` never imports from `data/`, so this can't create a
circular import):

```python
import asyncio
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

from app.meeting_room.data.crud_interfaces import STATUS_ACTIVE
from app.meeting_room.resume_analysis_pipeline.config_jsons_definitions.resume_schema import empty_resume
from app.meeting_room.resume_analysis_pipeline.merge import (
    apply_resolved_conflicts,
    merge_unresolved,
    merge_updates,
    remove_unresolved,
)
```

**Change 2** — `create_session`'s row gains two new keys (`resume_data` and
`llm_cost`):

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
        return row
```

**Change 3** — new methods, placed anywhere in the class (e.g. right after
`append_transcript_line`). Both share the same cost-folding logic, factored
into a small private helper:

```python
    def _fold_llm_usage(self, row: Dict[str, Any], llm_usage: Optional[Dict[str, Any]]) -> None:
        if not llm_usage:
            return
        cost = row["llm_cost"]
        cost["calls"] += 1
        cost["prompt_tokens"] += llm_usage.get("prompt_tokens") or 0
        cost["completion_tokens"] += llm_usage.get("completion_tokens") or 0
        cost["total_tokens"] += llm_usage.get("total_tokens") or 0
        cost["cost_usd"] += llm_usage.get("cost") or 0.0

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
            return accepted, rejected

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
            return accepted, rejected
```

## Key design points, explained

- **`resume_data: empty_resume()`** gives every new session the phase-1
  starting shape (empty dicts for singular blocks, empty lists for
  list/list-object blocks, and empty `conflicts`/`unresolved` lists) —
  `merge.py`'s `setdefault` calls would technically handle a missing key too,
  but starting from the full shape makes the row's contents predictable for
  anything that reads it later (e.g. the phase-9 debug endpoint, or a future
  resume-rendering consumer).
- **`_fold_llm_usage` is shared** between both methods rather than duplicated
  — both a no_update incremental batch and the final pass still burn tokens
  on the LLM call, and pitch_room's own cost accounting folds usage in
  unconditionally for exactly this reason.
- **`apply_resume_update`'s ordering** — merge normal `updates` first, then
  apply `resolved_conflicts` (which force-writes specific fields directly,
  bypassing conflict-diversion since it *is* the resolution), then
  `remove_unresolved` (dropping records whose fact was just folded into
  `updates` with proper attribution), then `merge_unresolved` (appending any
  *new* ambiguous facts this batch flagged). This order means a single batch
  can simultaneously resolve an old item and flag a brand new one without
  them interfering.
- **`apply_final_resolution` is a distinct method, not a flag on
  `apply_resume_update`**, because its semantics are categorically different:
  `force_overwrite=True` on the merge, and an *unconditional* reset of
  `conflicts`/`unresolved` to `[]` regardless of what `updates` did or didn't
  address — per the confirmed design, the final pass had the complete
  transcript, so its silence on a given conflict/unresolved item is treated
  as "nothing more to resolve," not as "leave it pending forever." Folding
  this behavior into `apply_resume_update` via a parameter would risk an
  incremental batch accidentally being called with the wrong flag and wiping
  tracking state mid-session.
- **Everything happens inside one `async with self._lock:` block** in both
  methods — the read of `row["resume_data"]`, the merge, the
  resolution/unresolved bookkeeping, and the cost update are all atomic with
  respect to every other CRUD method (all of which also acquire
  `self._lock`). This is the entire concurrency story for this feature;
  nothing else is needed given the single-process, in-memory nature of this
  CRUD.
