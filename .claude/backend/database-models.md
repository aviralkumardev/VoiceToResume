# Backend: Session Data / CRUD

## Purpose
Persistence layer for a resume-room session: transcript lines, the
accumulating structured resume document, conflict/unresolved tracking,
the interview's round-based question ledger, and running LLM cost — plus a
debug JSON export written to disk on every mutation. Currently in-memory
only (no real database yet).

## Key files
- `backend/app/meeting_room/data/crud_interfaces.py` — the `ResumeRoomCRUD`
  `Protocol` (structural interface) and session status constants.
- `backend/app/meeting_room/data/crud.py` — `InMemoryResumeRoomCRUD`, the
  only current implementation, plus `get_resume_room_crud()` accessor.
- `backend/json/*.json` — debug export dir: `{session_id}.json` (full
  session snapshot, including `resume_data` **and** `questions`) plus a
  companion `{session_id}_status.json` (a direct dump of just
  `field_completeness`, see below), both written on every state change.

## Public surface
- `STATUS_ACTIVE`, `STATUS_ENDED`, `STATUS_FAILED`, `STATUS_TIMED_OUT` —
  session status string constants used across the meeting_room domain.
- `ResumeRoomCRUD` protocol methods: `create_session`, `get_session`,
  `append_transcript_line`, `apply_resume_update`, `apply_final_resolution`,
  `apply_field_completeness`, `start_round`, `append_round_question`,
  `record_round_answer`, `close_round`, `mark_finished`, `list_active`,
  `get_active_by_room_name`, `count_active`. See docstrings in
  `crud_interfaces.py` for exact semantics of each, especially the
  accepted/rejected field-path tuple returned by
  `apply_resume_update`/`apply_final_resolution`.
- `apply_field_completeness(session_id, completeness_status, *, llm_usage=None)`
  — folds the freshly merged result (see
  [backend/completeness-pipeline.md](completeness-pipeline.md)'s
  `merge_completeness`) onto the stored `field_completeness` via
  `completeness_status.merge_status_preserving_terminal`, and folds
  `llm_usage` the same way `apply_resume_update` does. The fresh result
  wins everywhere **except** a leaf whose stored verdict is already
  terminal (`SUFFICIENT` / `UNABLE_TO_ANSWER`) and whose incoming one is
  not. This is the **sole** writer of `field_completeness` in the codebase
  — the interview director no longer writes any verdict of its own (the old
  `apply_answer_verdict` is deleted along with the target-selection model
  it served). Committed unconditionally the instant it's called — the
  caller (`silence_completeness_worker`, or the post-extraction-batch
  trigger) is responsible for only calling this once a result is final.
- **`start_round(session_id, *, question_text, forced_topic=None, max_questions=None, target=None, turn_latency_seconds=None) -> Optional[str]`**
  — opens a new round: creates `questions["rounds"][round_id]` with one
  exchange (`{"question": question_text, "answer": None, "asked_at": ...,
  "answered_at": None, "latency_seconds": turn_latency_seconds}`), appends
  `round_id` to `round_order`, sets `current_round_id`/`awaiting_answer=True`,
  and returns the new `round_id`. `max_questions` defaults to
  `settings.resume_room_max_questions_per_round`, stamped once at creation.
  `target` is `{"block", "item_id", "fields"}` (`item_id` nullable, `fields`
  an optional list of field names) describing what this round's question is
  about — stored verbatim as
  `rounds[round_id]["target"]`, see the round shape below and
  [backend/stt-tts-pipeline.md](stt-tts-pipeline.md) for how it's built and
  later used. `turn_latency_seconds` is the caller's own
  `time.monotonic()`-measured elapsed seconds since the previous answer was
  recorded — see the latency gotcha below; `None` for the opening question /
  idle recovery, which have no preceding graded answer.
- **`append_round_question(session_id, round_id, question_text, *, turn_latency_seconds=None) -> None`**
  — appends one more exchange (a probe) to an already-open round, without
  touching `forced_topic` or `current_round_id`. `turn_latency_seconds` — same
  as `start_round`'s, landing on this probe exchange instead.
- **`record_round_answer(session_id, round_id, answer_text, *, answered_at=None) -> None`** —
  fills `answer`/`answered_at` on the round's **most recent exchange whose
  answer is still `None`** (walks `exchanges` in reverse), and clears
  `awaiting_answer`. Load-bearing distinction from the old per-target
  message log: an exchange is a fixed one-shot `{question, answer}` slot,
  not an append-only list, so a caller must never fill this slot with
  anything other than the real answer to that exact question (see the
  meta-question gotcha below). `answered_at` should be the moment the
  answer was *finalized* (silence debounce elapsed, right before grading
  started), not whenever this method happens to actually run — see the
  gotcha below; falls back to `now()` only if the caller passes nothing.
- **`close_round(session_id, round_id, *, grade, llm_usage=None) -> None`**
  — sets `status="closed"`, stamps `grade`/`closed_at`, folds `llm_usage`,
  and clears `current_round_id`/`awaiting_answer` if this was the active
  round.
- `get_resume_room_crud() -> InMemoryResumeRoomCRUD` — `@lru_cache()`d
  singleton accessor, same pattern as the orchestrator's singleton.

## Data flow & dependencies
- `apply_resume_update`/`apply_final_resolution` delegate the actual
  merge logic to `merge.py` — see
  [backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md).
- `empty_resume()` (from `resume_schema.py`) seeds every new session's
  `resume_data`. `create_session` separately seeds `row["questions"] =
  {"current_round_id": None, "awaiting_answer": False, "round_order": [],
  "rounds": {}}` — a sibling key on the row, **not** nested inside
  `resume_data`/`RESUME_SCHEMA`, so extraction merge logic (`merge.py`)
  never has to special-case it. Each `rounds[round_id]` entry is:
  ```python
  {
      "round_id": str,
      "status": "open" | "closed",
      "grade": Optional[str],        # SUFFICIENT | PARTIAL | UNABLE_TO_ANSWER
      "forced_topic": Optional[str], # "conflict:<id>" | "unresolved:<id>" | "gap:<block>" | None
      "target": Optional[dict],      # {"block", "item_id", "fields"} -- what this round is about, or None
      "max_questions": int,          # stamped at open, from resume_room_max_questions_per_round
      "exchanges": [
          {
              "question": str, "answer": Optional[str], "asked_at": iso, "answered_at": Optional[iso],
              "latency_seconds": Optional[float],  # see the latency gotcha below
          },
          ...
      ],
      "opened_at": iso, "closed_at": Optional[iso],
  }
  ```
  One round per subject; probes append additional exchanges to the same
  round rather than creating a new one. This replaces the old per-target
  `threads`/`settled_paths`/`pending_claims`/`reopened` shape entirely — the
  interview director no longer tracks per-path settlement, exhaustion, or an
  unverified-claim ledger of any kind, since it no longer selects targets or
  writes verdicts at all. `InterviewDirector._build_conversation_history`
  (see [backend/stt-tts-pipeline.md](stt-tts-pipeline.md)) flattens every
  round's `exchanges` in `round_order` order into the flat
  `[{"question", "answer"}, ...]` history handed to the per-answer grading
  chain.
- Written to by: `room_orchestrator` (`create_session`, `mark_finished`),
  the STT/TTS pipeline's `persist()` closure (`append_transcript_line`), the
  resume-analysis worker (`apply_resume_update`, `apply_final_resolution`),
  the batched completeness worker (`apply_field_completeness`, also fired
  from the post-extraction-batch trigger), and the interview director
  (`start_round`, `append_round_question`, `record_round_answer`,
  `close_round`, plus `append_transcript_line` for meta-question asides) —
  see [backend/stt-tts-pipeline.md](stt-tts-pipeline.md) and
  [backend/completeness-pipeline.md](completeness-pipeline.md).
- Every mutating method writes a debug JSON snapshot to
  `backend/json/{session_id}.json` via `_write_resume_json` (off the
  asyncio event loop, via `asyncio.to_thread`). The same call also writes
  `backend/json/{session_id}_status.json` — a direct dump of just the
  row's `field_completeness`. **Not awaited by the mutating method itself**
  — `_schedule_write` deep-copies `row` while `self._lock` is still held (so
  the snapshot is consistent with the mutation just applied), then fires the
  actual write as a background `asyncio.create_task`, referenced in
  `self._write_tasks` so it can't be GC'd mid-flight. Actual disk writes for
  the same session still serialize against each other, via a **separate**
  per-session `self._write_locks[session_id]` (never `self._lock`), so a
  slower write can't clobber a newer snapshot on disk.

## Conventions & gotchas
- **In-memory only** — `_sessions: Dict[str, Dict]` lives in process memory
  behind a single `asyncio.Lock`. Restarting the backend loses all session
  state. If a real DB is ever added, it must implement the
  `ResumeRoomCRUD` protocol shape exactly (structural typing — no explicit
  inheritance required).
- **A round's exchange slot is fixed, not append-only.** `record_round_answer`
  fills the most recent exchange whose `answer` is still `None` — calling it
  with anything other than the literal answer to that exact question (e.g.
  an off-script meta-question aside) permanently fills the slot and leaves
  no place for the real answer that follows. `InterviewDirector._finish_answer`
  deliberately skips this call entirely on the meta-question path, recording
  the aside only as `user_aside`/`assistant_aside` transcript lines instead
  — see [backend/stt-tts-pipeline.md](stt-tts-pipeline.md).
- The debug JSON export directory is computed as
  `Path(__file__).resolve().parents[3] / "json"` (i.e.
  `backend/json/`) — moving `crud.py` to a different depth breaks this
  path arithmetic.
- `_write_resume_json` failures are logged (`logger.exception`) but never
  raised — a broken debug export must never break the session.
- `mark_finished` is a no-op if the session is already non-active — a
  session's terminal status is set exactly once.
- **`answered_at`/`asked_at` are meant to be read as a turn-latency
  measure**, not just a record of "answer happened." `InterviewDirector._finish_answer`
  (see [backend/stt-tts-pipeline.md](stt-tts-pipeline.md)) captures
  `answered_at` at the top of the method — right after the silence debounce
  elapsed, before the grading LLM call — and passes it into
  `record_round_answer` explicitly, even though that call is itself deferred
  until after grading returns (the write is off the critical path; the
  timestamp it carries is not). Do not let a future caller pass `None`/omit
  `answered_at` and let this method stamp `now()` at write-time instead —
  that would fold the grading call's own latency into `answered_at`, making
  `next_exchange.asked_at - this_exchange.answered_at` measure nothing
  (this was a real bug, fixed 2026-09-05).
- **`latency_seconds` on an exchange is the turn-latency number itself —
  no timestamp arithmetic required.** Superseding the `asked_at`/`answered_at`
  subtraction described above: `InterviewDirector` now measures the actual
  "candidate finished answering → next question asked by TTS" gap directly
  with `time.monotonic()` (captured as `turn_started` at the top of
  `_finish_answer`, before the grading call) and passes the elapsed seconds
  straight into `start_round`/`append_round_question` as
  `turn_latency_seconds`, landing on the exchange that opens as
  `latency_seconds`. This is the number to read for "how long did this turn
  take" — `asked_at`/`answered_at` remain correct (see above) and still work
  for cross-checking, but `latency_seconds` is direct and doesn't require
  parsing ISO timestamps or subtracting across two different exchanges.
  `None` on the opening question / idle-recovery exchanges, which have no
  preceding graded answer to measure from. See
  [backend/stt-tts-pipeline.md](stt-tts-pipeline.md) for exactly where each
  branch (organic next-question, forced conflict/unresolved topic,
  required-gap safety net, same-round probe) captures and forwards it —
  every branch's own extra work (LLM wording calls, `_await_task_a_settle`'s
  bounded wait) is included, since the candidate is genuinely waiting through
  all of it.

## Last synced
2026-09-05 (yet later still — added `turn_latency_seconds` to
`start_round`/`append_round_question`, stored on the opened/appended exchange
as `latency_seconds`: a direct `time.monotonic()`-measured turn-latency
number in seconds, superseding the `asked_at`/`answered_at`-subtraction
approach from the immediately preceding change — the user asked for a
seconds value, not timestamps to subtract. See the new gotcha above and
[backend/stt-tts-pipeline.md](stt-tts-pipeline.md).)
2026-09-05 (later still — `record_round_answer` gained an optional
`answered_at` kwarg; `InterviewDirector._finish_answer` now captures the
timestamp right after the silence debounce elapses and passes it through,
instead of letting the deferred write stamp `now()` after the grading LLM
call had already returned. Fixes `answered_at` so it reflects "candidate
finished speaking," not "grading finished" — needed to make
`next_exchange.asked_at - this_exchange.answered_at` a valid turn-latency
measurement. See the new gotcha above and
[backend/stt-tts-pipeline.md](stt-tts-pipeline.md).)
2026-09-05 (replaced the per-target `threads`/`settled_paths`/
`pending_claims`/`reopened` question ledger with a flat, round-based one:
`questions.rounds[round_id]` holds a sequence of fixed `{question, answer}`
exchanges under a per-round question budget. Deleted
`apply_question_target`, `apply_answer_verdict`, `file_pending_claim`,
`resolve_pending_claims`, `maybe_record_answer`; added `start_round`,
`append_round_question`, `record_round_answer`, `close_round`. Supports the
interview director's round-based rewrite — see
[backend/stt-tts-pipeline.md](stt-tts-pipeline.md) and
[backend/completeness-pipeline.md](completeness-pipeline.md). `apply_field_completeness`
is now the sole writer of `field_completeness` anywhere in the codebase.)

2026-09-05 (later same day — added `target` param to `start_round`, stored
as `rounds[round_id]["target"]`: `{"block", "item_id", "field"}` describing
what a round's question is about, self-reported by the fused question
chain for an organic question (sanitized by `InterviewDirector`), built
from the record for a forced conflict/unresolved topic, or `{"block":
gap_block}` for a required-gap topic. Lets a later `UNABLE_TO_ANSWER` grade
be committed back into `field_completeness` precisely via
`apply_field_completeness` — see
[backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md)'s
`build_unable_to_answer_patch` and
[backend/stt-tts-pipeline.md](stt-tts-pipeline.md).)

2026-09-05 (later still — `target`'s `field` key pluralized to `fields`
(`Optional[List[str]]`): a live run showed the same experience item getting
four separate rounds, one per remaining field (`location`, `projects`,
`achievements`, `awards`), because the target shape could only name one
field at a time. Now one round's question can consolidate every
currently-open field of an item/block at once, self-reported as a list and
committed together by `build_unable_to_answer_patch` on a decline — see
[backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md) and
[backend/stt-tts-pipeline.md](stt-tts-pipeline.md).)
