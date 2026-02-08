# Shared Intelligence — Agent-Mediated Social Knowledge

**Status:** Vision / Phase 3-4
**Prerequisite:** Solo agent MVP with paying users

## The Idea

Users in a group (neighbors, friends, community) share useful knowledge with each other — but their AI agents do the heavy lifting behind the scenes. Agents curate, suggest, and absorb. Humans approve and get the credit.

From the outside, it looks like a group of unusually well-informed, thoughtful people. The agents are invisible.

## How It Works

### The Share Flow (Outbound)

1. Agent learns something useful from its user's activity, conversations, or research
2. Agent privately suggests to its user: "This might be useful for the group — want to share?"
3. User can approve, edit, or reject
4. If approved, it's posted to the group **as the user** — human gets credit, agent stays invisible
5. User can also manually share anything unprompted

### The Absorb Flow (Inbound)

1. Someone in the group shares something (via the flow above, or just typing in the group chat)
2. Every member's agent quietly absorbs the information into its own memory
3. Agent surfaces it later when relevant: "Remember that restaurant Kenji shared last month? It's near where you're headed tonight"
4. No notification spam — the agent holds it until it's useful

### Key Principle: Agents Are Backstage

- Agents NEVER post to the group directly
- Agents NEVER chime in where everyone can see
- All group-visible communication comes from the human
- The agent is a private advisor, not a public participant

## Data Model (Sketch)

```
Group
  - id, name, description
  - created_by (User)
  - created_at

GroupMembership
  - group, user
  - joined_at
  - role (member, admin)
  - share_preferences (what categories the agent can suggest sharing)

SharedKnowledge
  - group, author (User)
  - content (text)
  - category (local_tip, recommendation, event, deal, general)
  - suggested_by_agent (bool) — was this agent-suggested or user-initiated?
  - created_at

PendingShare (private to each user)
  - user, group
  - suggested_content (what the agent wants to share)
  - source_context (why the agent thinks it's useful)
  - status (pending, approved, edited, rejected)
  - final_content (what actually got shared, if approved)
  - created_at, resolved_at
```

## Agent Behavior

### What agents suggest sharing:
- Local discoveries (restaurants, services, shops, parks)
- Deals or time-sensitive info
- Solutions to problems others in the group have mentioned
- Event recommendations matching group interests
- Practical tips (transit, weather, seasonal stuff)

### What agents NEVER suggest sharing:
- Health information
- Financial details
- Personal/family matters
- Anything from private conversations
- Anything the user hasn't explicitly discussed in a shareable context

### How agents absorb group knowledge:
- Store shared items in a group memory context
- Tag with relevance signals (location, category, freshness)
- Surface when the user's current context intersects (planning an outing, asking about food, etc.)
- Let stale knowledge decay — a restaurant rec from 6 months ago gets lower priority

## User Experience

### Joining a Group
- Invite link or QR code (reuse the Telegram linking pattern)
- User sets sharing preferences: what categories they're open to agent suggestions for
- Can mute agent suggestions per group without leaving

### In the Group Chat
- Looks like a normal group chat — humans talking to humans
- Could be Telegram group, or a native chat in the NBHD United app
- No bot messages, no "[shared via AI]" tags, no weirdness

### Private Agent Notifications
- Agent DMs user: "You visited that new ramen place yesterday and seemed to like it. The Nishi-ku group has been talking about lunch spots — want to share a recommendation?"
- User taps approve, edits the text, or dismisses
- If approved, it shows up in the group as: "[User]: Just tried the new ramen place on Midosuji — highly recommend the tsukemen 🍜"

## Why This Is Different

| Existing solutions | Problem |
|---|---|
| Nextdoor | Toxic, complaint-driven, ad-heavy |
| Facebook Groups | Noisy, algorithmic, hard to find signal |
| WhatsApp/LINE groups | Useful but chaotic, no memory, info gets buried |
| Word of mouth | Doesn't scale, depends on social energy |

NBHD United's advantage: **agents do the remembering, curating, and connecting.** Humans just approve and talk. The group's collective knowledge compounds over time without anyone having to manually organize it.

## Privacy & Trust

This feature lives or dies on trust. Non-negotiables:

1. **Explicit opt-in** — users choose to join groups and set sharing preferences
2. **Approval gate on every share** — nothing goes out without human confirmation
3. **Transparency** — users can see everything their agent has absorbed from the group
4. **Easy exit** — leave group = agent purges group knowledge (or keeps it, user's choice)
5. **No cross-group leakage** — agent doesn't suggest sharing Group A's knowledge in Group B unless the info is the user's own

## Technical Considerations

- Group chat routing: extend the existing Telegram router or build native chat
- Shared memory store: separate from personal agent memory, scoped per group
- Agent publish/subscribe: lightweight event system for group knowledge updates
- Approval queue: push notification or Telegram DM to user's agent chat
- Knowledge decay: TTL or relevance scoring to keep group memory fresh

## Sequencing

**Don't build this until:**
- [ ] Solo agent MVP is live and people are paying
- [ ] At least 10 active users to test group dynamics
- [ ] Personal agent memory/context is solid enough that suggestions are good

**Build order when ready:**
1. Group model + membership + invite flow
2. SharedKnowledge store + basic group chat
3. PendingShare + approval flow
4. Agent suggestion engine (what to share)
5. Agent absorption + contextual surfacing

## Open Questions

- Telegram group vs native chat vs both?
- How many groups can one user join? (cap to prevent agent noise)
- Do agents in the same group ever coordinate directly (agent-to-agent), or only through the shared knowledge pool?
- Monetization: is this included in $5/mo or a group add-on?
- Content moderation: what happens when someone shares bad info?

---

*Documented 2026-02-08. Build it when the foundation is solid.*
