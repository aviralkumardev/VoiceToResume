# Backend: Session Data / CRUD

## Purpose
Persistence layer for a resume-room session: transcript lines, the
accumulating structured resume document, conflict/unresolved tracking,
the interview's round-based question ledger (including the upcoming-question
queue), and running LLM cost — plus a debug JSON export written to disk on
every mutation. Currently in-memory only (no real database yet).

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
  `record_round_answer`, `close_round`, `apply_question_queue`,
  `pop_question_queue_head`, `mark_target_given_up`,
  `mark_forced_topic_spent`, `mark_finished`, `list_active`,
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
  not. This is the **sole** writer of `field_completeness` in the codebase.
  Committed unconditionally the instant it's called — the caller (the
  analysis worker's combined-chain batch, or `InterviewDirector` for an
  `UNABLE_TO_ANSWER` decline patch) is responsible for only calling this
  once a result is final.
- **`start_round(session_id, *, question_text, forced_topic=None, max_questions=None, target=None, turn_latency_seconds=None) -> Optional[str]`**
  — opens a new round: creates `questions["rounds"][round_id]` with one
  exchange (`{"question": question_text, "answer": None, "asked_at": ...,
  "answered_at": None, "latency_seconds": turn_latency_seconds}`), appends
  `round_id` to `round_order`, sets `current_round_id`/`awaiting_answer=True`,
  and returns the new `round_id`. `max_questions` defaults to
  `settings.resume_room_max_questions_per_round`, stamped once at creation.
  `target` is `{"block", "item_id", "fields"}` (`item_id` nullable, `fields`
  an optional list of field names) describing what this round's question is
  about — stored verbatim as `rounds[round_id]["target"]`, see the round
  shape below and [backend/stt-tts-pipeline.md](stt-tts-pipeline.md) for how
  it's built and later used. `turn_latency_seconds` — see the latency
  gotcha below; `None` for the opening question / idle recovery.
- **`append_round_question(session_id, round_id, question_text, *, turn_latency_seconds=None) -> None`**
  — appends one more exchange (a probe) to an already-open round, without
  touching `forced_topic` or `current_round_id`.
- **`record_round_answer(session_id, round_id, answer_text, *, answered_at=None) -> None`** —
  fills `answer`/`answered_at` on the round's **most recent exchange whose
  answer is still `None`** (walks `exchanges` in reverse), and clears
  `awaiting_answer`. An exchange is a fixed one-shot `{question, answer}`
  slot, not an append-only list — see the meta-question gotcha below.
  `answered_at` should be the moment the answer was *finalized* (silence
  debounce elapsed, right before grading started); falls back to `now()`
  only if the caller passes nothing.
- **`close_round(session_id, round_id, *, grade, llm_usage=None) -> None`**
  — sets `status="closed"`, stamps `grade`/`closed_at`, folds `llm_usage`,
  and clears `current_round_id`/`awaiting_answer` if this was the active
  round. `grade: Optional[str]` — `None` for the opening round, which is
  never graded (a multi-block opener has no single `complete_when` bar);
  every other round passes one of `question_chain`'s `ANSWER_GRADE_*`
  values.
- **`apply_question_queue(session_id, queue: List[Dict[str, Any]]) -> None`**
  — wholesale-overwrites `questions.queue` with the freshly regenerated
  candidate list from the combined analysis call. Never patched
  incrementally — every cycle recomputes the whole thing fresh, so this
  always replaces rather than merges. The caller
  (`analysis_orchestrator._run_batch`) only calls this when the combined
  call actually returned a queue — `None` means "call failed, leave the
  persisted queue untouched" (see `combined_chain`'s fail-soft contract,
  [backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md)) and
  must never reach this method.
- **`pop_question_queue_head(session_id) -> Optional[Dict[str, Any]]`** —
  atomically pops and returns `questions.queue[0]` (`None` if the queue is
  empty or the session doesn't exist). The only queue read/write
  `InterviewDirector` needs — the popped item's question text is already
  fully worded by the combined call, so no further LLM call is needed to
  ask it.
- **`mark_target_given_up(session_id, block, item_id) -> None`** — adds
  this target's `gap_key` to `questions.given_up_targets` (no-op if already
  present). Persisted equivalent of what used to be an in-memory set on
  `InterviewDirector` — needed now because candidate-queue regeneration
  runs in a different asyncio task (the analysis worker) than the one that
  decided to give up on this target.
- **`mark_forced_topic_spent(session_id, key) -> None`** — adds `key` (a
  `"conflict:<id>"`/`"unresolved:<id>"` candidate key) to
  `questions.forced_topics_spent` (no-op if already present). Same
  cross-task-visibility reasoning as `mark_target_given_up`.
- **`mark_more_items_checked(session_id, blocks: List[str]) -> None`** —
  adds each block name to `questions.more_items_checked` (no-op for any
  already present). Called by the analysis worker after a combined-call
  cycle, from that cycle's own self-reported `more_items_asked` — the
  Python-side record of which repeatable blocks have already had their
  one-time "do you have any other X?" side-question asked, so
  `combined_prompts.SYSTEM_PROMPT`'s JOB 3 doesn't ask it again for the
  same block on a later cycle. See
  [backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md).
- `start_round`/`append_round_question` also stamp
  `questions.last_asked_question = question_text` on every write — the
  full text (acknowledgment included) of the most recently spoken
  question, fed back into the next combined-call cycle purely so its
  JOB 3 acknowledgment-rotation rule has something concrete to avoid
  repeating.
- `get_resume_room_crud() -> InMemoryResumeRoomCRUD` — `@lru_cache()`d
  singleton accessor, same pattern as the orchestrator's singleton.

## Data flow & dependencies
- `apply_resume_update`/`apply_final_resolution` delegate the actual merge
  logic to `merge.py` — see
  [backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md).
- `empty_resume()` (from `resume_schema.py`) seeds every new session's
  `resume_data`. `create_session` separately seeds:
  ```python
  row["questions"] = {
      "current_round_id": None, "awaiting_answer": False,
      "round_order": [], "rounds": {},
      "queue": [],                 # regenerated wholesale every combined-call cycle
      "given_up_targets": [],      # ["gap:<block>:<item_id-or-''>", ...]
      "forced_topics_spent": [],   # ["conflict:<id>", "unresolved:<id>", ...]
      "last_asked_question": None, # full text (ack included) of the most recently spoken question
      "more_items_checked": [],    # block names already given their one-time "any other X?" ask
  }
  ```
  a sibling key on the row, **not** nested inside `resume_data`/`RESUME_SCHEMA`,
  so extraction merge logic (`merge.py`) never has to special-case it. Each
  `rounds[round_id]` entry is:
  ```python
  {
      "round_id": str,
      "status": "open" | "closed",
      "grade": Optional[str],        # SUFFICIENT | PARTIAL | UNABLE_TO_ANSWER | None (opening round)
      "forced_topic": Optional[str], # "conflict:<id>" | "unresolved:<id>" | None
      "target": Optional[dict],      # {"block", "item_id", "fields"} -- what this round is about, or None (opening round)
      "max_questions": int,          # stamped at open, from resume_room_max_questions_per_round
      "exchanges": [
          {
              "question": str, "answer": Optional[str], "asked_at": iso, "answered_at": Optional[iso],
              "latency_seconds": Optional[float],
          },
          ...
      ],
      "opened_at": iso, "closed_at": Optional[iso],
  }
  ```
  One round per subject; probes append additional exchanges to the same
  round rather than creating a new one. `InterviewDirector._build_conversation_history`
  (see [backend/stt-tts-pipeline.md](stt-tts-pipeline.md)) flattens every
  round's `exchanges` in `round_order` order into the flat
  `[{"question", "answer"}, ...]` history handed to the per-answer grading
  chain.
- Written to by: `room_orchestrator` (`create_session`, `mark_finished`),
  the STT/TTS pipeline's `persist()` closure (`append_transcript_line`), the
  resume-analysis worker (`apply_resume_update`, `apply_final_resolution`,
  `apply_field_completeness`, `apply_question_queue`), and the interview
  director (`start_round`, `append_round_question`, `record_round_answer`,
  `close_round`, `pop_question_queue_head`, `mark_target_given_up`,
  `mark_forced_topic_spent`, `apply_field_completeness` for an
  `UNABLE_TO_ANSWER` decline patch, plus `append_transcript_line` for
  meta-question asides) — see
  [backend/stt-tts-pipeline.md](stt-tts-pipeline.md) and
  [backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md).
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
- **`answered_at`/`asked_at`/`latency_seconds`** — `latency_seconds` on an
  exchange is the direct turn-latency number (`time.monotonic()`-measured
  "candidate finished answering → next question asked" gap), computed by
  `InterviewDirector` and passed straight into `start_round`/
  `append_round_question`. `None` on the opening question / idle-recovery
  exchanges, which have no preceding graded answer to measure from. This is
  the number to read for "how long did this turn take" — `asked_at`/
  `answered_at` remain a valid cross-check (`answered_at` is stamped at the
  moment the silence debounce elapsed, not when the deferred write actually
  runs, so it doesn't fold the grading call's own latency into the gap).
- `questions.queue`/`given_up_targets`/`forced_topics_spent` are all plain
  lists on the row, visible in the debug JSON export with zero extra export
  code — they ride the same `"questions": row.get("questions", {})` line
  every other `questions` sub-key already used.

## Last synced
2026-09-05 (later still — `questions` gained two more keys,
`last_asked_question` (stamped by `start_round`/`append_round_question` on
every write) and `more_items_checked` (written by the new
`mark_more_items_checked`), both feeding `combined_prompts.SYSTEM_PROMPT`'s
JOB 3 question-wording rules (acknowledgment rotation and the one-time
"any other X?" side-question) rather than any extraction/completeness
logic. See [backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md).)
2026-09-05 (major rewrite — `questions` gained three new keys (`queue`,
`given_up_targets`, `forced_topics_spent`) and four new CRUD methods
(`apply_question_queue`, `pop_question_queue_head`, `mark_target_given_up`,
`mark_forced_topic_spent`), replacing in-memory bookkeeping that used to
live on `InterviewDirector` — needed because candidate-queue computation
moved into the analysis worker's own asyncio task, which can't see the
director's instance state. `close_round`'s `grade` parameter is now
`Optional[str]` (was `str`) to match the opening round, which is never
graded and passes `grade=None`. See
[backend/stt-tts-pipeline.md](stt-tts-pipeline.md) and
[backend/resume-analysis-pipeline.md](resume-analysis-pipeline.md) for the
full round-based-queue redesign this supports. Older history predating this
rewrite (the `threads`/`settled_paths`/`pending_claims` ledger, the
`target["field"]` → `target["fields"]` pluralization, the `latency_seconds`
addition) has been compressed out of this file; see the paired backend docs
above for anything still load-bearing.)
