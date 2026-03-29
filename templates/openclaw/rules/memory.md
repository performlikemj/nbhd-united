# Memory

Two layers — journal DB wins over workspace files if they conflict:

- **Journal DB** (source of truth): daily notes, long-term memory, goals, tasks, ideas. Write here via journal tools.
- **Workspace files** (local index): `memory/YYYY-MM-DD.md`, `MEMORY.md`, `USER.md`. Mirror key facts for fast startup.

Search order: `nbhd_journal_search` → `memory_search` → `nbhd_journal_context`.

Write to daily note when: user shares something important, a decision is made, a preference is clear, meaningful work happened. Skip routine small talk.
