# Phase 5 — CRUD Changes

## What this does

Adds `next_question`/`current_focus_path` as real session-row state,
written in the exact same call (and under the exact same lock) as
`field_completeness` already is — not a new CRUD method, not a new lock
acquisition. `apply_field_completeness` becomes the single commit point
for "this cycle's verdicts, this cycle's question, and the sticky pointer
for next cycle", which is what lets `phase-6`'s worker reuse its existing
`asyncio.shield(...)` unconditional-commit-after-LLM-returns semantics for
the question too, with zero new cancellation bookkeeping.

The debug `_status.json` export switches from dumping bare
`field_completeness` to a small wrapper object that also includes the two
new fields — still just a direct dump of real state, no derivation step.

## File to modify: `backend/app/meeting_room/data/crud_interfaces.py`

Current file (for reference — this is what exists today):

```python
from typing import Any, Dict, List, Optional, Protocol, Tuple

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
        the same way as apply_resume_update. Also marks the session's
        final_pass_completed flag True, regardless of whether `updates` was
        truthy. Returns (accepted, rejected)."""
        ...


    async def apply_field_completeness(
        self,
        session_id: str,
        completeness_status: Dict[str, Any],
        *,
        llm_usage: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Replace the session's stored field_completeness wholesale with the
        freshly merged result (see completeness_status.merge_completeness),
        and fold llm_usage into the running cost accumulator the same way
        apply_resume_update does. Committed unconditionally the instant it's
        called — the caller (silence_completeness_worker) is responsible for
        only calling this once a result is final and should not be discarded."""
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

**Change 1** — `apply_field_completeness`'s Protocol signature gains two
new keyword params:

```python
    async def apply_field_completeness(
        self,
        session_id: str,
        completeness_status: Dict[str, Any],
        *,
        next_question: Optional[Dict[str, Any]] = None,
        current_focus_path: Optional[str] = None,
        llm_usage: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Replace the session's stored field_completeness wholesale with the
        freshly merged result (see completeness_status.merge_completeness),
        set next_question (the {target_type, target_path, question} dict to
        deliver to the voice bot, or None) and current_focus_path (the
        sticky target path for the next cycle's select_focus_target call,
        or None once resolved), and fold llm_usage into the running cost
        accumulator the same way apply_resume_update does. Committed
        unconditionally the instant it's called — the caller
        (silence_completeness_worker) is responsible for only calling this
        once a result is final and should not be discarded."""
        ...
```

## File to modify: `backend/app/meeting_room/data/crud.py`

Current file (for reference — this is what exists today):

```python
import asyncio
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple
import json
from pathlib import Path

from loguru import logger

from app.meeting_room.data.crud_interfaces import STATUS_ACTIVE
from app.meeting_room.resume_analysis_pipeline.config_jsons_definitions.resume_schema import empty_resume
from app.meeting_room.resume_analysis_pipeline.merge import (
    apply_resolved_conflicts,
    merge_unresolved,
    merge_updates,
    remove_unresolved,
)

JSON_EXPORT_DIR = Path(__file__).resolve().parents[3]/"json"

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


    def _write_resume_json_sync(self, session_id: str, row: Dict[str, Any]) -> None:
        JSON_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        path = JSON_EXPORT_DIR / f"{session_id}.json"

        final_pass_completed = row.get("final_pass_completed", False)
        ended_at = row.get("ended_at")
        session_duration = (
            row.get("session_duration", 0)
            if ended_at
            else compute_session_duration(row.get("started_at"))
        )

        export = {
            "session_id": row["session_id"],
            "status": "completed" if final_pass_completed else "pending",
            "final_pass_completed": final_pass_completed,
            "session_status": row["status"],
            "start_time": row.get("started_at"),
            "end_time": ended_at,
            "session_duration": session_duration,
            "llm_calls": row["llm_cost"]["calls"],
            "llm_cost": row["llm_cost"],
            "resume_data": row["resume_data"],
        }

        with path.open("w", encoding="utf-8") as f:
            json.dump(export, f, indent=2, ensure_ascii=False)

        status_path = JSON_EXPORT_DIR / f"{session_id}_status.json"
        with status_path.open("w", encoding="utf-8") as f:
            json.dump(row.get("field_completeness", {}), f, indent=2, ensure_ascii=False)


    async def _write_resume_json(self, session_id: str, row: Dict[str, Any]) -> None:
        try:
            await asyncio.to_thread(self._write_resume_json_sync, session_id, row)
        except Exception:
            logger.exception(f"RESUME ROOM: Failed to write debug JSON export for session {session_id}")

    

    async def create_session(self, *, room_name: str, room_url: str) -> Dict[str, Any]:
        session_id = str(uuid.uuid4())
        row = {
            "session_id": session_id,
            "room_name": room_name,
            "room_url": room_url,
            "status": STATUS_ACTIVE,
            "transcript": [],
            "resume_data": empty_resume(),
            "field_completeness": {},
            "final_pass_completed": False,
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
            await self._write_resume_json(session_id, row)
        return row


    async def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        return self._sessions.get(session_id)


    async def append_transcript_line(self, session_id: str, role: str, text: str) -> None:
        async with self._lock:
            row = self._sessions.get(session_id)
            if row is None:
                return
            row["transcript"].append({"role": role, "text": text, "ts": now_iso()})


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
            await self._write_resume_json(session_id, row)
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

            row["final_pass_completed"] = True
            self._fold_llm_usage(row, llm_usage)
            await self._write_resume_json(session_id, row)
            return accepted, rejected


    async def apply_field_completeness(
        self,
        session_id: str,
        completeness_status: Dict[str, Any],
        *,
        llm_usage: Optional[Dict[str, Any]] = None,
    ) -> None:
        async with self._lock:
            row = self._sessions.get(session_id)
            if row is None:
                return

            row["field_completeness"] = completeness_status
            self._fold_llm_usage(row, llm_usage)
            await self._write_resume_json(session_id, row)


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

**Change 1** — `_write_resume_json_sync`'s status-file write now wraps
three fields instead of dumping `field_completeness` bare:

```python
        status_path = JSON_EXPORT_DIR / f"{session_id}_status.json"
        status_export = {
            "field_completeness": row.get("field_completeness", {}),
            "next_question": row.get("next_question"),
            "current_focus_path": row.get("current_focus_path"),
        }
        with status_path.open("w", encoding="utf-8") as f:
            json.dump(status_export, f, indent=2, ensure_ascii=False)
```

**Change 2** — `create_session`'s row gains two new keys, right after
`"field_completeness": {}`:

```python
        row = {
            "session_id": session_id,
            "room_name": room_name,
            "room_url": room_url,
            "status": STATUS_ACTIVE,
            "transcript": [],
            "resume_data": empty_resume(),
            "field_completeness": {},
            "next_question": None,
            "current_focus_path": None,
            "final_pass_completed": False,
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
```

**Change 3** — `apply_field_completeness` gains the two params and writes
them into the row:

```python
    async def apply_field_completeness(
        self,
        session_id: str,
        completeness_status: Dict[str, Any],
        *,
        next_question: Optional[Dict[str, Any]] = None,
        current_focus_path: Optional[str] = None,
        llm_usage: Optional[Dict[str, Any]] = None,
    ) -> None:
        async with self._lock:
            row = self._sessions.get(session_id)
            if row is None:
                return

            row["field_completeness"] = completeness_status
            row["next_question"] = next_question
            row["current_focus_path"] = current_focus_path
            self._fold_llm_usage(row, llm_usage)
            await self._write_resume_json(session_id, row)
```

## Key design points, explained

- **`next_question`/`current_focus_path` are always overwritten, including
  with `None`.** This is deliberate, not a missing default-preservation
  guard: `phase-6`'s worker recomputes both from scratch every cycle it
  actually runs (`select_focus_target` + the fresh verdict), so `None`
  is a real, current answer ("nothing to ask right now" / "focus just
  resolved"), not a stale gap to fill in from what was there before.
- **Still one lock acquisition, one write, one debug-export call** — no
  new CRUD method, no second `async with self._lock` block anywhere. This
  is what lets `phase-6` wrap the whole thing in a single
  `asyncio.shield(...)` the same way it already shields
  `apply_field_completeness` today.
- **The `_status.json` export's top-level shape changes** (from a bare
  `field_completeness` dict to `{field_completeness, next_question,
  current_focus_path}`). This is a debug file with no other consumer in
  the codebase, so this is a safe, unversioned change — same reasoning
  the original completeness-pipeline docs already used when they switched
  this same file from a `field_status.py`-derived dump to a
  `field_completeness`-derived one.
