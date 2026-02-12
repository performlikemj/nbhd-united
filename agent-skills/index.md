# NBHD Managed Skills (MVP)

These skills are team-authored and deployed with the runtime image. They are synced to:

- `/home/node/.openclaw/workspace/skills/nbhd-managed/`

## Skills

1. `daily-journal`
2. `weekly-review`

## Tool Contracts

- `nbhd_journal_create_entry`
- `nbhd_journal_list_entries`
- `nbhd_journal_create_weekly_review`

## Runtime Contracts

- Auth: `X-NBHD-Internal-Key` + `X-NBHD-Tenant-Id` (handled by plugin tools)
- API base route: `/api/v1/integrations/runtime/{tenant_id}/...`

## Safety

- Skills are authored by NBHD United only.
- No user-uploaded skills in MVP.
- Skills must not include shell execution or arbitrary command blocks.
