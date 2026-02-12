---
name: weekly-review
description: Guide the user through a weekly reflection — reviewing journal entries, celebrating wins, and setting intentions for the next week.
---

# Weekly Review

Help the user step back and look at their whole week. Pull in their journal entries, find patterns, celebrate progress, and set intentions for next week. Think of it as a friendly Sunday evening conversation over tea.

## When to Use

- User asks to review their week ("how was my week?", "weekly review", "let's look back")
- It's Sunday evening and the user is checking in
- User says something like "what a week" or "glad that week is over"
- End-of-week reflection was scheduled

## When NOT to Use

- User is doing daily journaling (use `daily-journal` skill)
- Mid-week casual conversation — don't push a full review on a Wednesday
- User is asking about tasks or to-dos
- User wants to set goals without reviewing the past week first — help them, don't force a review
- User just wants to vent about one specific thing — listen, don't turn it into a structured review

## Before Starting: Fetch the Week's Data

Retrieve this week's journal entries before beginning the conversation:

```
GET /api/runtime/journal-entries/?date_from=2026-02-06&date_to=2026-02-12
Authorization: Bearer $RUNTIME_API_TOKEN
```

This gives you the raw material. If there are no entries, that's okay — you can still do a review from memory.

## Conversation Flow

### 1. Set the Scene
Make it feel like a pause, not a task.

- "Hey! Ready to look back at your week? I pulled up your journal entries — let's see what happened."
- If no journal entries: "Looks like we didn't journal much this week — no worries! Let's just talk through it. What stands out?"

### 2. Highlight Patterns
Look across the week's entries for themes. Share what you notice.

- "I noticed your energy was high early in the week but dipped Thursday and Friday. What do you think was going on?"
- "You mentioned [recurring challenge] a couple of times. Want to talk about that?"
- "Three out of five days you mentioned [positive thing] — that's a real pattern!"

If entries are sparse, skip this and go conversational.

### 3. Wins of the Week
Aggregate wins from daily entries + ask if they're missing anything.

- "Here are the wins I captured this week: [list]. Anything else you'd add?"
- "Which one are you most proud of?"

### 4. Challenges & Lessons
Same for challenges — look for patterns, not just a list.

- "The tough stuff this week: [list]. Any of these feel resolved, or are they carrying into next week?"
- "What did you learn from any of these?"

### 5. Overall Week Rating
Keep it simple and human.

- "If this week were a movie, would you give it a thumbs up, thumbs down, or a 'meh'?"
- Or: "One word for this week?"

### 6. Intentions for Next Week
Forward-looking but grounded. Not a to-do list — intentions.

- "What's one thing you want to focus on next week?"
- "Anything you want to do differently?"
- "Any events or commitments you're looking forward to — or dreading?"

Capture 1-3 intentions.

### 7. Wrap Up

- Summarize the review warmly: "Great week to reflect on. Here's the snapshot: [brief summary]. Saved!"
- "Have a good rest of your Sunday 💛"

## API Integration

### Save the Weekly Review

```
POST /api/runtime/weekly-reviews/
Authorization: Bearer $RUNTIME_API_TOKEN
Content-Type: application/json

{
  "week_start": "2026-02-06",
  "week_end": "2026-02-12",
  "mood_summary": "Started strong, dipped mid-week, recovered Friday",
  "top_wins": [
    "Finished the project proposal",
    "Exercised 4 out of 5 days",
    "Had a great conversation with Mom"
  ],
  "top_challenges": [
    "Team conflict on Wednesday",
    "Sleep was inconsistent"
  ],
  "lessons": [
    "Need to address conflict earlier instead of letting it simmer"
  ],
  "week_rating": "thumbs-up",
  "intentions_next_week": [
    "Talk to Jamie about the project timeline",
    "Get to bed by 11pm at least 4 nights"
  ],
  "raw_text": "Full conversational summary of the review session"
}
```

### Fields

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| week_start | string (YYYY-MM-DD) | yes | Monday of the review week |
| week_end | string (YYYY-MM-DD) | yes | Sunday of the review week |
| mood_summary | string | yes | Narrative of mood arc across the week |
| top_wins | array of strings | yes | Aggregated + curated from entries |
| top_challenges | array of strings | yes | Aggregated + curated from entries |
| lessons | array of strings | no | Can be empty or null |
| week_rating | string | yes | "thumbs-up", "thumbs-down", or "meh" |
| intentions_next_week | array of strings | yes | 1-3 forward-looking intentions |
| raw_text | string | yes | Natural language summary of the session |

### Error Handling

- If fetching journal entries fails: Proceed without them. "I couldn't pull up your entries right now, but let's review from memory."
- If saving the review fails: Same pattern as daily-journal — retry once, reassure the user, never lose their words.

## Tone Guide

- Reflective, not evaluative. You're helping them see their week, not grading it.
- Pattern-spotting, not lecturing. "I noticed X" not "You should do X."
- Celebrate genuinely. Don't be a cheerleader — be a friend who's actually happy for them.
- Keep it flowing. This should feel like a 5-10 minute conversation, not a form to fill out.

## Edge Cases

- **No journal entries at all this week:** That's fine. Do a purely conversational review. Don't guilt them about not journaling.
- **User only journaled once:** Use that one entry as a starting point but lean on conversation for the rest.
- **User wants to skip sections:** Let them. A partial review is better than a forced one.
- **User gets emotional during review:** Same as daily-journal — be human, be supportive, don't push structure.
- **It's not Sunday:** Weekly reviews can happen any day. Adjust the date range to cover the last 7 days from today.
- **User did a review earlier this week:** Acknowledge it. "We did a review on Wednesday — want to just cover since then, or do a fresh full-week look?"
