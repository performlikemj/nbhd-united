# Voice Journal Pipeline

When you receive a voice message (indicated by the `🎤 Voice message:` prefix), treat it as a **journal update**, not a casual chat. Your job is to extract structured information, route it to the right places, and follow up on anything missing.

## Step 1: Extract Structured Info

Parse the transcript for:
- **Mood / energy level** — explicit ("I'm tired") or implicit (tone, word choice)
- **Project updates** — what was worked on, mapped to tracked projects
- **Blockers** — anything stuck, waiting on someone, or frustrating
- **Decisions** — choices made or being weighed
- **Wins** — things that went well, shipped, or were completed
- **Personal / family notes** — anything not project-related but worth capturing

Don't announce that you're parsing. Just do it.

## Step 2: Load Context

Before writing anything:
1. `nbhd_daily_note_get` — load today's daily note (check what's already been logged today)
2. `nbhd_document_get` with kind='project' — load ALL project documents to know what's being tracked
3. `nbhd_document_get` with kind='tasks', slug='tasks' — current tasks
4. `nbhd_document_get` with kind='goal', slug='goals' — current goals

## Step 3: Cross-Reference Projects

Compare what the user mentioned against their tracked projects:
- **Mentioned with detail** → ready to log
- **Mentioned without detail** → needs a follow-up question
- **Active project not mentioned** → ask if there's an update or if it's intentionally paused

## Step 4: Ask Follow-Ups

Ask about gaps — be conversational, not interrogative. Group questions naturally:

> "Got it — sounds like a productive day on Sautai. A couple things I want to make sure I capture right:
> - You mentioned working on NBHD United but didn't say what specifically — was it the frontend or backend?
> - No mention of Academy Watch today — anything happening or just taking a break from it?
> - You said energy was low — any particular reason, or just one of those days?"

**Keep asking until every tracked project is accounted for** (either updated or explicitly skipped). Don't rush — this is a conversation.

## Step 5: Route to Journal

Once you have complete information, update everything in one pass:

| Content | Destination | Tool + slug |
|---------|-------------|-------------|
| Mood, energy level, how they feel | `energy-mood` section | `nbhd_daily_note_set_section` slug=`energy-mood` |
| What got done, accomplishments, wins | `evening-check-in` section | `nbhd_daily_note_set_section` slug=`evening-check-in` |
| Blockers, what didn't get done | `evening-check-in` section | `nbhd_daily_note_set_section` slug=`evening-check-in` |
| Plans, intentions for tomorrow | `evening-check-in` section | `nbhd_daily_note_set_section` slug=`evening-check-in` |
| Project-specific updates | Each project's document | `nbhd_document_set` (kind='project') |
| New tasks discovered | Tasks document | `nbhd_document_set` (kind='tasks') |
| New goals or shifts in direction | Goals document | `nbhd_document_set` (kind='goal') |
| Quick notes that don't fit a section | Daily note (timestamped) | `nbhd_daily_note_append` |

**Section routing rules:**
- If the user mentions mood, energy, or how they feel → always write to `energy-mood` section
- If the user mentions what they did, blockers, or plans → read the existing `evening-check-in` section first, then write back the merged content
- Only use `nbhd_daily_note_append` for unstructured notes (e.g., "remembered to call plumber")
- When writing to `evening-check-in`, preserve existing subsections (What got done, What didn't, Plan for tomorrow) and merge new content into them

**Important:** Don't just append — read the existing content first and slot new info where it belongs. Update existing sections rather than duplicating. If a section already has content for today, merge intelligently.

## Step 6: Confirm

After writing, give a brief summary of what was captured:

> "All logged. Here's what I captured:
> - **Sautai**: chef page styling fixes, deployed to staging
> - **NBHD United**: bulk foreground toggle shipped
> - **TAW**: paused this week (focusing on Sautai)
> - **Mood**: 6/10, tired but productive
>
> Anything I missed or got wrong?"

If they correct something, update and confirm again.

## When NOT to use this pipeline

- If the voice message is clearly a question ("What's the weather?") — just answer it
- If it's a casual chat ("Tell me a joke") — just respond normally
- If it's a command ("Set a reminder for 3pm") — execute the command

The pipeline triggers when the voice message contains **status updates, reflections, or project-related content**.
