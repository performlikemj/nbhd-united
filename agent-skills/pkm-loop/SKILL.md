---
name: pkm-loop
description: >
  Extract, classify, cross-reference, and persist goals/tasks/ideas/lessons from conversation and automation passes.
  Use when: end-of-conversation reflection cues, scheduled daily/weekly/monthly review triggers, or explicit user request to "save this"/"track this"/"remember".
  Don't use for routine chatting where no durable capture is needed.
---

# PKM Loop

## Purpose

This skill runs a repeatable Personal Knowledge Management pass so context is captured consistently:

- **Extract** durable items from conversation context
- **Classify** candidates into `goal`, `task`, `idea`, or `lesson`
- **Cross-reference** existing docs/lessons to avoid duplicates
- **Ask once** with grouped, natural confirmation UX
- **Write** only after explicit user approval
- **Run review cycles** (daily, weekly, monthly)

Primary tools required:

- `nbhd_document_get`
- `nbhd_document_put`
- `nbhd_document_append`
- `nbhd_lesson_suggest`
- `nbhd_lesson_search`
- `nbhd_lessons_pending`
- `nbhd_journal_search`
- `nbhd_journal_context`
- `nbhd_memory_get`
- `nbhd_memory_update`

## When to start this skill

Start PKM Loop when any of the following occurs:

1. **End of meaningful user turn**
   - Planning language: "I need...", "I want to...", "I should...", "I learned..."
   - Commit language: "done", "finished", "I will", "I started".
2. **Session-start context bootstrap**
   - Run a lightweight context scan before normal response.
3. **Scheduled automation**
   - Daily reflection, weekly review, monthly goals check.
4. **User explicitly asks**
   - "save this", "remember", "track", "capture", "add to goals/tasks/lessons".

If no actionable signal is found, skip write operations and continue normal response.

## 0) Session-start PKM bootstrap (every session)

1. Call `nbhd_journal_context({ "days": 14 })`.
2. Call `nbhd_lessons_pending({})`.
3. Optionally call `nbhd_journal_search` for the latest high-signal keywords from user message.
4. Internally prepare context for later confirmation prompts; do not mention tool calls in user-visible language.

## 1) Extraction pipeline

### 1.1 Build candidate candidates list

From the current conversation window and recent context:

1. Split user statements into atomic claims.
2. Keep only claims with durable value:
   - explicit outcomes / intentions
   - concrete actions / commitments
   - hypotheses / design ideas
   - reflections / insights / tradeoffs
3. For each claim, produce a candidate object:

```json
{
  "kind": "goal|task|idea|lesson",
  "text": "short proposition",
  "why": "what prompted it",
  "evidence": "quote/summary from convo context",
  "owner": "user|team|unknown",
  "due_date": "YYYY-MM-DD if explicit",
  "status_signal": "new|in_progress|done",
  "confidence": 0.0,
  "source": "turn_id/date/message_id"
}
```

### 1.2 Candidate examples

- “Let’s finish onboarding flow by Friday” → likely **task** (`due_date`, `owner=self`, `status_signal=new`).
- “I want to build a reusable grocery planning playbook” → **goal** (if strategic) or **idea** (if experimental; pick one).
- “I noticed checking out-of-stock SKUs early saves 12 minutes” → **lesson**.
- “We should try SMS reminders for cooks” → **idea**.

## 2) Classification rules

Use this order to classify each candidate:

1. **Goal**
   - Strategic outcome statement tied to a success condition.
   - Usually has horizon >1 day and measurable target.
2. **Task**
   - Concrete action item with owner and/or next-step phrasing.
   - Often has operational details and dependency/blocker.
3. **Lesson**
   - Reflection on what worked, failed, or insight with caveat/tradeoff.
   - Prefer this for reusable learnings and future recall.
4. **Idea**
   - Hypothesis, experiment, alternative approach, or feature suggestion.
   - Not yet anchored as commitment.

If ambiguous: classify as the **lowest-commitment type** first (idea), unless user explicitness is high.

### 2.1 Duplicate/related detection rules

For each candidate, run:

1. `nbhd_document_get({ kind: "goal", slug: "goal" })` and `nbhd_document_get({ kind: "tasks", slug: "tasks" })`
2. `nbhd_journal_search({ query: candidate.text, kind: "goal|tasks|ideas|weekly|daily", limit: 5 })`
3. `nbhd_lesson_search({ query: candidate.text, limit: 5 })`

Mark each candidate as:

- `new`: no strong match.
- `duplicate_candidate`: near same statement found.
- `related`: overlapping topic but different intent/timeframe.

Use source text + similarity hints from results to avoid duplicate writes.

## 3) Natural confirmation UX (required before writes)

Do not present as a rigid form. Use a single grouped proposal with warm language.

Template:

- "I spotted a few items I can save for you:

  **Capture now?**
  - **Goals**: ...
  - **Tasks**: ...
  - **Ideas**: ...
  - **Lessons**: ...
  
  I noticed a couple of related existing items:
  - ...
  - ...

  If you want, I can save these in one batch." 

Then ask:
- "Want me to save only the clearly new ones, update the related ones, or skip everything?"

Allowed user responses map:
- `save / go ahead / yes` → save all clearly new and related updates.
- `only goals`, `just tasks`, etc. → save selected groups.
- `skip` → write nothing, keep in conversation memory only.

Tone: concise, collaborative, not robotic. Group by kind and preserve order: goal → task → idea → lesson.

## 4) Write workflow (after approval)

### Goals (`goal`)
- For each goal candidate: `nbhd_document_get({ kind: "goal", slug: "goal" })`.
- If no existing section for item, append with `nbhd_document_append({ kind: "goal", content: "- [ ] ..." })`.
- For edits, use `nbhd_document_put` with updated `markdown`.

### Tasks (`task`)
- Load `tasks` doc via `nbhd_document_get({ kind: "tasks", slug: "tasks" })`.
- Insert/append using `nbhd_document_append({ kind: "tasks", content: "- [ ] ..." })`.
- For blockers/completion state changes, keep one concise status update line and avoid full rewrites unless editing structure.

### Ideas (`idea`)
- Use `nbhd_document_append({ kind: "ideas", content: "## Idea: ..." })`.
- For ideas becoming plans, convert to task via user confirmation in same batch.

### Lessons (`lesson`)
- Use `nbhd_lesson_suggest({
    text,
    context,
    source_type: "conversation",
    source_ref: source,
    tags
  })`
- Do not bypass lesson suggest; this keeps approvals in the lesson queue.

### Memory (`memory`)
- Read with `nbhd_memory_get({})`.
- If user confirms a recurring pattern or major preference shift, summarize as short update and write with `nbhd_memory_update({ markdown })`.

### Pending lessons
- If `nbhd_lessons_pending` returns items, include a gentle reminder at session end, not auto-save.

## 5) Review cycles

### A. Daily reflection
Use at automation time (end of day):

1. `nbhd_journal_context({ days: 1 })`
2. `nbhd_journal_search` for unfinished tasks and blockers
3. `nbhd_lessons_pending`
4. Propose:
   - day win/focus
   - unfinished tasks
   - 0–3 candidate lessons
5. Ask: "Want a brief daily snapshot saved now?" and write only on approval via `nbhd_document_put` / append.

### B. Weekly review draft
Triggered by weekly review automation:

1. `nbhd_journal_context({ days: 7 })`
2. `nbhd_journal_search` for goals/tasks/blocked items
3. `nbhd_lesson_search({ query: 'weekly review', limit: 8 })`
4. Build draft markdown sections:
   - Wins
   - Misses/risks
   - Completed tasks
   - Stalled assumptions
   - 3-week focus proposals
5. Ask for approval to save as `kind: "weekly"`, `slug: YYYY-MM-DD` (Monday date).
6. If approved call `nbhd_document_put({ kind: "weekly", slug: "...", title: "Weekly Review — ...", markdown: draft })`.

### C. Monthly goals check
On the 1st of month (or monthly-mode review pass):

1. `nbhd_document_get({ kind: "goal", slug: "goal" })`
2. `nbhd_document_get({ kind: "tasks", slug: "tasks" })`
3. `nbhd_lesson_search({ query: "stalled|blocked|recurring", limit: 10 })`
4. Propose changes:
   - keep / defer / split / retire goals
   - re-prioritize 1–3 goals
   - ask permission to revise goal/task structure
5. Ask to persist suggestions with grouped `nbhd_document_put`.

## 6) Automation mode mapping

- `daily_brief` automation should be treated as **daily reflection mode**.
- `weekly_review` automation should trigger **weekly review draft mode**.
- Monthly pass can piggyback on `weekly_review` automation by checking day-of-month and switching to monthly mode when today is the 1st.

## 7) Safety and quality rules

- Never write without explicit user approval.
- If one group fails, continue others and report status clearly.
- Don’t overfit: if confidence is low, classify as idea or lesson and ask for clarification.
- Keep payloads small; avoid tool-call loops >3 minutes.
- If no tool is available, state limitation and offer manual capture fallback.

## 8) Success criteria for one pass

A successful pass should:

- capture only user-relevant durable knowledge
- avoid duplicates
- include user-visible confirmation before each doc write
- produce reusable context for future conversations

Refer to `references/extraction-patterns.md` and `references/review-templates.md` for examples and draft formats.
