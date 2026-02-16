# Journal API Reference

All endpoints are under the runtime base URL. Auth via `X-NBHD-Internal-Key` and `X-NBHD-Tenant-Id` headers.

## Daily Notes

### Get daily note
```
GET /api/v1/integrations/runtime/{tenant_id}/daily-note/?date=YYYY-MM-DD
```
Response:
```json
{
  "tenant_id": "uuid",
  "date": "2026-02-16",
  "template_id": "uuid",
  "template_slug": "default",
  "template_name": "Default",
  "sections": [{ "slug": "morning-report", "title": "Morning Report", "content": "*Agent update.*" }],
  "markdown": "# 2026-02-16\n\n..."
}
```
Returns `{"markdown": ""}` if no note exists for that date.

### Append to daily note
```
POST /api/v1/integrations/runtime/{tenant_id}/daily-note/append/
Content-Type: application/json

{ "content": "Checked Gmail — nothing urgent.", "date": "2026-02-16" }
```
- `date` is optional (defaults to today)
- Auto-creates the note if it doesn't exist
- Auto-timestamps with current time and `author=agent`
- Returns `201` with updated markdown

Target a specific section with `section_slug`:

```json
{
  "content": "Morning report from the agent.",
  "date": "2026-02-16",
  "section_slug": "morning-report"
}
```

If no `section_slug` is provided, behavior is equivalent to legacy log append.

Response:
```json
{
  "tenant_id": "uuid",
  "date": "2026-02-16",
  "sections": [{ "slug": "morning-report", "title": "Morning Report", "content": "*Agent update.*", "source": "shared" }],
  "markdown": "# 2026-02-16\n\n..."
}
```

## Long-Term Memory

### Get memory
```
GET /api/v1/integrations/runtime/{tenant_id}/long-term-memory/
```
Response:
```json
{ "tenant_id": "uuid", "markdown": "# Memory\n\n## Preferences\n..." }
```
Returns `{"markdown": ""}` if no memory exists.

### Update memory
```
PUT /api/v1/integrations/runtime/{tenant_id}/long-term-memory/
Content-Type: application/json

{ "markdown": "# Memory\n\n## Preferences\n- Prefers morning meetings\n..." }
```
Replaces the entire memory document. Returns `200` with updated markdown.

## Journal Context (Session Init)

### Get context
```
GET /api/v1/integrations/runtime/{tenant_id}/journal-context/?days=7
```
Returns recent daily notes + long-term memory in one call. Use this at session start.

Response:
```json
{
  "tenant_id": "uuid",
  "recent_notes": [
    { "date": "2026-02-16", "template_name": "Default", "markdown": "..." },
    { "date": "2026-02-15", "template_name": "Default", "markdown": "..." }
  ],
  "long_term_memory": "# Memory\n...",
  "recent_notes_count": 2,
  "days_back": 7
}
```
`days` param defaults to 7. Max 30.

## Tool compatibility

- `nbhd_journal_evening_checkin` appends directly to the `evening-check-in` section.
- For backward compatibility, the same tool exists in `nbhd-google-tools` as an alias.
