# Journal v2 Implementation Plan

## 1. Data Model

### New: `Document` model
```python
class Document(models.Model):
    tenant = ForeignKey(Tenant)
    kind = CharField(choices=[daily, weekly, monthly, goal, project, tasks, ideas, memory])
    slug = CharField(max_length=128)  # "2026-02-16", "2026-W07", "sautai", "goals", etc.
    title = CharField(max_length=256)
    markdown = TextField(default="")
    created_at = DateTimeField(auto_now_add=True)
    updated_at = DateTimeField(auto_now=True)
    
    unique_together = ["tenant", "kind", "slug"]
```

### Migration Strategy
1. Create Document table
2. Migrate DailyNote → Document(kind="daily", slug=date)
3. Migrate UserMemory → Document(kind="memory", slug="memory")
4. Migrate JournalEntry → append to daily documents as markdown
5. Migrate WeeklyReview → Document(kind="weekly", slug=week_start)
6. Keep old tables for now (soft deprecation)

## 2. Backend API

### User-facing (`/api/v1/journal/`)
- `GET /documents/?kind=daily` — list documents by kind
- `GET /documents/<kind>/<slug>/` — get or create single document
- `PATCH /documents/<kind>/<slug>/` — update markdown
- `POST /documents/<kind>/<slug>/append/` — append text (for quick log)
- `GET /tree/` — sidebar tree structure

### Runtime/agent-facing (existing pattern, updated)
- `GET /runtime/<tenant>/document/?kind=daily&slug=2026-02-16`
- `POST /runtime/<tenant>/document/append/`
- `PUT /runtime/<tenant>/document/` — create or replace
- `GET /runtime/<tenant>/journal-context/` — unchanged

## 3. Frontend Architecture

### Components
- `JournalLayout` — sidebar + content area
- `Sidebar` — file tree mirroring vault structure
- `DocumentView` — rendered markdown with edit toggle
- `MarkdownEditor` — textarea for editing
- `QuickLogInput` — retained, enhanced
- `DateNav` — date navigation for daily notes

### Pages
- `/journal/` → redirect to `/journal/today`
- `/journal/today` → daily note with date nav
- `/journal/daily/[date]` → specific daily note
- `/journal/goals` → Goals.md document
- `/journal/tasks` → Tasks.md document  
- `/journal/ideas` → Ideas.md document
- `/journal/weekly` → latest weekly review
- `/journal/projects` → project list
- `/journal/projects/[slug]` → specific project
- `/journal/memory` → memory document

### Key Behaviors
- Click doc in sidebar → rendered markdown
- Edit button → markdown textarea
- Save → PATCH API → back to rendered
- Checkbox click → toggle in markdown source → auto-save
- Quick log → POST append → re-render

## 4. Template System
Default templates stored as Python constants. When a new document is created, template markdown is used with variable substitution ({{date}}, {{yesterday}}, {{tomorrow}}).

## 5. Files to Create/Modify/Delete

### Create
- `apps/journal/models.py` — add Document model
- `apps/journal/migrations/0008_document.py` — schema + data migration
- `apps/journal/document_views.py` — new v2 views
- `apps/journal/document_serializers.py` — new serializers
- `apps/journal/templates_md.py` — default markdown templates
- `frontend/components/journal/sidebar.tsx`
- `frontend/components/journal/document-view.tsx`
- `frontend/components/journal/markdown-editor.tsx`
- `frontend/app/journal/[...slug]/page.tsx` — catch-all route

### Modify
- `apps/journal/urls.py` — add document routes
- `apps/integrations/urls.py` — add runtime document routes
- `apps/integrations/runtime_views.py` — add runtime document views
- `frontend/app/journal/layout.tsx` — sidebar layout
- `frontend/lib/api.ts` — document API functions
- `frontend/lib/queries.ts` — document query hooks
- `frontend/lib/types.ts` — document types
- `frontend/components/markdown-renderer.tsx` — checkbox support
- `runtime/openclaw/plugins/nbhd-journal-tools/index.js` — update tools

### Delete (later, not now)
- Old template management page can stay but be hidden

## 6. Order of Implementation
1. Document model + migration
2. Default templates
3. Backend API (user-facing + runtime)
4. Frontend types + API functions + queries
5. Sidebar component
6. Document view + markdown editor
7. Updated journal pages
8. Checkbox toggle in markdown
9. Agent tools plugin update
10. Testing + cleanup
