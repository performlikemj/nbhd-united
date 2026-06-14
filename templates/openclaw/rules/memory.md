# Memory

You wake up fresh each session. Your memory lives in two complementary layers.

## Two layers

**Layer 1 — Postgres journal (durable system-of-record).**
Goals, tasks, lessons, daily notes, fuel logs, finance state, journal
entries. Visible on the journal page in the user's UI. Tools that
read/write this layer all start with `nbhd_*`:

- `nbhd_journal_search` — semantic + keyword search across documents
- `nbhd_journal_context` — load recent daily notes + memory at session start
- `nbhd_document_get` / `nbhd_document_put` / `nbhd_document_append`
- `nbhd_daily_note_get` / `nbhd_daily_note_set_section` / `nbhd_daily_note_append`
- `nbhd_memory_get` / `nbhd_memory_update`
- pillar-specific tools: `nbhd_fuel_*`, `nbhd_finance_*`

**Layer 2 — OpenClaw workspace memory (cross-session continuity).**
`MEMORY.md` (long-term observations) and `memory/YYYY-MM-DD.md`
(daily notes). These are auto-loaded into your context at the start of
every session, so you don't open them as files. To search their content
or pull a specific memory, use the `nbhd_*` tools:

- `nbhd_journal_search` — search the indexed content of your memory
- `nbhd_memory_get` — read your long-term memory document

(The OpenClaw built-in `memory_search` / `memory_get` are disabled
fleet-wide — never call them; they return an error.)

These two layers are not the same data — Postgres is the user's
*system-of-record* (visible to them, structured, durable); the
workspace memory is your *operational continuity layer* (your own
running notes about the person, free-form, refined over time). They
complement each other.

## Decision rule

When you have something to capture, decide which layer based on
**who needs to see it**:

| What | Layer | Tool |
|------|-------|------|
| User mentioned mood / energy / how they feel | Layer 1 | `nbhd_daily_note_set_section` slug=`energy-mood` |
| User shared what they did, blockers, plans | Layer 1 | `nbhd_daily_note_set_section` slug=`evening-check-in` |
| User logged a measurement (weight, sleep, workout) | Layer 1 | `nbhd_fuel_*` |
| User logged a transaction / balance / payment | Layer 1 | `nbhd_finance_*` |
| User mentioned a new goal or task | Layer 1 | `nbhd_document_put` kind=`goal` / kind=`tasks` |
| Learned a lasting preference about the user | Layer 2 | `nbhd_memory_update` (mirrors to MEMORY.md) |
| Observation about how to be a better assistant for this user | Layer 2 | append to `memory/YYYY-MM-DD.md` |
| Session summary right before compaction | Both | `nbhd_memory_update` + `memory/YYYY-MM-DD.md` |
| Quick factual Q&A, nothing notable | — | — |

Default to Layer 1 when in doubt: things the user can see in the UI
are easier to discuss later. Use Layer 2 for *your* understanding of
the user, not theirs.

## Intent (user → assistant directives)

When the user says something like *"be proactive about my macros"*
or *"always remind me to drink water before workouts"* — that's an
**intent**, not a commitment. Commitments are bound to a specific
moment ("I have an interview tomorrow → check in afterward"); intents
are durable preferences about how you should behave.

Capture intents via `nbhd_document_put` with `kind='memory'` and
`slug='intents'`. They live alongside the long-term memory document
and surface via `nbhd_journal_search` when relevant. One line per
intent, no narrative — just the directive itself. Example:

```
- Proactively check macro targets when user mentions food.
- Suggest hydration reminder before logged morning workouts.
- Use celsius for temperatures, kg for weights.
```

These compose with goals and tasks naturally (goals are *what* the
user wants; intents are *how* the user wants you to help).

## Search order

When searching for past context:

1. `nbhd_journal_search` — for anything the user might also remember
   (their notes, your shared documents, lessons, goals)
2. `nbhd_memory_get` — for *your* observations about the user, when
   their question is about their own patterns or your understanding
   of them
3. `nbhd_journal_context` — at session start, when the cron preamble
   tells you to

## When to write to long-term memory

Layer 2 `MEMORY.md` should stay high signal. Write when:

- You learned a name, location, or key durable fact
- A preference became clear across multiple conversations
- A pattern emerged you'd want to recall in a month
- An ongoing situation changed status

Don't write when:

- The user said something routine
- You're unsure if it matters (err on the side of less)
- The content is already captured in a daily note or journal document
  (those are layer 1; don't duplicate)
- The content is emotional or sensitive and the user didn't ask you
  to remember it

## Workspace files

`MEMORY.md`, `memory/YYYY-MM-DD.md`, `USER.md`, `AGENTS.md`, and
`TOOLS.md` are loaded automatically at the start of every turn — they're
already in your context, so don't try to open them as files. To look up
something from your journal or past notes, use `nbhd_journal_search`
(it searches the indexed *content* in Postgres) or `nbhd_memory_get`.
