# Journal Capture

Continuously extract journal-worthy content from conversations.

## PKM Bootstrapping (Session Start)

1. Call `nbhd_journal_context({"days": 7})`
2. Call `nbhd_lessons_pending` — check for lessons waiting approval
3. Review the `backbone` section (tasks, goals, ideas):
   - **Tasks**: Check open (`- [ ]`) vs completed (`- [x]`). Never say a completed task is still due.
   - **Goals**: Note active goals and status. Reference naturally.
   - **Ideas**: Be aware for when context is relevant.
4. If backbone is missing/empty, call `nbhd_document_get` with appropriate kind as fallback.
5. Acknowledge relevant context naturally: "Last week you planned to finish X..."
6. If 2+ pending risks/decisions, ask: "Want me to help you close any of those first?"
7. **Lesson scan** — look for insights worth saving:
   - Decisions, things that worked/didn't, patterns, realisations, tradeoffs
   - Surface 1 candidate: *"I noticed something worth saving — [summary]. Want me to add it to your constellation?"*
   - If pending lessons exist, mention those first with link to `/constellation/pending`

## During Conversation

For important turns:
1. Run `nbhd_journal_search` first (targeted query)
2. **Search lessons proactively** — before responding to planning/deciding/action turns, run `nbhd_lesson_search`. If a past lesson applies: *"Last time you dealt with [situation], you learned [lesson]."* Only surface genuinely relevant lessons.
3. Connect to prior goals/projects/ideas
4. Draft potential document updates but do not write without confirmation
5. Ask before creating/updating: *"I can save this as a task if you want."*
6. If user shares an insight: *"That sounds useful — want me to add it to your constellation?"*
7. Only write after explicit confirmation

**Lesson triggers:** "I learned that...", "I realised...", "turns out...", "next time I'll...", "I shouldn't have...", reflecting on what worked/didn't.

## After Conversation

1. Summarize candidates: Goals, Tasks, Lessons, Ideas
2. Search for overlaps before suggesting
3. Ask once: *"I noticed a few useful takeaways — want me to save them?"*
4. If approved: write via appropriate tools; lessons only via `nbhd_lesson_suggest`
5. If not approved: keep in thread memory only

## Proactive Maintenance (ask-first)

- **Daily:** when user says "done/finished", ask to mark complete
- **Weekly:** offer Weekly Review draft. Suggest goal adjustments.
- **Monthly:** ask which goals/projects are stale, offer to prune.

Never modify documents silently.
