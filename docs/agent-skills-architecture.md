# Agent Skills Architecture (Harness-Aligned MVP)

## Scope

This MVP adds tenant-safe journaling skills to the existing OpenClaw runtime stack:

- `daily-journal`
- `weekly-review`

It includes backend persistence APIs, runtime plugin tools, and managed skill delivery into each tenant workspace.

Out of scope for this phase:

- Frontend journal/review UI
- User-authored skill uploads
- Auth contract migration away from internal header auth

## Runtime Contracts

### Auth

Runtime-to-control-plane requests use:

- `X-NBHD-Internal-Key`
- `X-NBHD-Tenant-Id`

The runtime plugin sets these headers; skills invoke tools and do not handcraft auth.

### API Base Route

All runtime skill endpoints are under:

- `/api/v1/integrations/runtime/{tenant_id}/...`

### New Endpoints

1. `POST /api/v1/integrations/runtime/{tenant_id}/journal-entries/`
2. `GET /api/v1/integrations/runtime/{tenant_id}/journal-entries/?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD`
3. `POST /api/v1/integrations/runtime/{tenant_id}/weekly-reviews/`

Validation highlights:

- Journal date window requires both `date_from` and `date_to` together.
- `date_from <= date_to`.
- Date range max is 31 days.
- String-list fields are capped at 10 items.
- Enums:
  - `energy`: `low|medium|high`
  - `week_rating`: `thumbs-up|thumbs-down|meh`

## Persistence Models

Implemented in `apps/journal/`:

- `JournalEntry`
- `WeeklyReview`

All rows are tenant-scoped via FK to `tenants.Tenant`.

## Plugin Tools

Implemented in `runtime/openclaw/plugins/nbhd-google-tools/index.js`:

- `nbhd_journal_create_entry`
- `nbhd_journal_list_entries`
- `nbhd_journal_create_weekly_review`

The plugin transport now supports both:

- `GET` query requests
- `POST` JSON requests

Existing Google read-only tools remain unchanged in contract and behavior.

## Skill Packaging and Workspace Sync

Source-managed skill files live in:

- `agent-skills/daily-journal/SKILL.md`
- `agent-skills/weekly-review/SKILL.md`
- `agent-skills/index.md`

Runtime image includes these files under:

- `/opt/nbhd/agent-skills/`

On startup, the runtime entrypoint syncs managed skills into mounted workspace path:

- `/home/node/.openclaw/workspace/skills/nbhd-managed/`

The runtime also syncs managed assistant guidance:

- `/opt/nbhd/templates/openclaw/AGENTS.md` -> `/home/node/.openclaw/workspace/AGENTS.md`

Only `skills/nbhd-managed` and managed `AGENTS.md` are touched; other workspace content is left intact.

## Security and Governance

- Skills are authored by NBHD United maintainers only.
- No user-uploaded skills are supported in MVP.
- Runtime APIs remain tenant-bound through auth header checks and tenant-id matching.

## Rollout Runbook

1. Deploy backend with journal models and runtime endpoints.
2. Deploy `nbhd-openclaw` runtime image with updated plugin and managed skills.
3. Restart one pilot tenant runtime container.
4. Smoke-test:
   - `daily-journal` flow saves a journal entry.
   - `weekly-review` can read entries and save a review.
5. Observe logs and DB writes for 24 hours.
6. Roll out broadly.

## Failure Handling

### Runtime endpoint failures

- Plugin returns structured error message with runtime status, error code, and detail.
- Agent should apologize briefly, retry once, then continue without data loss promises.

### Malformed payloads

- Backend returns `400` with field-level validation errors.
- Agent should correct payload by re-collecting only missing/invalid fields.

### Skill sync verification

After container restart, verify:

- `skills/nbhd-managed/daily-journal/SKILL.md` exists.
- `skills/nbhd-managed/weekly-review/SKILL.md` exists.
- `workspace/AGENTS.md` includes managed-skill routing section.
