# QA Flow Redesign — Understanding Doc

This is **not a plan** — it's a checkpoint. Before designing the new
interview Q&A flow I want to write down exactly what I think you're asking
for, mapped onto the concrete files/mechanisms that exist today, so we can
correct any misreading before any code gets touched.

## 1. Current system, in one paragraph

Today there are **three** separate background LLM call sites, on **two**
different triggers:

- **Extraction** (`analysis_orchestrator.run_resume_analysis_worker`) —
  batches candidate transcript text until `resume_room_extraction_trigger_chars`
  (currently much larger than 100), then calls `run_resume_extraction_chain`
  (extraction only — pulls structured `resume_data` updates out of raw text).
- **Coverage grading** — triggered two ways: (a) after any extraction batch
  that changed something, `run_completeness_grading_cycle` fires
  (unawaited) as a second, separate LLM call
  (`run_completeness_chain`, whole-resume-vs-rubric); (b) independently,
  every time the candidate goes silent for 2s, `silence_completeness_worker`
  re-runs the same whole-resume grading call on its own debounce, regardless
  of whether the interview director is mid-question.
- **Per-answer grade + question drafting** (`InterviewDirector._finish_answer`
  → `question_chain.run_question_chain`) — one fused call per answer, but it
  is handed the **whole** resume, the **whole** askable coverage schema, the
  **entire** conversation history, `field_completeness`, and a
  Python-computed exhaustive list of remaining targets
  (`next_target.compute_next_targets`, priority-ordered: touched blocks
  before untouched, each by `objective_priority`). It both grades the just-
  given answer (`PARTIAL`/`SUFFICIENT`/`UNABLE_TO_ANSWER`) **and**, in the
  same response, drafts a same-topic probe (if still open) and a next
  question worded for the first unresolved candidate in that list.

So today: extraction and coverage grading are already two separate calls
from *each other*, and the per-answer call is a third, much heavier call
that re-derives "what to ask next" from scratch every single turn using the
whole resume + whole history as context, even though a Python function
already narrowed it down to a small ordered candidate list first.

## 2. What I understand you want instead

**(1) Opening.** Unchanged — fixed `GREETING_MESSAGE` spoken, candidate
starts answering. (Matches `BotSession.greet()` → `ask_opening_question`.)

**(2) One combined background call, smaller trigger.** Collapse extraction
+ coverage-grading + next-question-drafting into a **single** LLM call,
triggered by the transcript buffer hitting ~100 characters (much smaller
than today's extraction trigger) rather than today's two separate calls on
two separate triggers. Your reasoning — only 6-7 blocks exist, so one call
doing all three jobs at once isn't an overloaded prompt. I read this call as
the new sole owner of: merging structured updates into `resume_data`,
updating `field_completeness`, **and** maintaining an ordered queue of
upcoming questions (deciding whether the top open priority target is now
resolved, and if not, wording a question for it). I'm treating "coverage
check" here as this call also being the one that updates `field_completeness`
(replacing both the post-extraction-batch grading trigger and the
independent silence-debounce sweep — see open question below on whether the
independent 2s-silence sweep survives at all).

**(3) Two background tasks fire when the candidate's answer ends (silence
detected):**
- **(a) Buffer-process task** — *only if* the buffer hasn't already hit the
  100-char trigger and been processed since the last time — runs the
  combined call from (2) on whatever's left in the buffer, so if the
  candidate's answer happens to satisfy an upcoming queued question, that
  question gets updated/removed before it's asked. This is the same shape as
  today's `FlushRequest`/`flush_transcript` (force a batch on unprocessed
  text regardless of the char trigger), just now flushing into the *one*
  combined call instead of only the extraction chain.
- **(b) Grading task** — a separate, narrower LLM call than today's: grades
  the just-given answer, passed **only** the entire question thread
  (conversation history) plus the *current* target's own `complete_when` bar
  from the coverage schema — not the whole resume, not the whole coverage
  schema, not the Python candidate list. This is a deliberate narrowing from
  today's `run_question_chain`, which currently receives all of that.

**(4) PARTIAL → probe in the same call.** If (b) grades the answer
`PARTIAL`, the probe question is drafted in that same grading call (fused,
same as today's behavior — no new LLM round-trip for the probe).

**(5) SUFFICIENT → await (a), then pop the queue.** If (b) grades the
answer `SUFFICIENT`, the director awaits the still-in-flight buffer-process
task (a) so the queue reflects this answer's contribution, then takes the
**first** question off the queue that (2)/(a) maintains, and asks it
directly — no separate "word this question" LLM call at pick time. This is
a structural change from today: today, `next_question` wording happens
*inside* the same per-answer call that just did the grading (task b, in
your numbering); in the new design, next-question wording is entirely
(2)/(a)'s job, and (b) only ever grades + probes.

**(6) Empty queue → end.** If the queue is empty when (5) goes to pop it,
the interview ends — same terminal condition as today's `_complete_interview()`
on a null `next_question`, just phrased as "queue empty" instead of "the
per-answer call returned null."

**(7) No grading on the opening answer.** The opening question deliberately
spans multiple blocks at once (education + experience + projects + skills),
so there's no single target/`complete_when` bar for task (b)'s narrow
grading call to grade against — grading it would mean inventing a fake
composite target just for this one turn. So the opening answer skips the
grading/probing call entirely: it goes straight to (a) (buffer-process
through the combined call) and then pops the first item off the resulting
queue, same as any other SUFFICIENT/UNABLE_TO_ANSWER outcome. Every
question the queue produces after that point *does* have one real target,
so grading + probing applies normally starting from the second question
onward. Consequence worth flagging: a thin opening answer (e.g. barely
touches education) isn't specifically probed right after the opener — the
gap just sits in the queue at its normal priority and surfaces later as an
ordinary queued question with its own `complete_when` target, rather than
being special-cased into an immediate follow-up.

**What stays the same: priority ordering.** I read this as: the *order* in
which blocks/items get queued (touched blocks before untouched, each by
`objective_priority`, conflicts/unresolved records still jumping the queue)
is preserved from today's `next_target.compute_next_targets` +
`_pick_forced_topic` logic — only the mechanics of *when* an LLM call
figures out "is this candidate resolved yet" and *when* it gets worded move
around, not the priority rule itself.

## 3. Net shape of the new design, as I currently picture it

```
Candidate speaks
  │
  ├─ every ~100 chars of new buffered text ──► combined call:
  │                                            extraction + coverage regrade
  │                                            + queue maintenance/wording
  │                                            (writes resume_data,
  │                                             field_completeness, queue)
  │
  └─ candidate goes silent (answer ends)
        │
        ├─ (a) flush remaining buffer through the SAME combined call,
        │      only if buffer hasn't just been processed
        │
        └─ current round has a real target (i.e. not the opening round)?
              │
              ├─ NO (opening round) ──► skip grading entirely, await (a),
              │                          pop queue.head
              │
              └─ YES ──► (b) grade-this-answer call: conversation history +
                          current target's complete_when only
                          → PARTIAL: word a probe in this same call, ask it
                          → SUFFICIENT/UNABLE_TO_ANSWER: await (a), pop queue.head

        pop queue.head:
              → queue empty: end interview
              → else: ask queue.head's question
```

The two big structural changes vs. today:
1. **Extraction + coverage-grading + next-question-wording become one call**,
   on a single small char-based trigger, instead of three call sites across
   two triggers.
2. **Per-answer grading gets radically narrower** (thread + this target's
   `complete_when` only) and **no longer drafts the next question** — it
   only ever grades + (if PARTIAL) probes. All "what's next and how do I
   word it" work moves to the buffer-processing call and its queue.

## 4. Decisions locked in so far

- **Priority ordering — belt and suspenders, both ends agree.** Python keeps
  computing the deterministic priority-ordered candidate list (adapted from
  today's `compute_next_targets`: touched blocks before untouched, each by
  `objective_priority`) and hands it to the combined call in that order. The
  combined call is *also* explicitly instructed (prompt-level) to preserve
  that given order when it decides which candidates are resolved and words
  the rest into the queue — it's not free to reorder. Python then validates
  the returned queue's order/containment against the list it handed in
  (same trust-boundary shape as today's `question_chain._validate_next_target`
  — cheap containment/order check against a small Python-built list) and
  corrects it if the LLM's output ever drifts. Two independent things have
  to agree before an order ships, which is exactly the safety margin that
  was missing when the earlier free-choice design bounced between blocks
  live.
- **Queue regenerated wholesale every cycle**, not patched incrementally.
  Cheap with only 6-7 blocks, and avoids an entire class of staleness/
  reconciliation bugs (mirrors the existing "replace, don't append" rule for
  the final-resolution pass's array fields — see
  `resume-analysis-pipeline.md`'s gotcha on why patching broke there).
- **The queue is visible in the debug JSON export.** Same place/pattern as
  today's `questions` ledger and `field_completeness` dump — the combined
  call's output (the regenerated queue, in priority order, each entry's
  target + worded question) gets written into `backend/json/{session_id}.json`
  (or a sibling file, mirroring `_status.json`) on every update, purely for
  observability into what the pipeline is planning to ask next.
- **The independent 2s-silence whole-resume grading sweep is gone
  entirely.** Confirmed — there is no third, standalone silence-debounce
  trigger any more. The only two triggers left are: the ~100-char buffer
  threshold (fires mid-answer, while the candidate is still talking) and
  "answer complete" (the same silence-debounce mechanism as today, but its
  only job now is detecting that the candidate has stopped talking so the
  answer can be graded/probed/advanced — it no longer independently drives a
  whole-resume re-grade of its own). `silence_completeness_worker.py` as a
  standalone passive sweep is superseded; whatever of it survives is folded
  into the buffer-process call triggered by (2)/(a).
- **The 100-char buffer trigger fires mid-answer, same as today's
  extraction trigger.** Confirmed — the buffer is just whatever the
  candidate has said so far, so it naturally fills and fires while they're
  still mid-answer, not only once they go silent. No special-casing needed
  to make this happen; it falls out of "the buffer is fed by the transcript
  as it arrives," same as today's `resume_room_extraction_trigger_chars`
  behavior.
- **`UNABLE_TO_ANSWER` still exists as a third grade and is treated like
  SUFFICIENT for queue-advancement.** Confirmed — an explicit decline (not
  just PARTIAL/SUFFICIENT) still ends the round and moves on to await (a) +
  pop the queue, same branch as SUFFICIENT, just with the extra
  `field_completeness` patch on the side for the declined field(s) (today's
  `build_unable_to_answer_patch`) since the combined call can structurally
  never infer a verbal decline from `resume_data` alone.
- **The round/probe budget concept carries over unchanged.** A "round" is
  still one target (one queue item), and it's still allowed to stay open
  across multiple PARTIAL grades — probing deeper into the *same* target
  even when successive probes address different fields within that same
  target block (exactly like today's "consolidate, don't drip-feed" +
  multi-field `target.fields` shape). The budget
  (`resume_room_max_questions_per_round`, today default 2: opening question
  + 1 follow-up) still caps the total number of question exchanges spent on
  one target before it's force-closed even if still non-terminal — this is
  what bounds "how deep can we probe" and "how many times can we re-ask
  about the same block." A target that caps out non-terminal still needs
  the equivalent of today's `_organic_targets_given_up` guard, so the queue
  regeneration doesn't just hand the same stuck target right back as
  top-priority next cycle.
- **Conflicts/unresolved records still outrank every ordinary gap, but
  there's no separate dedicated wording call for them any more.** Priority
  is unchanged — Python still checks `resume["conflicts"]` then
  `resume["unresolved"]` first and places any outstanding, not-yet-forced
  record at the very front of the candidate list, ahead of every
  objective-priority-ordered gap. What's different from today: there's no
  more standalone `run_topic_question_chain` call. The **same** combined
  call that words ordinary queue questions also words these — it's handed
  the conflict/unresolved records at the front of its candidate list (same
  as any other candidate) and produces the worded question for them in the
  same response as everything else. One mechanism (the combined call +
  regenerated queue) now covers both cases instead of a queue plus a
  separate guardrail chain on top. The "mark as spent once forced" bookkeeping
  (so the same conflict/unresolved id doesn't force the identical question
  again next cycle before extraction has caught up to clearing the record)
  still applies, same as today's `_forced_topics_spent`.

## 5. Status: no more open questions

Every open item from the earlier rounds is now resolved (see "Decisions
locked in" above). Next step, when you're ready, is turning this into an
actual implementation plan.
