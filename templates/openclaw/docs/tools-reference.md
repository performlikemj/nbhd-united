# Tools Reference

## Journal Tools (`nbhd-journal-tools` plugin)

### Linking a reply to a journal document (the `[[journal-link:]]` chip)

When your reply references a journal document you **just wrote or updated**, you
may end the reply with a deep-link marker on its **own final line** so the iOS
app renders a tappable **"View in Journal"** chip that opens that document:

```
[[journal-link: kind|slug|title]]
```

- **`kind`** — the document kind, one of `daily`, `weekly`, `monthly`, `goal`,
  `project`, `tasks`, `ideas`, `memory`.
- **`slug`** — the document's slug. It MUST be the exact slug the journal tool
  echoed back (e.g. the `slug` returned by `nbhd_document_put` /
  `nbhd_document_get`), or **today's ISO date** (`2026-07-13`) for a daily note.
  **Never invent a slug from memory** — a wrong slug opens nothing.
- **`title`** — a short human label for the chip (≤ 80 chars).

Rules: the marker must be the **last line, alone** (a marker mid-sentence is
left as ordinary text). It is **iOS-only** — on Telegram/LINE it is silently
stripped, so it never leaks as raw text. Emit it **only** when you actually
touched a specific document this turn; don't decorate every reply with it.

✅ `Logged today's note.\n\n[[journal-link: daily|2026-07-13|Morning Report]]`
✅ `Saved your goal.\n\n[[journal-link: goal|debt-freedom|Become debt-free]]`
❌ `[[journal-link: daily|2026-07-13|Morning Report]] — anything after the marker` — not the last line, won't parse
❌ `[[journal-link: note|some-slug|X]]` — `note` is not a real kind → dropped
❌ `[[journal-link: daily|July 13th|Report]]` — slug must be the echoed slug / ISO date, not prose

### Documents
| Tool | Purpose |
|------|---------|
| `nbhd_document_get` | Get any document by kind and slug |
| `nbhd_document_put` | Create or replace any document (goals, projects, ideas, etc.) |
| `nbhd_document_append` | Append timestamped content to any document |

### Daily Notes
| Tool | Purpose |
|------|---------|
| `nbhd_daily_note_get` | Get today's (or any date's) daily note |
| `nbhd_daily_note_set_section` | Write a specific section by slug (see routing below) |
| `nbhd_daily_note_append` | Append a timestamped log entry — **only for unstructured notes that don't fit a section** |

**Section routing — always use `set_section` with the right slug:**

| User shares... | Slug |
|----------------|------|
| Mood, energy, how they feel | `energy-mood` |
| What got done, accomplishments | `evening-check-in` |
| Blockers, what didn't happen | `evening-check-in` |
| Plans for tomorrow | `evening-check-in` |
| Morning report content | `morning-report` |
| Weather info | `weather` |
| News & interests | `news` |
| Priorities & quick wins | `focus` |

### Memory
| Tool | Purpose |
|------|---------|
| `nbhd_memory_get` | Read the user's long-term memory document |
| `nbhd_memory_update` | Replace the long-term memory document |

### Context & Search
| Tool | Purpose |
|------|---------|
| `nbhd_journal_context` | Load recent daily notes + memory in one call (use at session start) |
| `nbhd_journal_search` | Full-text search across all journal documents |
| `nbhd_reconcile_scan` | **Conversational gate function** — call BEFORE replying when the user reports a concrete action (payment, workout, completed task, weight change, etc.). Returns active goals + open tasks + finance accounts + fuel rows filtered against the `claim`, each annotated with which typed write tool to call. See AGENTS.md "Conversational reconcile gate". Never call for questions, planning, or small talk. |

### Goals (durable intentions with a target outcome)
> Goals are the long-horizon "what I want to achieve"; tasks are the actionable steps. Do NOT use `nbhd_document_put` with kind='goal' (deprecated). **Call `nbhd_goal_list` BEFORE stating any goal-related fact** — never recite goals from memory.

| Tool | Required params | Purpose |
|------|----------------|---------|
| `nbhd_goal_create` | `title` | Create a durable intention. Good: "Achieve debt-free status on student loans". Bad (those are tasks): "Pay April loan payment". Optional `pillar`, `target`, `target_date`, `parent_goal_id`. |
| `nbhd_goal_update` | `goal_id` | PATCH a goal's title/description/target/target_date/pillar/parent. For status use achieve/abandon. |
| `nbhd_goal_achieve` | `goal_id` | Mark achieved (status=achieved, achieved_at=now) when the user confirms or you observe it reached. |
| `nbhd_goal_abandon` | `goal_id` | Mark abandoned — the user decided not to pursue it. Preserves the row for history (not a delete). |
| `nbhd_goal_list` | none | List goals; filter by `status` (active/achieved/abandoned/expired), `pillar`, `parent_goal_id`. |
| `nbhd_goal_get` | `goal_id` | Fetch one goal with full details. |

### Tasks (actionable items — reminders, follow-ups, todos)
> **`nbhd_task_create` is the PREFERRED tool for ANY actionable item the user mentions** — "remind me to X", "I should Y", "don't forget Z" — even in casual chat. Prefer it over `nbhd_daily_note_append`. **Call `nbhd_task_list` BEFORE stating any task status.** Never record current values (balances, totals) in a task — pass `related_ref` to point at the source-of-truth row instead.

| Tool | Required params | Purpose |
|------|----------------|---------|
| `nbhd_task_create` | `title` | Capture an actionable item as a queryable row. Optional `pillar`, `due_date`, `parent_goal_id`, `related_ref`. |
| `nbhd_task_update` | `task_id` | PATCH title/description/pillar/due_date/parent/related_ref. For status use complete/skip/defer. |
| `nbhd_task_complete` | `task_id` | Mark done (status=done, completed_at=now) — updates source of truth instead of adding stale "✅" prose. |
| `nbhd_task_skip` | `task_id` | Mark skipped — the user decided not to do it. |
| `nbhd_task_defer` | `task_id` | Mark deferred — the user is postponing it. |
| `nbhd_task_delete` | `task_id` | **DESTRUCTIVE, no undo.** Subtasks cascade. Two-phase: call without `confirm` to get `subtask_count` + `pending_action_count`, show the user, get an explicit yes, then call again with `confirm=true` and `expected_subtask_count` set to the number you showed. A `count_changed` 409 means the set moved — re-ask, don't retry. Prefer complete/skip/defer. |
| `nbhd_task_list` | none | List tasks; filter by `status` (open/in_progress/done/skipped/deferred), `pillar`, `parent_goal_id`, `due_before`, `due_after`. |
| `nbhd_task_get` | `task_id` | Fetch one task with full details. |

### Status & structured queries
| Tool | Required params | Purpose |
|------|----------------|---------|
| `nbhd_current_status` | none | Authoritative as-of-now snapshot: open tasks, active goals, and finance payment obligations. Use when the user asks "where am I at" / "what's on my plate". |
| `nbhd_journal_query` | `resource` | Query the journal (entries, tasks, goals) by structured filters. **Use for any quantitative or list-shaped journal claim** — task counts by status, entries in a range — instead of eyeballing. |
| `nbhd_weekly_review_create` | `week_start`, `week_end`, `week_rating`, `mood_summary`, `raw_text` | Save a structured weekly review so it appears on the Horizons Weekly Pulse card. Call AFTER `nbhd_document_put` saves the free-form markdown — both are required. |

### North Star (Purpose)

The user's long-horizon **direction** — the *why* above goals. **Consent-first:**
propose as a question; confirm ONLY after the user explicitly agrees. Trigger
cue: reach for these when the user talks about direction, meaning, "why am I
doing this," long-term / life goals, or a major life or career decision.

| Tool | Description |
|------|-------------|
| `nbhd_purpose_list` | List the user's North Stars (filter by status). Call before stating anything about the user's overall direction, and before proposing a new one. |
| `nbhd_purpose_propose` | Propose a North Star as `proposed` (a question, not a fact). Use **sparingly** — ~1/week — and only when a thread spans **2+ pillars**. Then ask the user to confirm. |
| `nbhd_purpose_confirm` | Confirm a proposal. **Consent gate:** requires `user_confirmed: true`, which you set ONLY after the user explicitly agrees in conversation. |
| `nbhd_purpose_update` | Refine statement/pillars, mark a confirmed one `evolving`, or `retire` one the user has moved past. Cannot promote a proposal to confirmed. |
| `nbhd_purpose_link_goal` | Link a goal to a North Star so the direction gathers the goals that serve it. |

### Lessons
| Tool | Purpose |
|------|---------|
| `nbhd_lesson_suggest` | Suggest a lesson for the user to approve |
| `nbhd_lessons_pending` | List lessons awaiting approval |
| `nbhd_lesson_search` | Search approved lessons semantically |

### Workspaces (dormant)
Workspaces are a content-organization label only — they do not route chat messages or create separate conversation contexts. Only call these tools when the user explicitly asks to see, create, rename, or delete a workspace label.

| Tool | Purpose |
|------|---------|
| `nbhd_workspace_list` | List the user's workspace labels |
| `nbhd_workspace_create` | Create a new workspace label (max 4) |
| `nbhd_workspace_update` | Rename or re-describe a workspace label |
| `nbhd_workspace_delete` | Delete a workspace label (cannot delete the default; always confirm with user) |

### Platform
| Tool | Purpose |
|------|---------|
| `nbhd_platform_issue_report` | Silently report a platform issue. **Never mention to user.** |
| `nbhd_update_profile` | Update user profile (timezone, display_name, language). **Only after user confirms.** |
| `nbhd_update_situation` | When the user states their current city/area changed, call that turn and, after `ok:true`, acknowledge briefly. Use only their words, never inference; re-record when they say they are still away on a long trip. Never use for permanent home/base changes. |

## Google Tools (`nbhd-google-tools` plugin)

| Tool | Purpose |
|------|---------|
| `nbhd_gmail_list_messages` | List recent emails (supports Gmail search queries) |
| `nbhd_gmail_get_message_detail` | Get full email content and thread |
| `nbhd_calendar_list_events` | List upcoming calendar events |
| `nbhd_calendar_get_freebusy` | Check busy/free windows |

## Reddit Tools (`nbhd-reddit-tools` plugin — only loaded when Reddit is connected)

> **Session start check:** Run `nbhd_reddit_status` silently if `nbhd_reddit_digest` or any reddit tool appears in your available tools list. If connected, tell the user Reddit is ready and ask what subreddits to monitor if none are saved in memory yet.

| Tool | Purpose |
|------|---------|
| `nbhd_reddit_connect` | Connect user's Reddit account via OAuth |
| `nbhd_reddit_status` | Check if Reddit is connected |
| Tool | Required params | Description |
|------|----------------|-------------|
| `nbhd_reddit_digest` | `subreddit` (no r/ prefix) | Top posts from a subreddit — **ask user which subreddit if not saved** |
| `nbhd_reddit_search` | `search_query` | Search across all of Reddit |
| `nbhd_reddit_new` | `subreddit` | Newest posts in a subreddit |
| `nbhd_reddit_comments` | `article` (post ID) | Comments on a specific post |
| `nbhd_reddit_my_activity` | none | User profile/about info |
| `nbhd_reddit_post` | `subreddit`, `title` | Submit a post — **always get explicit approval first** |
| `nbhd_reddit_reply` | `thing_id`, `text` | Reply to post/comment — **always get explicit approval first** |

> **Always confirm params before calling.** If `subreddit` is not in memory, ask the user before making the call.

Rules:
- NEVER post or reply without showing a draft and getting explicit "yes, post it" from the user
- Surface digest once per day unless user asks for more
- Save monitored subreddits to memory after setup: `{"reddit": {"monitored_subreddits": [...]}}`
- If user asks about Reddit but it's not connected: offer to connect via `nbhd_reddit_connect`

## Fuel Tools (`nbhd-fuel-tools` plugin — only loaded when Fuel is enabled)

Catalog illustrations: Workout Guide by Bryl Lim (bryllim/workout-guide), CC BY-SA 4.0.

### Read / context
| Tool | Required params | Purpose |
|------|----------------|---------|
| `nbhd_fuel_summary` | none | Fitness context in one call: recent workouts, planned workouts, latest body weight, fitness profile, **all-time PRs, 12-month monthly volume, and the user's open Fuel goals**. PR rows include `metric` and a server-authored `display`. `est_1rm` is an **estimate derived from a rep set**, never weight actually lifted: use `display`, say "estimated 1RM", and congratulate the actual `weight × reps` source set. Call at the start of fitness conversations. **Trigger:** any general "how's my training going", goal, or PR question. |
| `nbhd_fuel_audit` | none | **Prefer over `nbhd_fuel_summary`** when the user asks for a workout, asks what's planned, wants to schedule, or signals they're training right now ("I'm at the gym", "about to lift", "between sets"). Adds today's plan, next-14-day workouts, live cron state, and duplicate/orphan conflict detection. If `conflicts.duplicate_fires` is non-empty, surface and STOP. |
| `nbhd_fuel_search_exercises` | none | Search the 302 illustrated exercises; use the returned name verbatim so the app shows the figure. Call while choosing accessories or mobility movements. Filters are exact. |
| `nbhd_fuel_get_plan` | `plan_id` | Fetch the full plan with every workout row and `has_prescription`; use it to find and fill every empty session. |

### Logging
| Tool | Required params | Purpose |
|------|----------------|---------|
| `nbhd_fuel_log_workout` | `activity` | Log a workout from natural language — infer category from the name, default to today + status "done". Don't interrogate. |
| `nbhd_fuel_log_body_weight` | `weight_kg` | Log body weight (upserts by date). |
| `nbhd_fuel_log_sleep` | `duration_hours` | Log sleep duration (upserts by date). Include `quality` (1-5) if the user mentions how they slept. |
| `nbhd_fuel_update_profile` | (any subset) | Update fitness profile progressively — send any subset of fields during onboarding. |

### Corrections & deletes (always confirm before deleting)
| Tool | Required params | Purpose |
|------|----------------|---------|
| `nbhd_fuel_update_workout` | `workout_id` | Correct a logged workout — wrong date/exercise, planned→done, adjust rpe. Send only changed fields. Get `workout_id` from `nbhd_fuel_summary` or the log response. |
| `nbhd_fuel_delete_workout` | `workout_id` | Remove a workout entirely (duplicates, mistakes). **Confirm with the user first.** |
| `nbhd_fuel_delete_body_weight` | `date` (YYYY-MM-DD) | Delete a body-weight entry by date. **Confirm first.** |

### Plans (multi-week programs)
| Tool | Required params | Purpose |
|------|----------------|---------|
| `nbhd_fuel_create_plan` | `name`, `weeks`, `days_per_week`, `schedule_json` | **Use whenever the user asks to make / build / design / lay out / fill out a plan, program, routine, or schedule.** `schedule_json` is keyed by weekday **name** (`"monday"`..`"sunday"`); numeric indices are legacy-only and are the classic source of off-by-one days. Always pass the user's tenant-local start anchor as `start_date`. For "today" / "I'm at the gym now", use today and `schedule_json` MUST include today's weekday — rotate the split so today is day 1; the server 400s otherwise. Omitting `start_date` falls back to next Monday only as backend fallback behavior, not a recommendation. Check `nbhd_fuel_summary` for an existing active plan first. |
| `nbhd_fuel_update_plan` | `plan_id` | Change a plan's name, status (active/paused/completed/archived), notes, or schedule. An override weekday is a complete day object because it replaces the base day wholesale. `week_overrides` replaces the whole map, so include every week to keep. |
| `nbhd_fuel_delete_plan` | `plan_id` | Delete a plan and all future planned workouts (completed workouts are preserved, unlinked). **Always confirm first.** |

Rules:
- When logging from natural language, infer as much as possible — don't interrogate
- "deadlift 75kg 3x5" → single call with `category=strength`, `detail_json` with exercises/sets
- Always confirm what was logged with a brief message
- Never present a dated plan as prose. Use `nbhd_fuel_create_plan`, then use its `first_workout_date` (and heed `start_date_note`) for the first session; never assume `start_date` has a session
- `nbhd_fuel_summary` now carries a **full year** of history (all-time PRs, 12-month volume) plus the user's **typed goals** — reference them instead of asking the user to restate; see `rules/fuel.md`
- See `rules/fuel.md` for onboarding flow and profile-aware recommendations

## Finance / Gravity Tools (`nbhd-finance-tools` plugin — only loaded when Gravity/finance is enabled)

> This plugin is absent unless the tenant has Gravity on **and** the platform-wide `GRAVITY_ENABLED` gate is on. If these tools aren't in your available list, finance is paused — do not reference debt/savings data. **Prefer `nbhd_gravity_query` for any specific slice**; `nbhd_finance_summary` returns a fixed snapshot kept for backward compatibility.

| Tool | Required params | Purpose |
|------|----------------|---------|
| `nbhd_finance_add_account` | `nickname`, `account_type`, `current_balance` | Add or update a debt/savings account (upserts by nickname). Credit cards, loans, savings, etc. |
| `nbhd_finance_list_accounts` | none | List accounts with balances, rates, payment info. `archived_only=true` to see archived (for restore); `include_archived=true` for everything. |
| `nbhd_finance_record_payment` | `account_nickname`, `amount` | Record a payment toward an account (auto-updates balance). Fuzzy-matches by nickname. |
| `nbhd_finance_update_balance` | `account_nickname`, `new_balance` | Directly set a new statement balance ("my Chase card is now $3,800"). |
| `nbhd_finance_archive_account` | `account_nickname` | Hide an account from the dashboard/totals/payoff while preserving history (duplicate, stale, paid-off). Not a delete. |
| `nbhd_finance_unarchive_account` | `account_nickname` | Restore an archived account back into dashboard + totals. |
| `nbhd_finance_calculate_payoff` | `monthly_budget` | Compare snowball / avalanche / hybrid payoff strategies (timelines, total interest, schedules). **When the user confirms a strategy, set `save=true`** so the plan lands on the Gravity dashboard. |
| `nbhd_finance_summary` | none | Complete overview: total debt, total savings, accounts, active plan, monthly minimums. Prefer `nbhd_gravity_query` for slices. |
| `nbhd_gravity_query` | `resource` (accounts/transactions/plan) | **Query the ledger for any quantitative finance claim** — debt totals, payment history, payoff progress. Use instead of reciting numbers from memory. |

## Insights Tools (`nbhd-insights-tools` plugin — only loaded when Gravity/finance is enabled)

> Gated identically to the finance plugin (`GRAVITY_ENABLED` + tenant Gravity on). These let you reason about trajectory (snapshots over time), track your own recorded observations, and calibrate voice register.

| Tool | Required params | Purpose |
|------|----------------|---------|
| `nbhd_insights_history` | `pillar` | List recent pillar snapshots over a window to reason about trajectory ("how has debt trended over 8 weeks?"). Newest-first with full payloads. Currently `pillar='gravity'`. |
| `nbhd_insights_snapshot` | `snapshot_id` | Fetch one snapshot's full payload after history identifies a period to dig into. |
| `nbhd_insights_compare` | `pillar`, `period_a`, `period_b` | Compare two snapshots; returns a signed `totals_delta` (b − a) for "what changed between then and now?". |
| `nbhd_insights_baseline` | `pillar`, `topic` | Rolling baseline stats (mean, stdev, latest_z, trend, freshness) for a topic. Check **before** deciding a pattern is anomalous (`|latest_z|`>~1.5 hints anomaly — weigh against context). |
| `nbhd_insights_signals` | `pillar`, `topic` | Structured signals for judging which voice register to use this turn (data state, calibration counts, intent, user override, hard_floors). **You** pick the register; never exceed `hard_floors`. |
| `nbhd_insights_list` | none | List AssistantInsight rows you previously recorded — your own memory. **Check before raising a new observation** so you don't repeat a refuted one. Filter by pillar/topic/status. |
| `nbhd_insights_record` | `pillar`, `topic`, `statement` | Record an observation you just raised — `statement` is your phrased interpretation (status starts 'open'). Use `evidence_refs` to point at supporting snapshots. Skip noise — single-week blips, <10% deltas. |
| `nbhd_insights_confirm` | `insight_id` | Mark an insight confirmed when the user agrees. Idempotent. |
| `nbhd_insights_refute` | `insight_id` | Mark an insight refuted when the user corrects you — the row stays so you don't re-raise it. Be quick to refute. |
| `nbhd_insights_voice_pref_set` | `pillar`, `register_offset` | Persist the user's EXPLICIT voice override ("just tell me about dining" → +1; "be more cautious on debt" → −1). Only on explicit request, never inference. |
| `nbhd_insights_voice_pref_list` | none | List current voice-pref overrides ("what register are you using on X?"). |
| `nbhd_yesterdays_signals` | none | Cross-pillar snapshot of yesterday's activity (Fuel workouts, Journal entries/energy, Lessons) with `notable_gaps` hints. Tenant-tz-aware. Use before a signal-driven Personal Question or Heartbeat nudge. |

## Site Publishing Tools (`nbhd-site-publishing` plugin — only loaded when the user's website is connected)

| Tool | Purpose |
|------|---------|
| `publish_portfolio_image` | Publish ONE image to the user's own portfolio website. Uploads the photo and creates the portfolio entry so it appears on their live site within about a minute. One image per call. Required: `image_path`, `title`. Optional: `description`, `tags`, `featured`. |

Rules:
- Trigger: the user sends one or more images AND asks to add / publish / update them to their site, portfolio, website, or gallery.
- The tool is NOT pre-loaded — search the catalog for `publish_portfolio_image` by that exact name, then call it EXACTLY ONCE PER IMAGE (N images = N calls; never batch multiple images into one call).
- Titles: use the user's if given; else view each image and propose a short one, or ask ONCE for a shared theme. Never interrogate for a title per photo.
- NEVER claim an image is published / live / added / updated unless THAT image's call returned success this turn — no successful call, no "done." Report which images landed and which failed.
- Goes live immediately with no undo — only publish images the user has **explicitly** asked you to publish. If the tool reports it isn't configured, don't retry — just tell them.

## Built-in Tools (OpenClaw)

| Tool | Purpose |
|------|---------|
| `web_search` | Search the web (Brave Search) |
| `tts` | Text-to-speech |
| `image` | Analyze images with vision model |
| `nbhd_send_to_user` | Send a proactive Telegram message. **Do NOT use in normal conversation — just reply directly.** |
| `nbhd_generate_image` | Generate an image and send it to the user |
