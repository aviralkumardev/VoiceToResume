# Resume-Analysis Pipeline Dry Run

A hand-simulated walkthrough of `run_resume_analysis_worker` /
`run_combined_chain` against one specific candidate answer, batch by batch.
**Nothing here calls a real LLM** — JOB 1/2/3 outputs below are plausible,
hand-authored stand-ins for what `combined_chain.run_combined_chain` would
return, written to match the exact rules in `combined_prompts.SYSTEM_PROMPT`.
Everything *around* those outputs (trigger math, candidate-queue ordering,
merge/field-write rules, completeness bookkeeping) is traced exactly against
the current source, not approximated.

## Transcript being replayed

> Hi I am Aviral Kumar and I have completed my 12th from CBSE Board the
> Orbis School in 2022, I completed my graduation in BTech Computer Science
> from MIT Arts Design and Technology University based in Pune in 2026. I
> have one experience as an AI Intern at AISolve where my primary
> responsibilities were to build RAG pipelines, MCP tools and validate
> POC's. I do not have any personal projects and my main skills are Java,
> Python, Spring Boot, Langchain, LangGraph, MCP, RAG and Databases.

Split (by the STT/transcript layer) into:

| | Text |
|---|---|
| Batch-1 | `Hi I am Aviral Kumar and I have completed my 12th from CBSE Board the Orbis School in 2022, I comple` |
| Batch-2 | `ted my graduation in BTech Computer Science from MIT Arts Design and Technology University based in ` |
| Batch-3 | `Pune in 2026. I have one experience as an AI Intern at AISolve where my primary responsibilities wer` |
| Batch-4 | `e to build RAG pipelines, MCP tools and validate POC's. I do not have any personal projects and my m` |
| *(pending, not yet flushed)* | `ain skills are Java, Python, Spring Boot, Langchain, LangGraph, MCP, RAG and Databases.` |

## Mechanics that drive every batch below

- **One batch = one combined-chain call, immediately.** `resume_room_extraction_trigger_chars = 100` (`backend/app/core/config.py:32`). The worker accumulates `chunk + "\n"` and fires `run_combined_chain` once `len(accumulated_text) >= 100` (`analysis_orchestrator.py:184`). Each of these four batches is itself ~100+ chars, so each one triggers its own call the moment it's queued — there's no waiting to accumulate multiple batches first.
- **Call input = carried fragment + this batch.** `input_text = remaining_text + accumulated_text` (`analysis_orchestrator.py:76`). `remaining_text` is whatever the *previous* call flagged as a trailing mid-sentence fragment (JOB 1's `remaining_text` output), capped at `trigger_chars × 4 = 400` chars (`_cap_carry`).
- **The candidate queue is computed *before* the call, from the state as of the end of the previous batch** (`analysis_orchestrator.py:92-96`), then handed to the LLM as `<candidate_queue>` to word questions for. Priority order (`next_target.compute_candidate_queue`): outstanding conflicts → outstanding unresolved → ordinary coverage gaps, and within gaps: blocks that already have *some* resume data ("touched") before blocks that don't ("untouched"), each group sorted by `objective_priority` ascending.
- **`personal` and `summary` are `not_applicable`** in `COVERAGE_SCHEMA` (`coverage_schema.py:7,46`) — they're extracted in JOB 1 like anything else, but never graded (JOB 2) or asked about via a spoken question (JOB 3/`ASKABLE_COVERAGE_SCHEMA`).
- **A list-object block (education/experience/…) only ever surfaces its *first* item with an open field** — `_item_level_target` returns as soon as it finds one (`next_target.py:44-56`). A second item (e.g. a second education entry) stays invisible to the spoken queue until the first one's fields are all resolved or given up on.
- **Item-level completeness lags one batch behind extraction.** `field_completeness` for a brand-new list-object item only gets populated once `prune_for_judgment` sees that item already present in `resume_data` — which happens on the *next* batch, not the one that created it. Until then, `next_target`'s candidate computation (which reads `field_completeness`, not `resume_data`) treats every one of that item's fields as open, even ones already captured. This resolves itself one batch later.
- **An explicit whole-block decline** ("I do not have any personal projects" — literally the example in `combined_prompts.SYSTEM_PROMPT`'s JOB 2) is graded `UNABLE_TO_ANSWER`, which is terminal — the block drops out of the candidate queue for good (`_block_is_open`, `next_target.py:81-83`).

---

## Batch-1

**Trigger:** `len("Hi I am ... I comple\n")` ≥ 100 → fires immediately.
**Call input** (`remaining_text` = `""` + this batch): the batch text verbatim.
**Candidate queue going in:** resume is empty, nothing touched yet → all 10 askable blocks as whole-block gaps, `objective_priority` order: `experience(3), education(4), skills(5), projects(6), certifications(7), courses(8), achievements(9), awards(10), languages(11), additional_information(12)`.

**JOB 1 — extraction**
- `personal.name = "Aviral Kumar"`
- new `education` item **edu-1**: `degree: "12th Grade (CBSE Board)"`, `college: "The Orbis School"`, `end_date: "2022"`
- `status: "extracted"`, `remaining_text: "I comple"` (cut mid-word, "completed" continues next batch)

**JOB 2 — completeness:** everything still empty except the just-created edu-1 → `education` graded `PARTIAL` at the block level (edu-1 not yet field-graded — this is the one-batch lag described above); every other askable block `PARTIAL` (empty/untouched).

**JOB 3 — queue:** nothing resolved yet, so all 10 candidates get worded.

### State after Batch-1

**Extracted profile**
```json
{
  "personal": { "name": { "value": "Aviral Kumar", "source": "MEETING" } },
  "education": [
    { "id": "edu-1", "degree": {"value": "12th Grade (CBSE Board)"}, "college": {"value": "The Orbis School"}, "end_date": {"value": "2022"} }
  ],
  "experience": [], "projects": [], "certifications": [], "courses": [],
  "skills": [], "achievements": [], "awards": [], "languages": [], "additional_information": []
}
```

**Coverage status**

| Block | Status | Note |
|---|---|---|
| personal / summary | NOT_APPLICABLE | never graded |
| experience | PARTIAL | no data |
| education | PARTIAL | edu-1 captured, not yet field-graded (lag) |
| skills / projects / certifications / courses / achievements / awards / languages / additional_information | PARTIAL | empty |

**Question queue** (9→10, in priority order)
1. `gap:experience` — "Could you walk me through your work experience — where you worked, your role, and what you did there?"
2. `gap:education` — "Could you tell me more about your education — your field of study, and any dates or grades?"
3. `gap:skills` — "What are your main technical skills?"
4. `gap:projects` — "Have you worked on any personal or academic projects you'd like to share?"
5. `gap:certifications` — "Do you have any professional certifications?"
6. `gap:courses` — "Have you completed any standalone courses outside your degree?"
7. `gap:achievements` — "Do you have any notable achievements you'd like to mention?"
8. `gap:awards` — "Have you received any awards?"
9. `gap:languages` — "What languages do you speak or write?"
10. `gap:additional_information` — "Is there anything else you'd like to add to your resume?"

---

## Batch-2

**Call input:** `remaining_text ("I comple") + "ted my graduation in BTech Computer Science from MIT Arts Design and Technology University based in "` → reconstructs *"I completed my graduation in BTech Computer Science from MIT Arts Design and Technology University based in "*, again cut mid-sentence.
**Candidate queue going in** (state after Batch-1): `education` is now "touched" → order becomes `education(4), experience(3, still untouched but lower priority number is irrelevant once education is touched-first), skills, projects, certifications, courses, achievements, awards, languages, additional_information`. Education's candidate is still whole-block (edu-1 not yet field-graded at this point), so wording targets edu-1 generically.

**JOB 1 — extraction**
- new `education` item **edu-2**: `degree: "BTech"`, `field: "Computer Science"`, `college: "MIT Arts Design and Technology University"` (location/end_date not yet said — cut off at "based in ")
- `status: "extracted"`, `remaining_text: "based in "`

**JOB 2 — completeness:** edu-1 *is* visible to `prune_for_judgment` this round (it existed before this batch) → its `degree`/`college`/`end_date` get graded `SUFFICIENT`; `field`/`start_date`/`location`/`grade` remain `MISSING`. edu-2 (brand new this batch) isn't graded yet — same one-batch lag. Block-level `education` stays `PARTIAL` (edu-1 still has 4 open fields).

**JOB 3 — queue:** education's question now narrows toward edu-1's still-open pieces (field of study / start / location / grade); nothing else has changed, so all 10 candidates remain.

### State after Batch-2

**Extracted profile**
```json
{
  "personal": { "name": { "value": "Aviral Kumar", "source": "MEETING" } },
  "education": [
    { "id": "edu-1", "degree": {"value": "12th Grade (CBSE Board)"}, "college": {"value": "The Orbis School"}, "end_date": {"value": "2022"} },
    { "id": "edu-2", "degree": {"value": "BTech"}, "field": {"value": "Computer Science"}, "college": {"value": "MIT Arts Design and Technology University"} }
  ],
  "experience": [], "projects": [], "certifications": [], "courses": [],
  "skills": [], "achievements": [], "awards": [], "languages": [], "additional_information": []
}
```

**Coverage status**

| Block | Status | Note |
|---|---|---|
| personal / summary | NOT_APPLICABLE | — |
| education | PARTIAL | edu-1: degree/college/end_date SUFFICIENT, field/start_date/location/grade still open. edu-2: not yet field-graded (lag) |
| experience | PARTIAL | no data |
| skills / projects / certifications / courses / achievements / awards / languages / additional_information | PARTIAL | empty |

**Question queue** (10, education re-worded, edu-2 not yet independently askable — it's hidden behind edu-1)
1. `gap:education` (targets edu-1) — "For your 12th at The Orbis School, do you know your stream, when you started, where it was located, or your grade/percentage?"
2. `gap:experience` — "Could you walk me through your work experience — where you worked, your role, and what you did there?"
3. `gap:skills` — "What are your main technical skills?"
4. `gap:projects` — "Have you worked on any personal or academic projects you'd like to share?"
5. `gap:certifications` — "Do you have any professional certifications?"
6. `gap:courses` — "Have you completed any standalone courses outside your degree?"
7. `gap:achievements` — "Do you have any notable achievements you'd like to mention?"
8. `gap:awards` — "Have you received any awards?"
9. `gap:languages` — "What languages do you speak or write?"
10. `gap:additional_information` — "Is there anything else you'd like to add to your resume?"

---

## Batch-3

**Call input:** `remaining_text ("based in ") + "Pune in 2026. I have one experience as an AI Intern at AISolve where my primary responsibilities wer"` → *"based in Pune in 2026. I have one experience as an AI Intern at AISolve where my primary responsibilities wer"*.
**Candidate queue going in** (state after Batch-2): unchanged order/shape (education still touched-first, targeting edu-1 with its narrowed 4-field set from Batch-2's grading).

**JOB 1 — extraction**
- `education[edu-2]` updated: `location: "Pune"`, `end_date: "2026"`
- new `experience` item **exp-1**: `company: "AISolve"`, `role: "AI Intern"`
- `status: "extracted"`, `remaining_text: "where my primary responsibilities wer"` (cut mid-word, "were" continues next batch)

**JOB 2 — completeness:** edu-2's `location`/`end_date` (fresh values, this batch) are graded `SUFFICIENT`, alongside the `degree`/`field`/`college` it already had (visible to `prune_for_judgment` since edu-2 existed before this batch) — so edu-2 is now `degree/field/college/location/end_date = SUFFICIENT`, only `start_date`/`grade` still `MISSING`. `experience` is graded only at the block level (`PARTIAL`) — exp-1 didn't exist before this batch, so it isn't field-graded yet (same lag).

**JOB 3 — queue:** education's candidate still targets edu-1 (edu-2 remains hidden behind it, regardless of edu-2's own completeness progress); experience's candidate is still whole-block wording since it isn't field-graded yet.

### State after Batch-3

**Extracted profile**
```json
{
  "personal": { "name": { "value": "Aviral Kumar", "source": "MEETING" } },
  "education": [
    { "id": "edu-1", "degree": {"value": "12th Grade (CBSE Board)"}, "college": {"value": "The Orbis School"}, "end_date": {"value": "2022"} },
    { "id": "edu-2", "degree": {"value": "BTech"}, "field": {"value": "Computer Science"}, "college": {"value": "MIT Arts Design and Technology University"}, "location": {"value": "Pune"}, "end_date": {"value": "2026"} }
  ],
  "experience": [
    { "id": "exp-1", "company": {"value": "AISolve"}, "role": {"value": "AI Intern"} }
  ],
  "projects": [], "certifications": [], "courses": [],
  "skills": [], "achievements": [], "awards": [], "languages": [], "additional_information": []
}
```

**Coverage status**

| Block | Status | Note |
|---|---|---|
| personal / summary | NOT_APPLICABLE | — |
| education | PARTIAL | edu-1: field/start_date/location/grade still open. edu-2: degree/field/college/location/end_date SUFFICIENT, start_date/grade open |
| experience | PARTIAL | exp-1 captured, not yet field-graded (lag) |
| skills / projects / certifications / courses / achievements / awards / languages / additional_information | PARTIAL | empty |

**Question queue** (10, unchanged shape from Batch-2 — education still on edu-1, experience still whole-block)
1. `gap:education` (edu-1) — "For your 12th at The Orbis School, do you know your stream, when you started, where it was located, or your grade/percentage?"
2. `gap:experience` — "Could you walk me through your work experience — where you worked, your role, and what you did there?"
3. `gap:skills` — "What are your main technical skills?"
4. `gap:projects` — "Have you worked on any personal or academic projects you'd like to share?"
5. `gap:certifications` — "Do you have any professional certifications?"
6. `gap:courses` — "Have you completed any standalone courses outside your degree?"
7. `gap:achievements` — "Do you have any notable achievements you'd like to mention?"
8. `gap:awards` — "Have you received any awards?"
9. `gap:languages` — "What languages do you speak or write?"
10. `gap:additional_information` — "Is there anything else you'd like to add to your resume?"

---

## Batch-4

**Call input:** `remaining_text ("where my primary responsibilities wer") + "e to build RAG pipelines, MCP tools and validate POC's. I do not have any personal projects and my m"` → *"where my primary responsibilities were to build RAG pipelines, MCP tools and validate POC's. I do not have any personal projects and my m"*.
**Candidate queue going in** (state after Batch-3): `experience` is now touched (exp-1 exists) → touched group = `experience(3), education(4)` (sorted by priority within the touched group), then untouched = `skills(5), projects(6), certifications(7), courses(8), achievements(9), awards(10), languages(11), additional_information(12)`.

**JOB 1 — extraction**
- `experience[exp-1]` updated: `responsibilities: ["Built RAG pipelines", "Built MCP tools", "Validated POCs"]`
- `projects`: no field update (nothing to extract into the schema) — the decline is a JOB 2 matter, not JOB 1
- `status: "extracted"`, `remaining_text: "and my m"` (cut mid-word, "main skills are…" continues past the pending buffer, not yet flushed to this pipeline)

**JOB 2 — completeness:**
- `experience`: exp-1's `company`/`role` (visible to `prune_for_judgment` since they predate this batch) graded `SUFFICIENT`. `responsibilities` was *just* captured this batch, so — same one-batch lag as every other new field — it stays `MISSING` in `field_completeness` until a hypothetical next batch, even though `resume_data` already has it. `start_date/end_date/location/skills/projects/achievements/awards` remain `MISSING` (never mentioned). Block-level `experience` stays `PARTIAL`.
- `projects`: the excerpt contains "I do not have any personal projects" — an explicit, unambiguous whole-block decline, exactly the example given in `combined_prompts.SYSTEM_PROMPT` JOB 2 → graded **`UNABLE_TO_ANSWER`**. Terminal: `resume.projects` stays `[]` (declines aren't written into `resume_data`, only into `field_completeness`), and the block permanently drops out of the candidate queue from here on (`_block_is_open` returns `False` once a status is terminal).
- `education`: unchanged from Batch-3 (nothing new said about it this batch).

**JOB 3 — queue:** `projects`'s candidate is still nominally in this batch's *input* candidate list (the decline wasn't known until this same call), but a well-behaved response recognizes the candidate just ruled it out in this very excerpt and omits it from the output `queue` — `combined_chain._validate_queue` only keeps entries the model actually returned, so it's simply absent going forward. Every other candidate gets re-worded against the now-current `resume_state`.

### State after Batch-4

**Extracted profile**
```json
{
  "personal": { "name": { "value": "Aviral Kumar", "source": "MEETING" } },
  "education": [
    { "id": "edu-1", "degree": {"value": "12th Grade (CBSE Board)"}, "college": {"value": "The Orbis School"}, "end_date": {"value": "2022"} },
    { "id": "edu-2", "degree": {"value": "BTech"}, "field": {"value": "Computer Science"}, "college": {"value": "MIT Arts Design and Technology University"}, "location": {"value": "Pune"}, "end_date": {"value": "2026"} }
  ],
  "experience": [
    {
      "id": "exp-1", "company": {"value": "AISolve"}, "role": {"value": "AI Intern"},
      "responsibilities": ["Built RAG pipelines", "Built MCP tools", "Validated POCs"]
    }
  ],
  "projects": [], "certifications": [], "courses": [],
  "skills": [], "achievements": [], "awards": [], "languages": [], "additional_information": []
}
```
*(`skills` is still empty here — "my main skills are Java, Python, …" hasn't reached the analysis queue as a chunk yet; it's the text sitting in the pending buffer.)*

**Coverage status**

| Block | Status | Note |
|---|---|---|
| personal / summary | NOT_APPLICABLE | — |
| experience | PARTIAL | company/role SUFFICIENT; responsibilities captured but not yet field-graded (lag); start_date/end_date/location/skills/projects/achievements/awards open |
| education | PARTIAL | edu-1: field/start_date/location/grade open. edu-2: start_date/grade open |
| **projects** | **UNABLE_TO_ANSWER** | explicit decline — terminal, closed for the rest of the session |
| skills / certifications / courses / achievements / awards / languages / additional_information | PARTIAL | empty (skills' actual text is still pending upstream) |

**Question queue** (9 — `projects` has dropped out)
1. `gap:experience` (exp-1) — "When did you start (and finish, if applicable) your AI Intern role at AISolve, and where was it based?"
2. `gap:education` (edu-1) — "For your 12th at The Orbis School, do you know your stream, when you started, where it was located, or your grade/percentage?"
3. `gap:skills` — "What are your main technical skills?"
4. `gap:certifications` — "Do you have any professional certifications?"
5. `gap:courses` — "Have you completed any standalone courses outside your degree?"
6. `gap:achievements` — "Do you have any notable achievements you'd like to mention?"
7. `gap:awards` — "Have you received any awards?"
8. `gap:languages` — "What languages do you speak or write?"
9. `gap:additional_information` — "Is there anything else you'd like to add to your resume?"

`InterviewDirector` will pop `gap:experience`'s worded question next, since it's now first in priority order (experience became "touched" ahead of education once exp-1 was created) — assuming the currently-open round isn't itself already targeting one of these (excluded via `_current_round_key`, not modeled here since we're only tracing the analysis worker, not the live director state).
