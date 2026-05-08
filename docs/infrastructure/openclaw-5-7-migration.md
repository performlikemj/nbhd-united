# OpenClaw 4.x → 5.7 Migration

**Status:** canary in flight on `oc-148ccf1c-ef13-47f8-a` as of 2026-05-08.
**Owner:** MJ (with Claude assistance).
**Goal:** migrate the entire OC fleet from 4.5 / 4.21 / 4.25 to 2026.5.7 to reclaim ~50s of cold-start time per tenant.

## Why this matters

Production benchmarking across 25 tenants over 30 days:

| Cohort | Median boot | p90 boot | Notes |
|---|---|---|---|
| OC ≤ 4.5 | 2.3s | 2.9s | Pre-bundled-runtime-deps architecture |
| OC ≥ 4.21 | 50.1s | 86.8s | Bundled-plugin runtime-deps install at every cold start |

OC 5.2 retired the bundled-plugin runtime-deps install path entirely. Verified on canary (revision `0000047`): gateway ready in **3.7s** under 5.7 (4997fc1 image). The architectural win is real — we just need the plugin compatibility surface right.

## Code changes shipped (chronological)

| PR | Branch | Lands |
|---|---|---|
| #481 | `fix/wake-stall-recovery-ack` | `wake_on_message` re-acks on stall recovery instead of staying silent. Surfaces in 5.7 deployment because cold-start retries are common during the fleet rollout transition. |
| #484 | `fix/openclaw-5.7-upgrade` | Bumps `Dockerfile.openclaw:OPENCLAW_VERSION` and `tool_policy.py:OPENCLAW_CURRENT_VERSION` to `2026.5.7`. Includes migration `0054_openclaw_version_5_7` for the field default. |
| #491 | `fix/openclaw-5.7-config-schema` | `config_generator.py` no longer emits `agents.defaults.llm` for OC ≥ 5.0; emits `models.providers.openrouter.timeoutSeconds: 300` instead. OC 5.2 retired the legacy key — gateway boot fails with `Unrecognized key: "llm"` if we keep emitting it. |
| #494 | `fix/openclaw-5.7-plugin-migration` | Adds `npm install @openclaw/line@${OPENCLAW_VERSION}` to Dockerfile (OC 5.2 externalized the LINE channel plugin) and emits `plugins.bundledDiscovery: "compat"` for OC ≥ 5.0 (preserves bundled-provider auto-discovery under 5.x's stricter allowlist mode). |

## Things that were investigated and ruled out

- **`activation.onStartup` plugin manifest requirement (4.27).** Confirmed planner-only metadata via `dist/plugin-sdk/src/plugins/manifest.d.ts:PluginManifestActivation`. Plugins without it load fine. **No nbhd-* plugin changes needed.**
- **CommonJS vs ESM.** `nbhd-image-gen` is CJS; the rest are ESM. `dist/module-export-*.js:unwrapDefaultModuleExport` handles both transparently. **No change needed.**
- **Other config schema migrations.** Audited `dist/legacy-config-migrations-*.js`. We don't emit the other retired keys (`routing.allowFrom`, `routing.groupChat.requireMention`, `agents.defaults.sandbox.perSession`, `agents.defaults.embeddedHarness`, `agents.defaults.agentRuntime.fallback`, `session.maintenance.rotateBytes`, `session.parentForkMaxTokens`, `messages.tts.enabled`, `tools.web.x_search.apiKey`, `threadBindings.spawnSubagentSessions/spawnAcpSessions`).
- **`bundled-plugin runtime-deps install` path.** Removed in OC 5.2. Our plugins use only Node built-ins (`fs`, `https`, `crypto`), so nothing breaks. Postinstall script `scripts/postinstall-bundled-plugins.mjs` PRUNES the legacy directory.
- **Plugin SDK API (`registerTool`, `on`, `logger`, `pluginConfig`, `runtime`).** All preserved in `dist/plugin-sdk/src/plugins/types.d.ts:OpenClawPluginApi`.

## Per-tenant rollout steps

### Prerequisites

1. Latest `main` deployed (post-#494 merge). CI sets `OPENCLAW_IMAGE_TAG=<merge-sha>` on Django.
2. New OC image present in ACR: `nbhdunited.azurecr.io/nbhd-openclaw:<merge-sha>`.

### Step 1: regenerate the tenant's `openclaw.json` on its file share

The tenant's existing `openclaw.json` on the Azure File Share (`ws-<tenant-prefix>` on storage account `stnbhdprod`) was written by the pre-#491 config generator and still contains `agents.defaults.llm`. OC 5.7 rejects it on boot with `Unrecognized key: "llm"`.

**Option A — automated (preferred for fleet):** trigger config push via `bump_pending_configs` + wait for `apply_pending_configs` cron. CI already runs `bump_all_pending_configs/` after deploy, so all tenants are marked dirty. The cron only picks up tenants with `last_message_at < now - 15min`. Active tenants need to go idle, OR you wait.

**Option B — explicit single-tenant:** run the management command:
```bash
python manage.py bump_openclaw_version \
  --oc-version 2026.5.7 \
  --tenant <uuid> \
  --image-tag <merge-sha>
```
This is atomic: sets `tenant.openclaw_version = 2026.5.7`, calls `update_tenant_config()` (which writes new `openclaw.json` to the file share), then calls `update_container_image()` (which creates a new revision on the new image and auto-activates it).

**Option C — emergency hand-edit (canary-only escape hatch):** if the Django pipeline isn't workable for some reason, edit the file share directly:
```bash
az storage file download --account-name stnbhdprod \
  --share-name ws-<tenant-prefix> --path openclaw.json \
  --dest /tmp/openclaw.json --auth-mode key
# strip agents.defaults.llm, add models.providers.openrouter.timeoutSeconds: 300,
# add plugins.bundledDiscovery: "compat"
az storage file upload --account-name stnbhdprod \
  --share-name ws-<tenant-prefix> --source /tmp/openclaw.json \
  --path openclaw.json --auth-mode key
az containerapp revision restart --name oc-<tenant-prefix> \
  --resource-group rg-nbhd-prod --revision <revision-name>
```
This is what we did for canary on 2026-05-08. Don't use for fleet — Option B is the supported path.

### Step 2: bump the container image

Atomic with Option B above. Otherwise:
```bash
az containerapp update --name oc-<tenant-prefix> \
  --resource-group rg-nbhd-prod \
  --image nbhdunited.azurecr.io/nbhd-openclaw:<merge-sha>
```

### Step 3: verify the canary

Watch for:
- `[gateway] http server listening (N plugins: ...; <X>s)` — should be ≤ 5s.
- No `[gateway] config warnings:` lines (especially no `plugin not installed: line`).
- A LINE/Telegram round-trip works end-to-end.
- One tool call succeeds (e.g. `nbhd_finance_summary`).

## Fleet rollout

After canary is clean for at least 30 minutes:

```bash
curl -X POST -H "X-Deploy-Secret: $DEPLOY_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"include_hibernated": true}' \
  "$DJANGO_URL/api/cron/rollout-byo-image-bump/"
```

This wraps `bump_all_tenant_images` which:
- Filters to active tenants with a real container.
- `--include-hibernated` extends to hibernated tenants too (per `apps/orchestrator/management/commands/bump_all_tenant_images.py:178`).
- ThreadPool with 5 workers, idempotent (skips tenants already on the target tag).
- Container Apps single-revision mode auto-activates the new revision and wakes hibernated containers.

**Important caveat:** `bump_all_tenant_images` only updates the IMAGE. The CONFIG push happens lazily via `apply_pending_configs` cron on idle tenants. To avoid the "container has new image but stale config" boot-failure window, do EITHER:

- (a) Run a fleet-wide config push first (`bump_all_pending_configs/` is already called by CI; wait for `apply_pending_configs` to drain, then bump images). This works but takes hours for active tenants to go idle.
- (b) Add a fleet-wide variant of `bump_openclaw_version --all` that does config + image atomically. **NOT IMPLEMENTED.** This is the right next step for proper fleet rollout safety. See follow-up below.

## Gotchas observed during canary work

1. **The "auto-bumper" can re-pull a broken image.** When PR #489 (`agenda-cross-domain-phase-c`) merged after our broken canary, the new OC image at `9b0cdc84` got auto-pushed to canary by an opportunistic-bump path (`apps/router/container_updates.py` triggered by per-message activity, OR `apply_pending_configs` cron). Mitigation during a known-bad image phase: pin Django's `OPENCLAW_IMAGE_TAG` env var to the last-known-good SHA via `az containerapp update --set-env-vars OPENCLAW_IMAGE_TAG=<known-good-sha>`. CI's deploy step will overwrite it on the next merge.
2. **File-share config staleness is invisible.** A bumped image with a stale config will fail at gateway boot with no easy way to detect ahead of time. Always pair an image bump with a config refresh.
3. **`@openclaw/line` peer-version mismatch.** If npm pulls a `@openclaw/line` minor version that doesn't match `OPENCLAW_VERSION`, the manifest's `minHostVersion` check rejects it with no log entry. **Always pin to `${OPENCLAW_VERSION}` exactly.** PR #494 does this.
4. **The 4.27 `activation.onStartup` deprecation warning.** Plugins without it currently load fine. If a future OC release removes the implicit fallback, our nbhd-* plugins go silent. **Watch the boot logs for `[plugins] X failed to activate` warnings**, especially after upgrading past 5.7.
5. **`bundledDiscovery: "compat"` is a transitional opt-out.** Upstream may eventually retire it. The proper long-term fix is to enumerate `anthropic`, `openrouter`, `memory-core`, `telegram` in `plugins.allow` explicitly (not just bundle them via auto-discovery). Tracked.
6. **Don't run `openclaw doctor --fix` on a bumped tenant.** OC 5.5/5.6/5.7 had multiple back-and-forth fixes for Codex OAuth route rewrites. Doctor `--fix` is non-interactive and could rewrite credentials we want kept. Manual verification only.

## Follow-ups (not in this rollout)

- [ ] **Atomic fleet bump endpoint.** Wrap `bump_openclaw_version --all` in a Django HTTP endpoint (mirroring `rollout-byo-image-bump`) so version + config + image roll together. Avoids the "image bumped, config stale" foot-gun on fleet-wide rollouts.
- [ ] **Plugin allowlist tightening.** Replace `bundledDiscovery: "compat"` with explicit `plugins.allow` entries for each bundled provider we use. Future-proofs against further allowlist tightening.
- [ ] **`@openclaw/line` config compat.** Verify `channels.line.allowFrom` and `channels.line.groups.*.requireMention` are still accepted by the externalized 5.7 LINE plugin. (Likely yes; verify post-canary.)

## Rollback plan

If the OC 5.7 rollout regresses on a tenant:

1. `az containerapp update --name oc-<tenant-prefix> --resource-group rg-nbhd-prod --image nbhdunited.azurecr.io/nbhd-openclaw:ebbccc88608f75c2b312cc31e4b05584ec67a300` (last-known-good 4.25 image).
2. Restore the prior `openclaw.json` from the file share's `.bak` file (OC writes one on each `Config overwrite`):
   ```bash
   az storage file copy start --source-account-name stnbhdprod \
     --source-share ws-<tenant-prefix> --source-path openclaw.json.bak \
     --destination-share ws-<tenant-prefix> --destination-path openclaw.json \
     --account-name stnbhdprod --auth-mode key
   ```
3. Restart the revision: `az containerapp revision restart`.
4. If the issue is fleet-wide, revert PR #494 (and possibly #491) on a hotfix branch, which makes CI rebuild the prior image.

For broader rollback, also pin Django's `OPENCLAW_IMAGE_TAG=ebbccc88...` to stop the auto-bumper from re-pulling the bad image until the rollback PR deploys.

## Verification checklist (post-fleet-rollout)

- [ ] All `oc-*` containers show `[gateway] http server listening (... ;Xs)` with X ≤ 5s in their post-bump logs.
- [ ] Boot-time fleet median dropped from ~50s to under 5s (re-run the variance benchmark).
- [ ] No `[gateway] config warnings:` entries.
- [ ] Sample LINE round-trip on at least 3 tenants — message in, reply out, no silent drops.
- [ ] Sample Telegram round-trip on at least 3 tenants.
- [ ] Cron schedules unaffected (5.5 cron `nextRunAtMs` repair touched persisted state — audit).
- [ ] No regression in BYO Anthropic / OpenRouter routing.

## File references

- `Dockerfile.openclaw:25-33` — OC + LINE install
- `apps/orchestrator/config_generator.py:1199-1226` — plugin block, `bundledDiscovery` migration
- `apps/orchestrator/config_generator.py:1259-1300` — provider blocks, `models.providers.openrouter.timeoutSeconds` migration
- `apps/orchestrator/management/commands/bump_openclaw_version.py` — single-tenant atomic bump
- `apps/orchestrator/management/commands/bump_all_tenant_images.py` — fleet image bump (config-stale unsafe)
- `apps/cron/views.py:1479-1540` — `rollout_byo_image_bump` endpoint wrapping the above
- `apps/orchestrator/services.py:351` — `update_tenant_config()` (the canonical config-push)
- Upstream verification: `/tmp/openclaw-latest/package/dist/discovery-CVL9-KJt.js:1339` (`resolvePackageExtensionEntries`), `dist/legacy-config-migrations-DMwlWTca.js:851` (`bundledDiscovery` migration), `dist/official-external-plugin-catalog-*.js:222` (`@openclaw/line` install spec), `dist/plugin-sdk/src/plugins/manifest.d.ts:PluginManifestActivation` (planner-only).
