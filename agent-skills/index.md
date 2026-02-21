# NBHD Managed Skills (MVP)

These skills are team-authored and deployed with the runtime image. They are synced to:

- `/home/node/.openclaw/workspace/skills/nbhd-managed/`

## Skills

1. `daily-journal`
2. `weekly-review`
3. `pkm-loop`

## Tool Contracts

- `nbhd_document_get` / `nbhd_document_put` / `nbhd_document_append`
- `nbhd_daily_note_get` / `nbhd_daily_note_set_section` / `nbhd_daily_note_append`
- `nbhd_memory_get` / `nbhd_memory_update`
- `nbhd_journal_context` / `nbhd_journal_search`
- `nbhd_platform_issue_report`

## Runtime Contracts

- Auth: `X-NBHD-Internal-Key` + `X-NBHD-Tenant-Id` (handled by plugin tools)
- API base route: `/api/v1/integrations/runtime/{tenant_id}/...`

## Safety

- Skills are authored by NBHD United only.
- No user-uploaded skills in MVP.
- Skills must not include shell execution or arbitrary command blocks.
