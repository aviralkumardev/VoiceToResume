# QA Flow — How the Interview's Question/Answer System Works

This explains the **completeness grading + structured interview (Q&A)**
system from first principles: why it exists, every JSON field it produces,
every component involved, and the exact LLM calls it makes. Read top to
bottom once and you'll have the whole thing in your head.

---

## 1. The problem, from scratch

We're building a resume out of a spoken conversation. Two different LLM
pipelines already run in the background:

1. **Extraction** (a separate pipeline, not covered here in depth — see
   `docs`/`.claude/backend/resume-analysis-pipeline.md`) turns raw
   transcript text into structured `resume_data` (name, email, experience
   items, etc).
2. **QA / Completeness** (this doc) asks: *"Is what we've extracted so far
   actually good enough?"* — not "does this field have a value" but "does
   it meet the bar a real resume needs" (e.g. a work-experience entry needs
   company + role + dates + real responsibilities, not just a company
   name). Where it finds a gap, it **asks the candidate about it directly**,
   live, over TTS.

So there are really two jobs bolted together:
- **Grading**: is this block/field good enough? (`SUFFICIENT` / `PARTIAL` /
  `UNABLE_TO_ANSWER` / `MISSING`)
- **Interviewing**: if not, pick ONE thing to ask about, ask it, grade the
  reply, repeat.

## 2. The two engines (mental model)

| | Batched Completeness Worker | Interview Director |
|---|---|---|
| **Trigger** | Candidate goes silent (VAD stop-speaking event) | Same signal, but handled in-process by the bot |
| **What it does** | Re-grades the *whole* resume against the rubric in the background | Picks **one** open gap, speaks a question about it via TTS, grades the answer, commits |
| **Catches** | Things the candidate volunteers unprompted in free conversation | Things nobody would have said unless asked |
| **Writes** | `field_completeness` (whole-dict replace) | `field_completeness` (single-path patch) + `questions` ledger |
| **File** | `silence_completeness_worker.py` | `interview_director.py` |

They **don't coordinate** and don't need to — they write to the same state
but at different granularity, and there are guard rails (below) so they
never fight each other or re-ask something already settled.

Think of the batched worker as "listening passively" and the director as
"actively interviewing." Same rubric, same output shape, different
triggers.

---

## 3. Core nomenclature (glossary)

| Term | What it means | Where it lives |
|---|---|---|
| `RESUME_SCHEMA` | The shape of the resume document itself (blocks like `personal`, `experience`, `projects`...) | `config_jsons_definitions/resume_schema.py` |
| `COVERAGE_SCHEMA` | The **static rubric** — per block/field: `importance` (`required`/`recommended`/`optional`), `complete_when` (a plain-English bar), and `objective_priority` (ask-order tiebreaker, 1 = highest) | `config_jsons_definitions/coverage_schema.py` |
| `field_completeness` | The **verdict state** — per path, what grading verdict it currently holds (`SUFFICIENT`/`PARTIAL`/`MISSING`/`UNABLE_TO_ANSWER`) | Written by both engines; dumped raw to `backend/json/{session_id}_status.json` |
| `target` | The ONE thing currently being asked about: `{target_type, target_path, complete_when}` | `completeness_status.py` |
| `target_type` | `"FIELD"` (one field), `"BLOCK"` (a whole topic, e.g. "tell me about your projects"), `"CONFLICT"`, `"UNRESOLVED"` | same |
| `target_path` | Dot-path into the resume (`"personal.github"`, `"experience.<item_id>.role"`), or `"conflict:<id>"` / `"unresolved:<id>"` for special targets | same |
| `conflict` | A resume side-list record: the candidate gave two different values for the same field. `{id, block, field, item_id, existing_value, candidates: []}` | `merge.py` (`_add_conflict`), lives in `resume_data["conflicts"]` |
| `unresolved` | A resume side-list record: something said that couldn't be attached to any entry. `{id, block, text, note}` | `merge.py` (`merge_unresolved`), lives in `resume_data["unresolved"]` |
| `select_focus_target()` | Pure function that picks the next thing to ask about, in strict priority order (see §5) | `completeness_status.py` |
| `open_targets()` | The ranked list of *other* still-open targets, handed to the answer grader as a fixed "menu" it may report against | same |
| `also_covered` | Extra targets from that menu the candidate's answer *also* answered, so we don't re-ask them | grader output, validated in `interview_director.py` |
| `resolved_paths` | The set of target paths excluded from selection — union of settled paths, *exhausted* threads, and paths with a pending claim. All three are derived from `row["questions"]` on every pass; the director keeps no exclusion set of its own. | `interview_director._excluded()` |
| `sticky_path` | While the director is mid-question on a target, it keeps re-selecting the *same* one rather than jumping around, until it resolves | `interview_director.py` |
| `evidence` | A verbatim quoted span from the candidate's own answer, proving a `SUFFICIENT`/`UNABLE_TO_ANSWER` verdict is backed by something real | grader output |
| `evidence_hint` | When re-opening a voided claim, the candidate's own past words get quoted back so the re-ask doesn't sound like we weren't listening | `questions.reopened[path].evidence` |
| `block_fingerprint()` | A count of value-bearing leaves in a block — used as a cheap "did real data actually land" signal | `completeness_status.py` |
| `TERMINAL_STATUSES` | `{SUFFICIENT, UNABLE_TO_ANSWER}` — once a target reaches one of these it is never re-asked or downgraded | `completeness_status.py` |
| `UNABLE_TO_ANSWER` | The explicit-decline verdict ("I don't have a GitHub"). Terminal. Needs no data and no verification. | grader output |
| `settled_paths` | List of target paths resolved without a full question thread (e.g. an `also_covered` extra, or a confirmed claim) | `row["questions"]["settled_paths"]` |
| `pending_claims` | Unverified **BLOCK**-level settlements awaiting confirmation from the extraction pipeline (see §6) | `row["questions"]["pending_claims"]` |
| `reopened` | Paths whose claim was voided — re-opened with the candidate's quoted words attached | `row["questions"]["reopened"]` |
| `threads` | Full conversational history — one entry per target ever asked about, question/answer messages in order | `row["questions"]["threads"]` |
| `probe_count` / `max_probe_count` | Per thread: how many questions this target has cost, and its budget. `probe_count` counts the opening question too, so the default budget of 2 = 1 opening + 1 follow-up. | `row["questions"]["threads"][path]` |
| `exhausted` | A thread that is still non-terminal but has spent its whole budget — derived from those two numbers, skipped by selection. Replaced the old stored `abandoned_paths` list. | `interview_director._exhausted_paths()` |
| `reason` (on a question) | **Why** this question was asked — the selection rationale for an opening question, the grader's own verdict reason for a probe | `_selection_reason()` in `interview_director.py` |
| `claim` | An unverified assertion that a BLOCK target is now `SUFFICIENT`, filed instead of settled outright, pending proof (see §6) | `_build_block_claim()` in `interview_director.py` |

---

## 4. Where the state actually lives

Per session, two things matter:

```
row["resume_data"]        <- the actual resume (blocks, fields, conflicts, unresolved)
row["field_completeness"] <- verdict per path (grading scratch state)
row["questions"] = {
    "current_focus_path": None,
    "awaiting_answer": False,
    "threads": {},          # {target_path: {target_type, status,
                            #                probe_count, max_probe_count,
                            #                messages: [...]}}
    "settled_paths": [],
    "pending_claims": [],
    "reopened": {},
}
```

A thread's messages are
`{"role": "question", "text", "ts", "reason"}` and
`{"role": "answer", "text", "ts"}` — so the export answers "why was I asked
this?" for every single question.

`row["questions"]` is a **sibling** of `resume_data`, not nested inside it —
deliberately, so the extraction merge logic never has to know it exists.
It is also never recomputed from scratch (unlike `field_completeness`, which
the batched worker re-derives from `resume_data` every ~2s of silence) —
which is *why* the claim ledger lives here and not there: there'd be nothing
to re-derive it from.

File: `backend/app/meeting_room/data/crud.py` (`InMemoryResumeRoomCRUD`),
interface: `crud_interfaces.py`. Debug dumps:
`backend/json/{session_id}.json` (full row, includes `questions`) and
`backend/json/{session_id}_status.json` (just `field_completeness`).

---

## 5. The full walkthrough (director side)

This is the actual sequence, state by state.

**Idle.** Candidate falls silent for `resume_room_silence_hardbound_seconds`
(2s). The persona LLM has been talking; the director wakes up and calls:

```
select_focus_target(resume, COVERAGE_SCHEMA, field_completeness, sticky_path=None, resolved_paths=_excluded(row))
```

Priority order it walks through, **highest first**:
0. **Sticky** — if we were already mid-question on a path, keep it.
1. **CONFLICT** — an outstanding `resume_data["conflicts"]` record. Fixing a
   known-wrong fact outranks collecting a new one.
2. **UNRESOLVED** — an outstanding `resume_data["unresolved"]` record.
3. **Importance tiers**: all `required` gaps before any `recommended`, all
   `recommended` before any `optional`. Within a tier: started blocks
   before empty ones, then field `importance`, missing-before-partial.
4. `None` — nothing left to ask: ungate and leave interview mode.

Before that selection runs, the director does two things in a fixed order:

1. **Flushes the transcript** (`orchestrator.flush_transcript`) — a short
   remark might not have hit extraction yet, and asking about something the
   candidate just said is the exact bug this flush prevents. The flush
   forces an extraction batch and *awaits* it.
2. **Reconciles the claim ledger** (`reconcile_pending_claims`) — since the
   extractor has now provably had its say about the last answer, this is the
   one moment where a BLOCK claim can be settled or voided for real. A
   voided one drops straight back into the selection below (§6).

**Asking.** Calls `run_completeness_chain({}, COVERAGE_SCHEMA,
question_target=target, resume=resume)` — LLM call #1 (see §7) — to get the
question worded. Speaks it via `TTSSpeakFrame(append_to_context=False)`
(never added to the persona's own context — the persona must never think
*it* asked this). Writes `crud.apply_question_target(..., reason=...,
max_probe_count=...)`, entering **awaiting_answer**. The `reason` is built
by `_selection_reason()` from the rubric and the current verdict — e.g.
`"Required field 'personal.email' -- no value yet."` or `"Recommended block
'projects' has no content yet."` — so the thread records *why* selection
landed here.

**Awaiting answer.** The `UserInputGate` cuts the candidate's transcript
off from the persona LLM (captions/extraction still flow normally — only
the persona-bound copy is gated). Every line the candidate says is buffered
by `record_candidate_text()`. After `resume_room_answer_silence_seconds`
(3s) of silence, the answer is "done": everything they said, joined, is
`answer_text`.

**Grading the answer.**
1. Build the de-dup menu: `open_targets(...)` minus the primary target.
2. `crud.maybe_record_answer(session_id, answer_text)`.
3. `run_answer_evaluation_chain(target, answer_text, ..., candidates)` — LLM
   call #2 (see §7).
4. `validate_also_covered(...)` — throws out any `also_covered` entry not
   genuinely on the offered menu, without real evidence, or double-using a
   quote (bad LLM output must never write bad data or eat a needed question).
5. **Commit** (`_commit_answer`):
   - One `apply_resume_update` folding every FIELD value (primary + extras)
     together, plus any resolved conflict/unresolved.
   - `apply_answer_verdict` per FIELD target (BLOCK targets are **not**
     verdict-written here — see §6). A special target's own pseudo-path
     never gets a verdict, but a settled CONFLICT now writes one at the
     **disputed field's real path** — the field, not the dispute.
   - `apply_question_target` for each `also_covered` extra, recording it in
     `settled_paths` if terminal.
6. **Probe cap** — if still `PARTIAL` and this thread's `probe_count` has
   reached its `max_probe_count`, give up on the target: drop the probe.
   Nothing is written anywhere — the spent thread already says so, and
   `_exhausted_paths` derives it on the next selection pass. This is what
   stops an unsettleable conflict from stalling the whole interview forever.
7. `PARTIAL` → speak the probe, same target, same thread, with the grader's
   own `reason` recorded as *why* the probe was asked.
   `SUFFICIENT`/capped → re-select the next target (or leave interview
   mode if `select_focus_target` returns `None`).

---

## 6. Pending claims — why BLOCK targets are special

A **FIELD** or **CONFLICT** verdict closes its own loop: the value is
written into `resume_data` in the *same* commit as the verdict, so nothing
can go out of sync.

A **BLOCK** verdict ("tell me about your projects" → SUFFICIENT) can't do
that — turning a free-form answer into structured resume items is
extraction's job, not the director's (we don't want two extractors). So
instead of settling immediately, a `SUFFICIENT` BLOCK verdict is filed as an
unverified **claim**:

```json
{
  "claim_id": "a1b2c3d4",
  "target_path": "projects",
  "target_type": "BLOCK",
  "evidence": "I also built a small CLI tool that scrapes...",
  "reason": "Candidate described a personal project with clear scope.",
  "confidence": 0.82,
  "baseline": 0,
  "source": "primary"
}
```
- `baseline` = the block's `block_fingerprint()` (count of value-bearing
  leaves) at filing time — confirmation needs a real **delta**, not mere
  presence.
- No usable `evidence` (missing, or not actually found in the answer text)
  → no claim is filed at all; the target just stays open.

**Reconciling** (`claim_reconciler.classify_claims`) — just two rules:
1. Fingerprint grew past baseline → **confirm**. The delta *is* the
   evidence, whichever batch produced it.
2. Otherwise → **void** if `force`, else still pending.

This used to be a five-rule ladder with sequence numbers, a per-batch
watermark, grace rounds and an expiry timer, all defending against "what if
extraction never runs?". It doesn't need to any more: the director *makes*
extraction run (the flush) and *awaits* it before checking, so both callers
pass `force=True` — by the time either one looks, the claim has already had
its chance.

**On confirm**: `resolve_pending_claims` writes the real settlement
(verdict + thread status / `settled_paths`).
**On void**: the path goes into `reopened`:
```json
"reopened": {
  "projects": {
    "evidence": "I also built a small CLI tool that scrapes...",
    "count": 1,
    "voided_at": "2026-09-04T10:16:35+00:00"
  }
}
```
That quoted `evidence` becomes `evidence_hint` the next time this target is
asked — the re-ask opens with *"You mentioned building a CLI tool that
scrapes... walk me through that?"* instead of a cold generic question, and
the block drops straight back into the *same* selection pass. Repeated
voids need no separate cap: every re-ask burns one of that thread's
`max_probe_count` questions, so the probe budget bounds them.

This runs from exactly two places: the director, between the flush and the
selection (the one that matters live), and the final pass at session end
(the sweep). There is no third "drain" state and no polling — the ordering
made both unnecessary.

**A pending claim is a suppression, not a settlement.** It keeps the block
out of selection while we check, and evaporates the instant the claim is
voided.

---

## 7. Components

| Component | What it is | Why it exists | File |
|---|---|---|---|
| **Interview Director** | A two-state machine (`idle` ⇄ `awaiting_answer`) living inside the live bot process | Runs the *active* Q&A independent of the persona LLM — so the persona never has to know structured interviewing is happening | `stt_tts_pipeline/interview_director.py` |
| **UserInputGate** | A pipecat `FrameProcessor` sitting between transcription and the persona's LLM input | Cuts the candidate's answer off from the persona *only* — STT, captions, and the director's own text capture all keep working upstream | `stt_tts_pipeline/processors/bridges.py` |
| **Silence Completeness Worker** | An `asyncio.Task` consuming a speaking-state queue | The passive engine — re-grades the whole resume in the background whenever the candidate pauses, catching unprompted info | `resume_analysis_pipeline/silence_completeness_worker.py` |
| **Claim Reconciler** | Pure classification (`classify_claims`) + the async apply step (`reconcile_pending_claims`) | Verifies BLOCK-level claims against what extraction actually produced, so nothing is settled on faith | `resume_analysis_pipeline/claim_reconciler.py` |
| **Completeness Status core** | Pure, no-I/O functions: `select_focus_target`, `open_targets`, `prune_for_judgment`, `merge_completeness`, `block_fingerprint`, etc. | The single source of truth for "what's open, what's next, what counts as done" — no LLM, no I/O, fully testable | `resume_analysis_pipeline/completeness_status.py` |
| **CRUD / Ledger** | `InMemoryResumeRoomCRUD` — the only place any of this state is actually written | Owns every mutation, writes the debug JSON snapshot on every change | `data/crud.py`, interface in `data/crud_interfaces.py` |
| **COVERAGE_SCHEMA** | Static rubric data — not code, not runtime state | The bar every grading call and every question is measured against | `config_jsons_definitions/coverage_schema.py` |

---

## 8. The two LLM calls, with real input/output shapes

Both calls use the same provider machinery
(`app/services/llm_providers/`, OpenRouter, `generate_json` with a JSON
schema) but are two independent chains with separate prompts, configured by
`resume_room_completeness_*` settings.

### Call #1 — `run_completeness_chain` (batched grading + question wording)

File: `resume_analysis_pipeline/completeness_chain.py` +
`completeness_prompts.py`. Used by **both** engines — the batched worker
passes only `to_judge` (ignores `question`); the director passes only
`question_target` (ignores `blocks`).

**Input** (user message; the system prompt is static instructions):
```json
{
  "rubric": {
    "personal": {
      "importance": "required",
      "objective_priority": 1,
      "complete_when": "The candidate can be identified and reached through at least one reliable channel, and their full name is known.",
      "fields": {
        "github": {
          "importance": "optional",
          "complete_when": "A GitHub profile URL has been captured."
        }
      }
    }
  },
  "blocks": {},
  "question_target": {
    "target_type": "FIELD",
    "target_path": "personal.github",
    "complete_when": "A GitHub profile URL has been captured.",
    "context": { "name": "Priya Sharma", "email": "priya@example.com" }
  }
}
```
(A batched-grading call instead sends a populated `blocks` dict — e.g.
`{"personal": {"fields_to_judge": {"linkedin": {"value": "linkedin.com/in/p"}}, "missing_fields": ["github"]}}`
— and omits `question_target` entirely.)

**Output**:
```json
{
  "reasoning": "No GitHub info given yet; asking directly.",
  "blocks": {},
  "question": "Do you have a GitHub profile you'd like me to note down?",
  "_llm_usage": {
    "model": "openai/gpt-5.1",
    "prompt_tokens": 412,
    "completion_tokens": 28,
    "total_tokens": 440,
    "cost": 0.0009
  }
}
```
For a batched grading call, `blocks` instead comes back populated per the
`fields_to_judge`/`items_to_judge` keys it was given, e.g.:
```json
{
  "reasoning": "Personal block now has a name and two contact channels.",
  "blocks": {
    "personal": {
      "completeness_status": "SUFFICIENT",
      "reason": "Name, email and LinkedIn are all captured.",
      "confidence": 0.9,
      "fields": {
        "linkedin": { "completeness_status": "SUFFICIENT", "reason": "Valid profile URL captured.", "confidence": 0.95 }
      }
    }
  },
  "question": null,
  "_llm_usage": { "...": "..." }
}
```

### Call #2 — `run_answer_evaluation_chain` (grading one spoken answer)

File: `resume_analysis_pipeline/answer_evaluation_chain.py` +
`answer_evaluation_prompts.py`. Only the director calls this — once per
completed answer, inline in the live loop.

**Input**:
```json
{
  "target": {
    "target_type": "FIELD",
    "target_path": "personal.github",
    "complete_when": "A GitHub profile URL has been captured.",
    "value_kind": "scalar"
  },
  "answer": "yeah it's github dot com slash priya dash s, and also I forgot to mention I'm based in Pune",
  "context": { "name": "Priya Sharma", "email": "priya@example.com" },
  "conversation": [
    { "role": "question", "text": "Do you have a GitHub profile you'd like me to note down?" }
  ],
  "open_targets": [
    { "target_type": "FIELD", "target_path": "personal.location", "complete_when": "The candidate's city/region is known.", "value_kind": "scalar" }
  ],
  "block_rubric": { "importance": "required", "complete_when": "..." },
  "primary_block": "personal"
}
```

**Output** (note the `also_covered` de-dup — the candidate also mentioned
their location unprompted, so it gets folded in instead of re-asked):
```json
{
  "completeness_status": "SUFFICIENT",
  "reason": "Candidate gave a usable GitHub URL.",
  "confidence": 0.92,
  "question": null,
  "evidence": null,
  "extracted_value": "github.com/priya-s",
  "also_covered": [
    {
      "target_path": "personal.location",
      "status": "SUFFICIENT",
      "evidence": "I'm based in Pune",
      "value": "Pune",
      "reason": "Candidate stated their city directly."
    }
  ],
  "_llm_usage": {
    "model": "openai/gpt-5.1",
    "prompt_tokens": 501,
    "completion_tokens": 63,
    "total_tokens": 564,
    "cost": 0.0012
  }
}
```

For a **BLOCK** target with a `SUFFICIENT` verdict, `evidence` is required
and non-null (it becomes the claim's evidence, §6). For `UNABLE_TO_ANSWER`
(e.g. "I don't have a GitHub"), `extracted_value` is `null` and no claim or
value is written — the target is simply terminal.

Both chains are **fail-soft**: any provider/schema error degrades to a safe
"nothing changed" shape (`blocks: {}, question: null` / `PARTIAL` with no
question or value) rather than raising — a bad LLM call can never crash a
live interview, it just leaves the target open to be asked again.

---

## 9. File map — quick reference

| Concept / keyword | File |
|---|---|
| `COVERAGE_SCHEMA`, rubric definitions | `config_jsons_definitions/coverage_schema.py` |
| `RESUME_SCHEMA` (resume shape) | `config_jsons_definitions/resume_schema.py` |
| `select_focus_target`, `open_targets`, `prune_for_judgment`, `merge_completeness`, `block_fingerprint`, `TERMINAL_STATUSES`, special-target helpers | `resume_analysis_pipeline/completeness_status.py` |
| Batched grading + question-wording LLM call, its prompt | `resume_analysis_pipeline/completeness_chain.py`, `completeness_prompts.py` |
| Per-answer grading LLM call, its prompt, `also_covered` validation input | `resume_analysis_pipeline/answer_evaluation_chain.py`, `answer_evaluation_prompts.py` |
| Evidence-span matching (`evidence_matches`, `evidence_tokens`) | `resume_analysis_pipeline/answer_evidence.py` |
| Pending-claim classification (`classify_claims`) + apply (`reconcile_pending_claims`) | `resume_analysis_pipeline/claim_reconciler.py` |
| Batched debounce/cancel/commit worker | `resume_analysis_pipeline/silence_completeness_worker.py` |
| Conflict/unresolved record creation, resume merge logic | `resume_analysis_pipeline/merge.py` |
| Interview Director state machine, `_selection_reason`, `_exhausted_paths`, `_build_block_claim`, `combine_field_updates`, `validate_also_covered`, `_excluded` | `stt_tts_pipeline/interview_director.py` |
| `UserInputGate`, transcript/speaking bridges | `stt_tts_pipeline/processors/bridges.py` |
| Bot assembly, wiring director into the pipeline | `stt_tts_pipeline/pipeline.py` |
| `questions` ledger schema (`settled_paths`, `pending_claims`, `reopened`, `threads` with `probe_count`/`max_probe_count`/question `reason`), all CRUD writers | `data/crud.py`, `data/crud_interfaces.py` |
| Debug JSON exports | `backend/json/{session_id}.json`, `backend/json/{session_id}_status.json` |
| LLM provider abstraction (`generate_json`, schema validation, OpenRouter) | `services/llm_providers/*` |
| Config knobs (`resume_room_silence_hardbound_seconds`, `resume_room_answer_silence_seconds`, `resume_room_max_probes_per_target`, `resume_room_flush_timeout_seconds`, `resume_room_completeness_*`) | `core/config.py` |

---

## 10. One-paragraph recap

Every ~2 seconds of silence, a background worker re-grades the whole resume
against a static rubric. Separately, the Interview Director picks the ONE
highest-priority open gap (conflicts first, then unresolved mentions, then
required → recommended → optional fields), asks about it directly over
TTS while gating the persona LLM out of the loop, grades the reply with a
second focused LLM call, and — if the candidate's answer also happened to
cover other open targets — settles those too without asking. A `FIELD`
answer writes its value immediately; a `BLOCK` answer can't (extraction
owns that), so it's filed as an unverified claim and only counted once real
structured data actually lands, or else it re-opens quoting the
candidate's own words back to them. Every decision — what to ask, what's
settled, what's abandoned — lives in one small `questions` JSON ledger
sitting next to the resume, so nothing is ever re-asked twice and nothing
is ever settled on faith.
