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

## 4. Open questions / assumptions before I plan implementation

1. **Does the independent 2s-silence whole-resume grading sweep
   (`silence_completeness_worker`'s own debounce, separate from the answer-
   end flush) survive in the new design, or is it fully replaced by the
   char-triggered combined call + the answer-end flush?** Point (2)/(3) only
   describe a char-based trigger and an answer-end flush — I don't see a
   third, independent silence-debounce trigger mentioned, so my working
   assumption is **it's gone, folded into the char-triggered call**. Please
   confirm.
2. **Does the combined call in (2) still receive a Python-precomputed
   priority-ordered candidate list (à la `compute_next_targets`), or does it
   decide priority order itself from the raw resume + rubric each time?**
   "What stays the same: priority ordering" reads to me as *the rule* stays
   the same, but I want to confirm whether Python keeps computing/feeding the
   candidate list (cheaper, more deterministic, matches today's trust-
   boundary pattern) versus the LLM re-deriving order from scratch each call.
3. **What exactly is "the queue"?** Is it a list of already-**worded**
   questions ready to speak (so popping it never needs another LLM call), or
   a list of **targets** (block/item/fields) that still need a wording step
   before being asked? Point (5) ("pick the first question from the queue")
   reads as pre-worded to me, but I want that confirmed since it affects
   whether popping the queue can ever be a zero-latency operation or not.
4. **Does the buffer-process call (2)/(a) get access to the in-progress
   answer's transcript before the candidate finishes speaking**, i.e. is the
   100-char trigger evaluated against the *whole* running transcript
   (mid-answer included, same as today's extraction trigger, which fires
   "however long the candidate keeps talking, not just once they go
   silent"), or only against completed answers? I'm assuming the former
   (matches today's behavior) — flag if not.
5. **Forced conflict/unresolved topics** — today these jump the queue ahead
   of whatever the per-answer call drafted, decided by
   `InterviewDirector._pick_forced_topic` reading `resume["conflicts"]`/
   `resume["unresolved"]` directly, worded by a small dedicated chain
   (`run_topic_question_chain`), independent of the per-answer grading call.
   I'm assuming this guardrail is unchanged and just now jumps the new
   *queue* instead of overriding a per-turn `next_question` — confirm this
   still holds, since it isn't mentioned in your 6 points.
6. **`UNABLE_TO_ANSWER`** — today an explicit decline is a third possible
   grade (not just PARTIAL/SUFFICIENT), and closes the round while
   patching `field_completeness` directly for the declined field(s)
   (`build_unable_to_answer_patch`) since the batched grader can structurally
   never infer a decline on its own. Your 6 points only mention
   PARTIAL/SUFFICIENT outcomes for step (b) — I'm assuming
   `UNABLE_TO_ANSWER` still exists as a third grade and is treated like
   SUFFICIENT for queue-advancement purposes (move on), just with that same
   `field_completeness` patch on the side. Confirm.
7. **Round/probe budget** — today a "round" caps at
   `resume_room_max_questions_per_round` (default 2) exchanges before being
   force-closed even if still non-terminal (`_organic_targets_given_up`
   guards against re-queueing a subject that capped out non-terminal). Does
   this cap still apply per queue item in the new design?

I'll hold off on any code changes or an implementation plan until these are
resolved.
