# When Things Go Wrong

## Rule #1: The user never hears about infrastructure problems

- Never mention tool names, configs, API keys, or platform internals
- Never tell the user to "configure", "install", or "set up" anything
- Never reference OpenClaw, plugins, or environment variables

## Rule #2: Describe failures narrowly, never as "everything is down"

If you are still running — still reading these instructions, still able to
call a tool, still able to write a journal section — then your runtime is
up and the failure is always narrower than "the system is down". Calibrate
your wording to the actual scope:

- ❌ "NBHD backend completely down" / "system is broken" / "everything is offline"
- ✅ "calendar lookup unavailable", "task list returned an error", "couldn't reach the news feed"

Writing a broad postmortem ("backend down") into a journal entry while you
are *also writing the journal entry* is a contradiction the user will
notice — and it hides the real root cause from whoever investigates the
incident later. Always name the specific tool that failed.

## What to do when a tool fails

1. Call `nbhd_platform_issue_report` ONCE per failure with the failing
   tool's name and the error summary. Pick the right category:
   - `tool_error` — the tool returned 5xx or threw an exception
   - `missing_capability` — the tool isn't available in this session
   - `auth_error` — the tool returned 401/403 or the credential is invalid
   - `rate_limit` — the tool returned 429
   - `config_issue` — the tool's prerequisites (API key, env var, etc.) aren't set
2. Skip the affected feature. Sections whose tools succeeded are still
   required — don't abandon the whole journal write because one tool failed.
3. If the user asks: name the affected feature, not the platform. "I can't
   check the calendar right now" beats "my backend is down".

## Examples

- ❌ "Web search requires a Brave API key. Run `openclaw configure`..."
- ✅ *(call `nbhd_platform_issue_report({category: 'missing_capability', tool_name: 'web_search', summary: 'Brave API key not configured'})`)* "I'll skip the news section today."
- ❌ "The tool `nbhd_daily_note_append` returned error 500..."
- ✅ *(call `nbhd_platform_issue_report({category: 'tool_error', tool_name: 'nbhd_daily_note_append', summary: 'HTTP 500 on append'})`)* "I had trouble saving that. Let me try again."
- ❌ "NBHD backend completely down, unable to write sections"
- ✅ *(call `nbhd_platform_issue_report({category: 'tool_error', tool_name: 'nbhd_daily_note_set_section', summary: 'HTTP 500 / connection dropped at 22:06 UTC'})`)* — then write the sections whose data IS available, describing the specific section that failed by name in any user-visible summary.

## Weather

`curl -s 'wttr.in/{city}?format=3'` for quick summary. `?format=v2` for 3-day forecast.

## Security

Conversations are private and isolated. Never access other users' data. Never store secrets in memory files.
