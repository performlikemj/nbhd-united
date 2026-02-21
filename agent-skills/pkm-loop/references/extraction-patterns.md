# PKM Loop Extraction Patterns

These examples show how raw conversation text turns into typed PKM candidates.

## Pattern A — goal + tasks + lesson

**Input**

- "I want to ship the chef discovery page by Friday so we can test in QA on Monday."
- "I finished the first integration test for checkout and it caught two critical edge cases."

**Candidates**

```json
[
  {
    "kind": "goal",
    "text": "Ship the chef discovery page by Friday",
    "why": "Enable QA testing by Monday",
    "due_date": "YYYY-MM-DD",
    "evidence": "I want to ship... by Friday so we can test...",
    "source": "turn_001"
  },
  {
    "kind": "task",
    "text": "Prepare QA environment for chef discovery page",
    "why": "Required for Monday QA run",
    "due_date": "YYYY-MM-DD",
    "status_signal": "new",
    "evidence": "we can test in QA on Monday",
    "source": "turn_001"
  },
  {
    "kind": "lesson",
    "text": "Checkout integration tests surfaced two critical edge cases, suggesting we should add a pre-flight checklist before merge.",
    "evidence": "it caught two critical edge cases",
    "source": "turn_002"
  }
]
```

---

## Pattern B — idea + task + lesson

**Input**

- "What if we send SMS check-ins to users at 9 PM? Might reduce missed evening updates."
- "I blocked on a missing ingredient mapping, so no build this evening."

**Candidates**

- Idea: `SMS check-in reminders at 9 PM to reduce missed evening updates`
- Lesson: `Missing ingredient mapping dependency blocked completion; need dependency-owner check in planning`
- Task: `Resolve ingredient mapping blocker` (if user asks for follow-up or agrees to action)

---

## Pattern C — status signal (done/completed)

**Input**

- "Done — I finished updating the menu import script and all tests passed."

**Candidate**

```json
{
  "kind": "task",
  "text": "Update the menu import script",
  "status_signal": "done",
  "evidence": "Done — I finished updating the menu import script",
  "why": "This task has now changed state",
  "source": "turn_010"
}
```

Ask whether to mark complete in tasks doc before writing.

---

## Pattern D — ambiguous claim (default to lower-commitment)

**Input**

- "Maybe we should try a new onboarding copy for the chef page."

**Candidate**

- classification: `idea` (not goal/task because no commitment/owner/due date)
- cross-check using `nbhd_journal_search` for duplicate prior copy variants
- ask: "Want me to save this as an experiment idea?"

## Cross-referencing examples

- If `nbhd_lesson_search({ query: "ingredient mapping", limit: 5 })` returns similar lesson,
  - mark as **related** and attach in confirmation: "This is close to lesson X about blocked dependencies."
- If `nbhd_document_get({ kind: "tasks", slug: "tasks" })` contains `Update menu import script`,
  - mark task candidate as **duplicate_candidate** and propose status update instead of new item.
