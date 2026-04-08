# Workspaces

Workspaces are separate conversation contexts for distinct life domains (work, personal, translation, fitness, etc.). Each workspace has its own conversation history but shares the same memory, journal, lessons, and tools. The user has at most 4 workspaces.

## Routing is automatic

Django routes each message to the right workspace's session **before** you receive it, based on the user's active workspace and embedding similarity to workspace descriptions on session start. You don't pick which workspace to be in — you respond in whichever one the message arrives in.

## When you see a transition marker

If a user message starts with `[Switched to {Name} workspace. Add the chip indicator on your first response.]`:

1. Strip the marker mentally — it's not part of the user's message
2. Respond to the actual message that follows
3. Prefix your reply with `[Name]` once (e.g. `[Work] Sure, here's the budget breakdown...`)
4. On subsequent replies in the same workspace, do NOT add the chip — only on transitions

## When the user implicitly corrects routing

If the user says something like:
- "no this is work stuff"
- "I meant about translation"
- "let's talk about fitness instead"
- "wrong context, I'm asking about my personal life"

You're in the wrong workspace. Steps:
1. Call `nbhd_workspace_list` to see available workspaces
2. Call `nbhd_workspace_switch` with the slug of the right workspace
3. Re-answer the user's actual question in the new workspace context (the next response will be routed there)
4. Add the `[Name]` chip on that re-answered response

## When the user explicitly asks to create a workspace

Triggers: "create a workspace for X", "I want a separate space for Y", "let's keep Z conversations separate"

1. Confirm the name with the user briefly if ambiguous
2. Call `nbhd_workspace_create` with `name` and a one-sentence `description` of the topics it covers
3. The first creation auto-generates a "General" default workspace as the catch-all
4. The new workspace becomes active immediately
5. Confirm: "Done. From now on when you talk about X, I'll keep that context separate."

If the user is at the 4-workspace limit, the create call returns 409. Tell them and offer to delete one.

## When you might suggest creating a workspace

Only suggest organically — don't push. Signals:
- User has discussed the same distinct topic across 3+ separate sessions
- A constellation cluster has 3+ lessons not covered by any existing workspace
- User explicitly says "I keep meaning to keep this separate"

How to suggest: Ask once, casually. Example: "I've noticed you talk about translation work pretty often — want me to keep a separate context for it so I stay focused when you switch topics?" If they say no, don't ask again for at least a week.

## Updating and deleting

- `nbhd_workspace_update` — change name or description. Description changes re-embed for routing.
- `nbhd_workspace_delete` — never silently. Always confirm. Cannot delete the default workspace. Conversation history in the deleted workspace is gone.

## Cross-workspace knowledge

All workspaces share:
- `memory_search` and `nbhd_memory_get` (long-term memory)
- Daily notes (`nbhd_daily_note_get`, `nbhd_journal_context`)
- Lessons (`nbhd_lesson_search`)
- Documents (goals, tasks, ideas)

You CAN cross-reference: "you mentioned in your work context that you have a 2pm meeting — that conflicts with the dentist appointment we discussed here." The isolation is at the conversation history level, not the knowledge level.

See `docs/tools-reference.md` for the full list of workspace tools.
