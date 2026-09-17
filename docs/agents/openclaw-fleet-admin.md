# OpenClaw fleet admin — settings, the one rule, and the one button

Read this before changing anything about how tenant OpenClaw containers are built or run. It exists because the 2026.9.4 runtime only boots on a container that carries the right storage layout, and a "small" edit on the Azure portal breaks a tenant silently — on its *next* restart, hours or days later.

## The one rule

**The code is the source of truth. Azure is a cache of it. Never hand-edit a tenant Container App template (`oc-*`) on the portal or with `az containerapp update`.**

Change the desired state in code (`apps/orchestrator/azure_client.py`), deploy, then converge the fleet with the one button. Every write is a new revision, i.e. a container restart, so the command defers a tenant whose cron is mid-flight or imminent (same guard as the hourly cron) and never writes to a hibernated tenant:

```bash
python manage.py ensure_openclaw_ready --all --dry-run   # see per-field drift, write nothing
python manage.py ensure_openclaw_ready --all             # converge; a second run is a clean no-op
```

Anything you set by hand on an `oc-*` app is drift. The daily drift check will page you about it, and the next reconcile or image bump will overwrite it. The *Django* app (`nbhd-django-westus2`) is different: its env vars ARE the settings knobs below and are meant to be set with `az containerapp update --set-env-vars` (the CI deploy exports the live definition and preserves them).

## What "OpenClaw ready" means (the desired state)

All constants live in `apps/orchestrator/azure_client.py` and are the same ones `create_container_app` provisions with. `ensure_openclaw_ready` and `detect_openclaw_drift` compare against exactly these — there is no second list.

| Field | Safe value | Why |
|---|---|---|
| `workspace` AzureFile `mountOptions` | `_WORKSPACE_MOUNT_OPTIONS` = `uid=1000,gid=1000,dir_mode=0700,file_mode=0600,mfsymlinks,nobrl,cache=none,serverino` | 9.4's fs-safe layer requires node-owned 0700 dirs; the default root-owned 0755 SMB mount throws `FsSafeError` at boot |
| `oc-state` volume | `EmptyDir` | 9.4 reads its SQLite (state/flows/tasks/plugin-state) through a read-only worker that fails on SMB |
| `oc-state` mount on the `openclaw` container | `/home/node/oc-state` | same |
| env `OPENCLAW_STATE_DIR` | `/home/node/oc-state` | moves all runtime SQLite to local ephemeral disk |
| env `OPENCLAW_CONFIG_PATH` | `/home/node/.openclaw/openclaw.json` | pins the config back to the share (durable) |
| env `OPENCLAW_WORKSPACE_DIR` | `/home/node/.openclaw/workspace` | pins memory/workspace back to the share (durable) |
| env `XDG_CACHE_HOME` | `/home/node/oc-state/cache` | keeps caches off SMB |
| `openclaw` container image | `nbhdunited.azurecr.io/nbhd-openclaw:<OPENCLAW_IMAGE_TAG>` | **gated** — see rollout below |

The storage/env rows are reconciled regardless of the image gate. 2026.5.28 honors the relocation env too, so on a 5.28 tenant this moves its runtime state (sessions, cron store, identity) to ephemeral disk exactly as on 9.4. Losing `oc-state` is by design: durable truth is the share (config + workspace/memory) and Postgres (crons re-seed, transcripts). Nothing here touches tenant data.

## Settings and env vars (Django app `nbhd-django-westus2`)

Names in `config/settings/production.py` MUST match the Container App env var names.

| Setting / env var | Default | What it does |
|---|---|---|
| `OPENCLAW_IMAGE_TAG` | set by CI on every deploy (`<oc-version>-<7char-sha>`) | The fleet's desired image tag. Never hand-set; `docs/agents/workflow.md` covers the emergency re-pin. |
| `OPENCLAW_IMAGE_ROLLOUT_TENANT_IDS` | `""` = **nobody** | Allowlist for the *automatic* roll onto `OPENCLAW_IMAGE_TAG` (hourly `apply-pending-configs`, wake refresh, and `ensure_openclaw_ready`). Comma-separated tenant UUIDs, or `*`. |
| `OPENCLAW_DRIFT_ALERTS_ENABLED` | `false` | Turns the daily `detect-openclaw-drift` cron from a 200 no-op into a real sweep + Pushover alert. |
| `PUSHOVER_API_TOKEN` / `PUSHOVER_USER_KEY` | already set in prod | Where health and drift alerts go. Shared with `run-health-check`. |
| `AZURE_RESOURCE_GROUP` / `AZURE_ACR_SERVER` | `rg-nbhd-prod` / `nbhdunited.azurecr.io` | Where the `oc-*` apps and images live. |
| `AZURE_MOCK` | unset | `true` makes every Azure call a logged no-op; both commands print `[MOCK]` and stop. |
| `OPENCLAW_*_PLUGIN_ID` / `_PATH` | see `config/settings/base.py` | Which bundled plugins the generated `openclaw.json` enables. Empty ID = plugin off (silently — see the `OPENCLAW_AUTOMATION_PLUGIN_ID` note in base.py). |

Code constant, not a setting: `OPENCLAW_CURRENT_VERSION` in `apps/orchestrator/tool_policy.py` (`2026.9.4`) is what bare/`latest` tags map to; `Tenant.openclaw_version` selects the config schema and must move with the image (every image path does this via `openclaw_version_for_image_tag`).

Setting a knob:

```bash
az containerapp update --name nbhd-django-westus2 --resource-group rg-nbhd-prod \
  --set-env-vars OPENCLAW_IMAGE_ROLLOUT_TENANT_IDS=<uuid>
```

Verify with `az containerapp show ... --query "properties.template.containers[0].env[?name=='OPENCLAW_IMAGE_ROLLOUT_TENANT_IDS']"`.

## Staged image rollout (canary → soak → `*`)

A deploy bumps `OPENCLAW_IMAGE_TAG` on every merge, but with an empty allowlist **nobody moves**. To roll a schema- or storage-crossing image:

1. **Converge storage first**: `ensure_openclaw_ready --all` (storage/env only is fine: `--no-image`). 9.4 will not boot without it, and `update_container_image` now bakes the storage state into every image bump as a backstop.
2. **Canary**: set `OPENCLAW_IMAGE_ROLLOUT_TENANT_IDS=<your tenant uuid>`. Either wait for the hourly cron (idle tenants only) or run `ensure_openclaw_ready --tenant <uuid>` — it rolls through the same `apply_single_tenant_image_task` path (cron snapshot → image → version → config → cron restore), bakes the storage state into that same revision (one restart, not two), and defers if a tenant cron is mid-flight or imminent.
3. **Soak**: send a message, fire a cron, check `run-health-check` stays quiet. Widen the list to a few more UUIDs and repeat.
4. **Fleet**: set `*`. The hourly cron finishes the roll; `ensure_openclaw_ready --all` does it now.
5. Tenants not on the allowlist show as `image: stale-gated` in both commands. That is expected and never alerts.

For a one-off custom image on a single tenant without touching the DB tag, `canary_tenant_image` still exists (`docs/runbooks/canary.md`). It creates a `db-mismatch` drift that pages daily while alerts are on, and with an empty allowlist nothing reverts it automatically. When the canary is done, re-pin with `bump_openclaw_version` (or allowlist the tenant so the hourly cron rolls it back onto `OPENCLAW_IMAGE_TAG`).

## Onboarding and repairing a tenant

- **New tenant**: provisioning (`create_container_app`) already emits the full desired state. Nothing to do.
- **Tenant missing container metadata** (`container_id` empty, stuck in provisioning): `repair_tenant_provisioning --tenant-id <uuid>`.
- **Tenant on an old template / hand-edited / "gateway refuses to boot" after a restart**: `ensure_openclaw_ready --tenant <uuid>`. Output lists every drifted field (`drift:` or `UNFIXABLE:` — the latter means the workspace share volume itself is missing; re-provision), then the tenant verdict: `fixed N field(s)`, `would fix N` (dry run), `ROLLED`, or `DEFERRED`.
- **Tenant is hibernated**: `--all` skips it and `--tenant` refuses it, so a template write never wakes one. The wake path's image refresh converges it on next wake (allowlist permitting), or wake it first and re-run.
- **`DEFERRED (cron_in_flight / cron_imminent / cron_state_unknown)`**: a tenant cron would be killed by the restart. Nothing was written; re-run in a few minutes.
- **`image: DB/LIVE MISMATCH`**: someone changed the image outside the code paths. Decide which is right, then re-pin with `bump_openclaw_version --tenant <uuid> --oc-version <v> --image-tag <tag>`.
- Legacy `ensure_oc_state_dir_mount` still works but reports only changed/unchanged; prefer the new command.

## Drift alerts

- `detect_openclaw_drift --all` is READ-ONLY: it reads every active, non-hibernated tenant's live template and compares it to the table above. `--json` for machine output, `--no-alert` to only print, `--tenant <uuid>` for one.
- The same sweep runs daily at 08:40 UTC via the `detect-openclaw-drift` QStash cron (`/api/cron/detect-openclaw-drift/`, QStash-signed or `X-Deploy-Secret`). It is registered on every deploy but **does nothing until `OPENCLAW_DRIFT_ALERTS_ENABLED=true`**. Flip it on after the fleet converges; flip it off to silence during a planned migration.
- One consolidated Pushover message per sweep (same channel and priority as health alerts), max 10 tenants listed, field names only — never env values, never SDK error bodies.
- **Alerts**: any storage/env row drifted, `Tenant.container_image_tag` ≠ the tag Azure runs, or a template that could not be read (a deleted `oc-*` app is the worst drift).
- **Never alerts**: a tenant simply not on `OPENCLAW_IMAGE_TAG` yet (`stale-gated` / `stale-allowed`). Staged rollouts are supposed to look like that.
- Fix is always the one button. If an alert repeats after a reconcile, the field is `UNFIXABLE` or something keeps rewriting it — find the writer, do not hand-patch Azure.

Code map: `apps/orchestrator/openclaw_drift.py` (comparison + alert text), `azure_client.ensure_openclaw_storage_ready` (per-field reconcile), `management/commands/ensure_openclaw_ready.py`, `management/commands/detect_openclaw_drift.py`, `apps/cron/views.py::detect_openclaw_drift`, `apps/orchestrator/image_rollout.py` (the gate).
