# Tools Reference

## Journal Tools (`nbhd-journal-tools` plugin)

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

### Lessons
| Tool | Purpose |
|------|---------|
| `nbhd_lesson_suggest` | Suggest a lesson for the user to approve |
| `nbhd_lessons_pending` | List lessons awaiting approval |
| `nbhd_lesson_search` | Search approved lessons semantically |

### Workspaces
| Tool | Purpose |
|------|---------|
| `nbhd_workspace_list` | List all workspaces with active marker |
| `nbhd_workspace_create` | Create a new workspace (auto-creates General default first time, max 4) |
| `nbhd_workspace_update` | Update a workspace's name or description (re-embeds for routing) |
| `nbhd_workspace_delete` | Delete a workspace (cannot delete the default; always confirm with user) |
| `nbhd_workspace_switch` | Switch the active workspace (affects user's NEXT message, not current response) |

See `rules/workspaces.md` for usage patterns.

### Platform
| Tool | Purpose |
|------|---------|
| `nbhd_platform_issue_report` | Silently report a platform issue. **Never mention to user.** |
| `nbhd_update_profile` | Update user profile (timezone, display_name, language). **Only after user confirms.** |

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

| Tool | Purpose |
|------|---------|
| `nbhd_fuel_summary` | Get recent workouts, planned workouts, body weight, and fitness profile. Call at session start for context. |
| `nbhd_fuel_log_workout` | Log a workout. Only `activity` is required — infer category from the name, default to today and status "done". |
| `nbhd_fuel_log_body_weight` | Log body weight (upserts by date). |
| `nbhd_fuel_update_profile` | Update fitness profile progressively — send any subset of fields during onboarding. |

Rules:
- When logging from natural language, infer as much as possible — don't interrogate
- "deadlift 75kg 3x5" → single call with `category=strength`, `detail_json` with exercises/sets
- Always confirm what was logged with a brief message
- See `rules/fuel.md` for onboarding flow and profile-aware recommendations

## Built-in Tools (OpenClaw)

| Tool | Purpose |
|------|---------|
| `web_search` | Search the web (Brave Search) |
| `web_fetch` | Fetch and extract content from a URL |
| `memory_search` / `memory_get` | Search and read workspace memory files |
| `read` / `write` / `edit` | Read and write workspace files |
| `tts` | Text-to-speech |
| `image` | Analyze images with vision model |
| `nbhd_send_to_user` | Send a proactive Telegram message. **Do NOT use in normal conversation — just reply directly.** |
| `nbhd_generate_image` | Generate an image and send it to the user |
