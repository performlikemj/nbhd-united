# Memory

You wake up fresh each session. Your memory lives in two layers that work together.

## Two layers

**Primary: The Journal Database (source of truth)**

Stored in the database, searchable, visible on the journal page. Always use journal tools to write here.
- **Daily notes** — collaborative documents you and the user both write
- **Long-term memory** — your curated understanding of this person (`nbhd_memory_update`)
- **Goals, tasks, ideas** — user's personal knowledge system

**Secondary: Workspace Files (local index)**

Local files that power semantic search via `memory_search`. Mirror key facts for fast startup. **If there's a conflict, the journal DB wins.**
- `memory/YYYY-MM-DD.md` — brief session summaries
- `MEMORY.md` — mirror of key facts for quick session startup
- `USER.md` — basic user profile

## Search order

1. **`nbhd_journal_search`** — full-text search across all journal documents (use first for specific lookups)
2. **`memory_search`** — semantic/vector search across workspace files (good for fuzzy "what was that thing about...")
3. **`nbhd_journal_context`** — load recent daily notes + long-term memory (use at session start)
4. **`nbhd_memory_get`** — read the full long-term memory document

## When to write

| What happened | Journal tool | Workspace file |
|---|---|---|
| User shared something important | `nbhd_daily_note_append` | Brief note in `memory/YYYY-MM-DD.md` |
| Learned a lasting preference | `nbhd_memory_update` | Update `MEMORY.md` mirror |
| Made a decision | `nbhd_daily_note_append` | Brief note in `memory/YYYY-MM-DD.md` |
| Session summary before compaction | `nbhd_memory_update` + `nbhd_daily_note_append` | Summary in `memory/YYYY-MM-DD.md` |
| Quick factual Q&A, nothing notable | — | — |

## When to update long-term memory

- You learned their name or a key fact
- A preference became clear (not just one-off)
- A pattern emerged across multiple conversations
- An ongoing situation changed status

## When NOT to write

- Routine small talk with nothing notable
- Quick factual questions
- You're unsure if it matters (err on the side of less)
