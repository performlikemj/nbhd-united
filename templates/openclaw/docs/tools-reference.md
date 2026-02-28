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
| `nbhd_daily_note_set_section` | Write a specific section (morning-report, weather, news, focus, evening-check-in) |
| `nbhd_daily_note_append` | Append a timestamped log entry (marks author=agent) |

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
| `nbhd_reddit_digest` | Get top posts from monitored subreddits |
| `nbhd_reddit_my_activity` | Check replies to user's posts/comments |
| `nbhd_reddit_post` | Submit a post — **always get explicit approval first** |
| `nbhd_reddit_reply` | Reply to a post/comment — **always get explicit approval first** |

Rules:
- NEVER post or reply without showing a draft and getting explicit "yes, post it" from the user
- Surface digest once per day unless user asks for more
- Save monitored subreddits to memory after setup: `{"reddit": {"monitored_subreddits": [...]}}`
- If user asks about Reddit but it's not connected: offer to connect via `nbhd_reddit_connect`

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
