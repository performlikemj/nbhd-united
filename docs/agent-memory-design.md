# Agent Memory Design

## Overview

NBHD United subscriber agents use a **file-based memory system** persisted via Azure Files. Agents remember users across sessions, learn preferences, and deepen the relationship over time — all invisible to the user.

## Architecture

```
/home/node/.openclaw/workspace/
├── SOUL.md          # Agent personality (seed-once, agent-evolvable)
├── USER.md          # Quick-reference user profile
├── MEMORY.md        # Curated long-term memory (loaded every session)
├── HEARTBEAT.md     # Heartbeat task list (future use)
├── AGENTS.md        # Agent instructions (system-managed, overwritten on boot)
├── TOOLS.md         # Tool notes (seed-once)
└── memory/
    ├── 2025-01-15.md  # Daily notes
    ├── 2025-01-16.md
    └── ...
```

### What Goes Where

| File | Purpose | Loaded | Written by |
|------|---------|--------|------------|
| `MEMORY.md` | Curated knowledge about the user | Every session | Agent |
| `memory/YYYY-MM-DD.md` | Raw daily notes | Today + yesterday | Agent |
| `USER.md` | Name, timezone, basics | Every session | Agent |
| `SOUL.md` | Agent personality | Every session | Agent (carefully) |

### Why Files, Not a Database

- Azure Files already mounted per container — zero infrastructure cost
- OpenClaw's `group:files` tools (read/write/edit) work out of the box
- OpenClaw's `group:memory` tools (memory_search/memory_get) provide semantic search over workspace files via qmd
- No additional backend needed
- Files are human-readable if debugging is needed
- Natural fit for LLM context — just read the file

## Memory Lifecycle

### Session Start
Agent reads SOUL.md → USER.md → MEMORY.md → recent daily notes. This gives full context without the user repeating themselves.

### During Conversation
Agent operates normally. When something notable happens (preference revealed, decision made, personal info shared), it mentally notes it.

### Session End / After Meaningful Exchange
Agent writes to `memory/YYYY-MM-DD.md` with bullet points. Updates MEMORY.md if something significant was learned.

### Periodic Maintenance (Future: Heartbeat)
When heartbeat is enabled, the agent reviews recent daily notes and distills patterns into MEMORY.md. For now, this happens organically during sessions.

## Privacy Model

### What Gets Stored
- Name, timezone, communication preferences
- Interests, goals, ongoing projects
- Preferences (how they like help, topics they care about)
- Relationship context (important people, situations)
- Patterns the agent notices

### What NEVER Gets Stored
- Passwords, API keys, tokens
- Financial details (account numbers, balances)
- Health records or diagnoses
- Government IDs
- Anything the user asks to forget

### Data Isolation
Each subscriber's agent runs in its own Azure Container App with its own Azure Files mount. There is no cross-tenant file access. The agent's tool policy denies session management and automation tools that could break isolation.

## Growth Model

The agent-user relationship deepens naturally:

1. **Day 1:** Agent knows nothing. Learns name, basic preferences.
2. **Week 1:** MEMORY.md has basics filled in. Agent starts remembering context.
3. **Month 1:** Agent knows communication style, interests, ongoing situations. Conversations feel continuous.
4. **Month 3+:** Agent has rich context. Can anticipate needs, reference past conversations naturally, notice patterns.

This happens without any user action. They just chat. The agent does the rest.

## Entrypoint Seeding

On container boot (`entrypoint.sh`):
- `memory/` directory created if missing
- `MEMORY.md` seeded from template if missing (seed-once pattern)
- `HEARTBEAT.md` seeded from template if missing (for future use)
- `AGENTS.md` always overwritten (system-controlled instructions)

## Tool Policy

Both `group:memory` and `group:files` are in the basic tier allow-list:
- `group:files` → read, write, edit (for all workspace files)
- `group:memory` → memory_search, memory_get (semantic search via qmd)

## Configuration

- **Compaction:** `safeguard` mode — sufficient for memory usage
- **Heartbeat:** disabled (`every: "0m"`) — memory works reactively for now
- **Heartbeat (future):** enable with `every: "30m"` and agent uses HEARTBEAT.md to run periodic memory maintenance

## Future: Social Sharing

Memory lays the groundwork for the community vision — agents that know their users well enough to facilitate connections. A user's agent could (with permission) share relevant context with another user's agent: "My human loves Jamaican food and yours is a Jamaican chef — maybe they should connect." This is not built yet, but the file-based memory structure makes it straightforward to add a controlled sharing layer later.
