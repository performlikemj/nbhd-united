# CONTINUITY — Journal shaping (assistant-editable journal template), gated pilot

## Why (context for reviewers)

User feedback: the journal ships with founder-authored default templates and founder-authored
scheduled-task presets — "it's my template and my shared tasks," nothing derived from the user.
The NBHD-native fix is NOT a template-editor UI: the tenant's assistant becomes the
personalization engine. The user says "track my mood each evening" in chat; the assistant
reshapes the daily-note template (NEW capability, this PR) and pairs it with a check-in
schedule (EXISTING capability — the fleet-wide `cron` tool already has full authority over
all of a tenant's scheduled tasks, including preset-born ones; verified in recon).

Pilot scope: capability is flag-gated per tenant (`journal_shaping_enabled`, default False).
It will be enabled for exactly two consenting test tenants. Flag-off tenants must be
byte-identical to today — that parity is tested, not assumed.

Anchor: origin/main @ 9090d529. All file/line references below verified against that sha.

## Design decisions (already made — do not relitigate)

1. **New OC plugin** `runtime/openclaw/plugins/nbhd-journal-shaping/` with exactly two tools:
   `nbhd_journal_template_get`, `nbhd_journal_template_update`. Clone the shape of
   `runtime/openclaw/plugins/nbhd-document-keep/` (openclaw.plugin.json + index.js):
   `configSchema` with `journalShapingEnabled: boolean` (`additionalProperties: false`),
   `register(api)` fail-closed first line (`if (cfg.journalShapingEnabled !== true) return;`),
   callbacks into Django via the same `callRuntime()`/`tenantPath()` idiom with
   `X-NBHD-Internal-Key` + `X-NBHD-Tenant-Id` headers.
2. **Belt AND suspenders gating** — stricter than document-keep (which has no server-side
   check; recon finding): the new runtime views MUST check `tenant.journal_shaping_enabled`
   and return 403 when off (clone the friends-tools "runtime endpoints 403 independently"
   stance), IN ADDITION to config-level absence (plugin not emitted, gate block not emitted).
3. **Tools operate on the tenant's DEFAULT NoteTemplate only.** No slug/name/is_default
   management exposed. Get returns the default template; update replaces its `sections`.
4. **Reuse existing validation/persistence** — delegate to `apps/journal/services.py`
   `_validate_template_sections()` + the same persistence semantics as
   `NoteTemplateSerializer.update()` (apps/journal/serializers.py:378) rather than
   reimplementing. Use `get_default_template()` / seed-on-demand
   (`seed_default_templates_for_tenant` / `get_or_seed_note_template`) so a tenant that has
   never opened the journal still works.
5. **Additional caps at the TOOL surface only** (do not change console/legacy validation):
   max 12 sections; section slug ≤64 chars; title ≤120 chars; content ≤4000 chars;
   serialized sections payload ≤20KB. Clear ValidationError messages (the assistant reads
   them and self-corrects).
6. **Config push on write**: after a successful update, fire
   `publish_task("update_tenant_config", str(tenant.id))` in the same best-effort
   try/except-pass shape as `TemplateDetailView.patch` (apps/journal/views.py:526-533).
   (This also refreshes the agent-facing `templates.md` skill reference automatically via
   `render_templates_md`.)
7. **Prompt-side delivery clones tour-guide, not rules-files**: a lean AGENTS.md gate block
   (≤700 chars, tested) appended in `render_workspace_files()` BEFORE the Gravity block
   (apps/orchestrator/personas.py — insert after the tour-guide block at ~line 840-848,
   before finance/Gravity at ~850), gated on the flag; plus a full behavioral doc uploaded
   ONLY for flagged tenants to `workspace/docs/journal-shaping.md` via the
   `file_map_overwrite` mechanism in `update_tenant_config()`
   (apps/orchestrator/services.py:723-729 tour-guide precedent). Register the doc template in
   `_WORKSPACE_DOCS` (personas.py:565-572). Do NOT add a row to the static rules table in
   `templates/openclaw/AGENTS.md` (that would cost every tenant bytes) and do NOT create a
   fleet-wide rules file. Flag-off tenants: zero new bytes anywhere.
8. **Migration** `apps/tenants/migrations/0132_tenant_journal_shaping_enabled.py` — single
   `AddField` BooleanField(default=False) on Tenant. No new table → NO relock migration.
9. **Management command** `apps/tenants/management/commands/set_journal_shaping.py` — clone
   `set_tour_guide.py` exactly (minus mode): `--tenant-id`, `--enable/--disable`,
   `bump_pending_config()`, print the `force_apply_configs` follow-up hint.
10. **Dockerfile.openclaw**: add the plugin COPY line exactly like nbhd-document-keep's
    (the CI guard requires config emission + COPY to pair).

## Files to create/modify (complete list)

CREATE:
- `runtime/openclaw/plugins/nbhd-journal-shaping/openclaw.plugin.json`
- `runtime/openclaw/plugins/nbhd-journal-shaping/index.js`
- `templates/openclaw/docs/journal-shaping.md`  (content provided below — use verbatim)
- `apps/tenants/migrations/0132_tenant_journal_shaping_enabled.py`
- `apps/tenants/management/commands/set_journal_shaping.py`
- `apps/integrations/test_journal_shaping_views.py`
- `apps/orchestrator/test_journal_shaping_directive.py`

MODIFY:
- `apps/tenants/models.py` — add `journal_shaping_enabled = models.BooleanField(default=False)`
  next to `tour_guide_enabled`.
- `apps/integrations/runtime_views.py` — two new views (clone document-keep view idiom:
  `permission_classes=[AllowAny]`, `authentication_classes=[]`, `_internal_auth_or_401`
  first line, `_load_tenant_or_404`, THEN the explicit flag→403 check).
- `apps/integrations/urls.py` — wire `runtime/<tenant_id>/journal/template/` (GET) and
  `runtime/<tenant_id>/journal/template/update/` (POST), following lines 258-269 idiom.
- `apps/orchestrator/config_generator.py` — plugin emission gated on flag (clone the
  document-keep block at ~line 2004 and the entries-config at ~2288:
  `{"journalShapingEnabled": True}`).
- `apps/orchestrator/personas.py` — `_WORKSPACE_DOCS` entry + gate block (constant near the
  tour-guide gate; text provided below, use verbatim) appended before Gravity, flag-gated.
- `apps/orchestrator/services.py` — `file_map_overwrite` conditional upload of
  `workspace/docs/journal-shaping.md` when flag on (clone tour-guide lines 723-729).
- `Dockerfile.openclaw` — COPY line for the new plugin.
- The existing worst-case AGENTS.md budget test (in
  `apps/orchestrator/test_document_ingestion_directive.py`,
  `FinanceTenantBudgetTest.test_finance_friends_propose_doc_render_fits_under_cap_no_truncation`
  or its current name) — ADD `journal_shaping_enabled=True` to the combined worst-case
  tenant so the all-flags render is proven under `BOOTSTRAP_MAX_CHARS` (import the cap,
  never re-pin it locally — the 2026-07-11 canary truncation lesson).

Do NOT touch anything else. Explicitly out of scope: apps/cron (no changes), frontend/,
iOS, seeded cron defaults, `Document.Kind` enum, legacy `TemplateListCreateView`/
`TemplateDetailView` behavior, .github/workflows, requirements files.

## AGENTS.md gate block (use this text verbatim; keep ≤700 chars)

```
## Journal shaping

This user can reshape their journal template through you.
- `nbhd_journal_template_get` — read the current daily-note sections.
- `nbhd_journal_template_update` — replace the sections list.
- Before ANY reshape: read `docs/journal-shaping.md`, then propose the exact sections and get explicit agreement. Never reshape silently.
- Template = future structure only; existing notes are never modified by a template change.
- Pair every section change with its check-in schedule: prefer folding into an existing check-in over creating new ones.
```

## workspace/docs/journal-shaping.md (use verbatim)

```
# Journal shaping — making this journal theirs

This tenant has journal shaping enabled. You can reshape the user's daily-note
template so their journal captures what THEY care about — mood, sleep, gratitude,
training, anything they ask for. Founder defaults are scaffolding, not fixtures:
the user may keep, reword, or drop any of them.

## The pairing model

A ritual has two halves. The template section is the WHAT (where it lands in the
journal). The scheduled check-in is the WHEN (when you come to ask). Whenever you
change one half, consider the other in the same conversation:
- Add a "Mood" section → offer an evening check-in that asks about mood and writes
  the answer into that section.
- Asked to change or drop a check-in → offer to adjust the template section it fed.

Use your existing scheduling capability for the WHEN half. Prefer FOLDING questions
into an existing check-in (one evening visit can fill mood + sleep + gratitude)
over creating new scheduled tasks — the user has a 10-task limit, and several
founder-seeded tasks may already exist. Reshaping or retiming an existing check-in
(e.g. "Evening Check-in") is almost always better than stacking a new one.

## Etiquette (non-negotiable)

1. Propose, then write. Show the exact section list you intend to set (titles, one
   line each) and get an explicit yes before calling `nbhd_journal_template_update`.
2. Never reshape silently or bundle a template change into an unrelated action.
3. Template changes shape FUTURE daily notes only. Never present a template edit as
   affecting past entries, and never delete captured journal content as part of
   shaping.
4. When the user asks to drop a founder default (a section or a seeded check-in),
   confirm once, then do it — their journal, their call.
5. All scheduling uses the user's own timezone.

## Mechanics

- `nbhd_journal_template_get` returns the default template: name and sections
  (`slug`, `title`, `content`, `source`). `content` is the seed text under each
  heading in a fresh daily note; keep it short or empty.
- `nbhd_journal_template_update` REPLACES the whole sections list — always get
  first, modify, then update. Limits: ≤12 sections, slug ≤64 chars, title ≤120,
  content ≤4000. Duplicate slugs are rejected. On a validation error, adjust and
  retry; on repeated failure, tell the user honestly.
- Mark sections you create at the user's request as `source: "human"`; sections you
  propose yourself as `source: "agent"`.
- Tomorrow's daily note materializes from the updated template. Say so plainly:
  "you'll see this starting with tomorrow's note."
```

## Required tests (all must pass; clone the named precedents)

apps/orchestrator/test_journal_shaping_directive.py (clone test_tour_guide_directive.py):
- flag-off tenant: AGENTS.md contains neither "## Journal shaping" nor either tool name
  (fleet parity), and openclaw config plugins load/entries contain no nbhd-journal-shaping.
- flag-on tenant: gate present before the Gravity/Observation block when finance also on;
  both tool names present; plugin emitted with `{"journalShapingEnabled": True}` entry.
- gate block length ≤700 chars.
- doc registered: flag-on `update_tenant_config` file map includes
  `workspace/docs/journal-shaping.md`; flag-off does not (mock uploads like the tour-guide
  services test does).
- extended worst-case budget test stays under imported BOOTSTRAP_MAX_CHARS.

apps/integrations/test_journal_shaping_views.py (clone test_document_keep_views.py auth
harness):
- missing/wrong internal key → 401; tenant-id mismatch → 401/403 (match existing idiom).
- flag OFF → 403 on both endpoints (this is the belt-and-suspenders test document-keep lacks).
- GET on a tenant with no template rows → seeds and returns the default (5 seeded sections).
- POST valid replacement → 200, DB row updated, sections round-trip exactly; asserts
  `publish_task` called with ("update_tenant_config", tenant_id) — mock it.
- POST rejections: empty list, dup slugs, >12 sections, oversize title/content, non-list
  payload → 400 with message; DB unchanged; publish_task NOT called.

## Gates before handoff (run yourself, report ✔/✘ lines)

- `env -u OPENAI_API_KEY /Users/michaeljones/Projects/nbhd-united/.venv/bin/python manage.py test apps.orchestrator apps.integrations apps.journal apps.tenants --noinput`
  (run from THIS worktree). Known local landmines, neither caused by you: a teardown-flush
  deadlock hits ~40% of local full runs — re-run once before concluding failure; a
  migration-0058 UndefinedColumn error on fresh local runs is pre-existing on clean main.
- `/Users/michaeljones/Projects/nbhd-united/.venv/bin/ruff format <every python file you touched>` then
  `/Users/michaeljones/Projects/nbhd-united/.venv/bin/ruff check <same files>`.
- `env -u OPENAI_API_KEY /Users/michaeljones/Projects/nbhd-united/.venv/bin/python manage.py makemigrations --check --dry-run` must be clean.

## Commit discipline

- Commits in this worktree; branch `feat/journal-shaping` already exists — stay on it.
- Stage files BY PATH (`git add <path> ...`). NEVER `git add -A` or `git add .` (hook-blocked).
- Logical commits with conventional prefixes, e.g.:
  1. `feat(tenants): journal_shaping_enabled flag + set_journal_shaping command`
  2. `feat(integrations): runtime journal template get/update views (internal-auth + flag 403)`
  3. `feat(openclaw): nbhd-journal-shaping plugin + Dockerfile COPY`
  4. `feat(orchestrator): flag-gated AGENTS.md gate + journal-shaping doc emission`
  5. `test(journal-shaping): directive parity + runtime view suites`
  Include CONTINUITY_journal_shaping.md in the first commit.
- No --no-verify. Never print secrets or dump env vars.

## Implementation state

- Parent: `CONTINUITY.md`
- Root: `CONTINUITY.md`
- Related: document-keep runtime/plugin precedents; tour-guide gate/doc/command precedents
- Owner: `/root`
- Done: all directive files implemented; focused 15-test suite and full 2,247-test gate pass.
- Done: ruff format/check pass for all 11 touched Python files; migration drift check reports no changes.
- Now: final commit and clean-tree audit.
- Next: handoff summary.
- Open questions: none.
