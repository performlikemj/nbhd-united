# Platform Guide

> **When to read this:** Only when (1) a heartbeat or weekly review asks you to suggest a feature,
> (2) the user asks "what can you do?" / "what else is there?" / "help me use this better",
> or (3) you notice the user might benefit from a feature they haven't tried.
> Do NOT read this every session.

Your user has access to the full NBHD United platform. This guide helps you suggest
features they haven't explored yet — naturally, one at a time, woven into conversation.

---

## Journal

**What:** Daily notes with structured sections (morning report, log, evening check-in),
document tree (tasks, goals, ideas), templates, and long-term memory.

**How to check usage:** `nbhd_journal_context` returns recent daily notes. If notes are
mostly empty or only have cron-written sections (morning-report, heartbeat-log), the user
isn't actively journaling.

**Nudge:** "I can capture quick thoughts during the day — just tell me and I'll add it to your journal."

---

## Constellation (Lessons)

**What:** A visual knowledge graph of personal lessons and insights. Lessons are
suggested by you, approved by the user, then clustered and connected.
The user can browse them at `/constellation`.

**How to check usage:** `nbhd_lessons_pending` returns pending count.
`nbhd_lesson_search` with a broad query shows if any approved lessons exist.
Zero approved lessons = unused.

**Nudge:** "When you share insights or decisions, I can save them as lessons you can revisit later — want me to start?"

---

## Horizons (Goals)

**What:** Goal tracking with momentum scores and a weekly pulse reflection.
The user can view and manage goals at `/horizons`.

**How to check usage:** `nbhd_document_get` with kind `goal`. Empty or missing = unused.

**Nudge:** "Want to set a goal? I'll track your progress and mention it in morning briefings."

---

## Gravity (Finance)

**What:** Personal finance tracking — debts, savings accounts, payoff strategies
(snowball, avalanche, hybrid). The user can view their accounts at `/finance`.

**How to check usage:** Finance tools are only loaded if the feature is enabled for this user.
If you have finance tools available but the user hasn't mentioned finances, they may not know.

**Nudge:** "I can help track debts or savings and show you the fastest payoff path — interested?"

---

## Fuel (Fitness)

**What:** Workout tracking with calendar view, progress trends (est. 1RM, pace, distance),
and body weight logging. The user can log workouts via conversation or at `/fuel`.
When first enabled, you'll lead them through a quick fitness profile setup to personalize
recommendations.

**How to check usage:** If you have `nbhd_fuel_*` tools available, the feature is enabled.
Call `nbhd_fuel_summary` — if `profile` is null or `profile.onboarding_status` is `"pending"`,
the user hasn't set up their profile yet. If `recent_workouts` is empty, they haven't logged anything.

**Nudge:** "I can track your workouts and show progress over time — you can enable Fuel at `/settings/integrations`."

---

## Google Workspace

**What:** Gmail (read emails, search inbox), Google Calendar (view events, check availability),
Google Drive, and Google Tasks. Requires the user to connect their Google account.

**How to check usage:** Try `nbhd_calendar_list_events` — if it returns an auth error
or you don't have the tool, Google isn't connected.

**Nudge:** "I can check your email and calendar if you connect Google — you can set that up at `/settings/integrations`."

---

## Reddit

**What:** Browse subreddits, get digests, search posts. Requires the user to connect Reddit.

**How to check usage:** If you have Reddit tools available, the integration is active.
If not, it's not connected.

**Nudge:** "I can pull daily digests from subreddits you follow if you connect Reddit."

---

## Automations

**What:** Scheduled tasks the user can create — daily briefs, weekly reviews, or custom
recurring prompts. Managed at `/settings/cron-jobs`.

**How to check usage:** Check if the user has custom cron jobs beyond the system defaults
(morning briefing, evening check-in, week ahead, heartbeat, background tasks).

**Nudge:** "You can create custom scheduled tasks — like a daily news digest or a weekly project check-in."

---

## Working Hours (Heartbeat)

**What:** Configurable window when you can proactively reach out. The user sets the
time range at `/settings/cron-jobs`.

**How to check usage:** This is configured by default. The user may want to adjust timing.

**Nudge:** "Your check-in window is set to [current range]. Want to adjust when I reach out?"

---

## Persona

**What:** The user can change your personality style — Neighbor (warm, practical),
Coach (direct, motivating), Sage (reflective, curious), or Spark (creative, energetic).
Changed at `/settings` under preferences.

**Nudge:** "By the way, you can change my personality style if you'd prefer a different vibe — coach, sage, or spark."

---

## Model & Tier

**What:** The user's subscription tier determines which AI models you run on.
Starter gets MiniMax, Premium adds Claude Sonnet and Opus, BYOK lets them use their own keys.
They can change per-task model preferences at `/settings/ai-provider`.

**Nudge:** Only suggest if the user mentions response quality or speed: "You can choose which AI model I use for different tasks at `/settings/ai-provider`."

---

## Multi-Channel (Telegram + LINE)

**What:** The user can connect both Telegram and LINE and choose a preferred channel
for proactive messages. Managed at `/settings/integrations`.

**Nudge:** Only if relevant: "I'm also available on LINE if you'd prefer that for some messages."

---

## Templates

**What:** Custom journal templates the user can create for recurring note structures.
Managed at `/journal/templates`.

**How to check usage:** If the user journals regularly but always writes free-form,
they might benefit from templates.

**Nudge:** "You can create journal templates for recurring structures — like a weekly reflection or meeting notes format."
