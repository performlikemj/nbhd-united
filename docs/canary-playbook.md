# Canary Playbook — Single-Tenant Pre-Merge Validation

Procedure for validating risky changes on one tenant (typically the admin's
personal tenant) before CI/CD rolls them out fleet-wide. Pairs with the
harness framework (`config_validator.py`, `config_security.py`,
`openclaw_config_doctor_smoke.sh`) — those catch config-schema drift; the
canary catches runtime-behavior drift that only shows up under real use.

## When to canary

Required for:

- OpenClaw version bumps (`Dockerfile.openclaw` `ARG OPENCLAW_VERSION`)
- Plugin SDK changes or new plugins under `runtime/openclaw/plugins/`
- Tool-policy changes in `apps/orchestrator/tool_policy.py`
- Cron-generator or config-generator changes that affect all tenants
- Any change that touched `runtime/openclaw/entrypoint.sh` or
  `runtime/openclaw/suppress-chmod-eperm.js`

Not required for:

- Django-only changes with tests
- Frontend changes (`frontend/` static export deploys independently)
- Docs, dependency bumps that don't touch the runtime image

## Canary tenant

**`oc-148ccf1c-ef13-47f8-a`** — admin's personal tenant, used for
dogfooding. Failures here reach only one human.

## Procedure

### 1. Open the PR first

CI runs tests/lint/validator on every branch, but deploy is gated on
`github.ref == 'refs/heads/main'` (`.github/workflows/ci-cd.yml:211`).
A branch push alone doesn't touch production. Confirm CI is green on the
PR before proceeding.

### 2. Build the canary image

From the PR branch, build against the live ACR so we skip local Docker
setup and match the CI build environment exactly:

```bash
SHORT_SHA=$(git rev-parse --short HEAD)
CANARY_TAG="canary-${SHORT_SHA}"

az acr build \
  --registry nbhdunited \
  --image nbhd-openclaw:${CANARY_TAG} \
  --file Dockerfile.openclaw \
  .
```

The tag prefix `canary-` is important: it doesn't collide with CI-produced
`<sha>` or `latest` tags, so nothing else in the fleet picks it up by
accident.

### 3. Deploy to the canary tenant only

Call the orchestrator's existing single-tenant image update path
(`apps.orchestrator.azure_client.update_container_image`) via Django shell
against production Django. This is the same function
`apply_single_tenant_image_task` uses during fleet rollouts — we're just
invoking it for one tenant, with a custom tag.

```bash
CANARY_CONTAINER="oc-148ccf1c-ef13-47f8-a"
CANARY_IMAGE="nbhdunited.azurecr.io/nbhd-openclaw:${CANARY_TAG}"

az containerapp exec \
  --name nbhd-django-westus2 \
  --resource-group rg-nbhd-prod \
  --command "python manage.py shell -c \"
from apps.orchestrator.azure_client import update_container_image
update_container_image('${CANARY_CONTAINER}', '${CANARY_IMAGE}')
\""
```

Alternatively, directly via `az` (bypasses Django, keeps the DB's
`container_image_tag` in sync with the old value — only use if Django is
unreachable):

```bash
az containerapp update \
  --name ${CANARY_CONTAINER} \
  --resource-group rg-nbhd-prod \
  --image ${CANARY_IMAGE} \
  --revision-suffix u$(echo ${CANARY_TAG} | sha256sum | cut -c1-6)
```

### 4. Watch startup

```bash
az containerapp logs show \
  --name ${CANARY_CONTAINER} \
  --resource-group rg-nbhd-prod \
  --tail 200 \
  --follow
```

**Red flags:**

- `chmod EPERM` loop — the suppress-chmod-eperm.js `--require` hook failed.
  Likely the OpenClaw plugin SDK export moved/renamed
  (see `runtime/openclaw/suppress-chmod-eperm.js:41-43` —
  `registerUnhandledRejectionHandler`).
- `Config file missing or invalid` — the config schema changed and the
  tenant's stored `openclaw.json` has a key the new version rejects. Last
  seen in the 2026.4.5 bump (`b39e2a1`, LINE `capabilities` key).
- `CIAO ANNOUNCEMENT CANCELLED` — mDNS isn't disabled; make sure
  `OPENCLAW_DISABLE_BONJOUR=1` is still being set.
- Silent crash after ~5s — the gateway process exited without a clear
  error; usually a missing env var or an unavailable plugin path.

### 5. Real-usage verification

The smoke test proves startup; the canary proves behavior. Run exercises
that touch the surface area the change affects. For OpenClaw version
bumps, at minimum:

- **Cross-session recall** — start a new session, reference something
  from a session >24h old (e.g., a goal, a lesson, a journal entry).
  Does the agent recall it naturally, or does it need to explicitly
  search? (This is where the built-in memory engine's silent
  pre-compaction flush should help.)
- **Constellation flow** — propose a lesson, approve it. Does the agent
  cross-reference existing lessons via vector search? Does the approval
  write back to the journal DB cleanly?
- **Horizon pulse** — trigger a weekly reflection cron manually. Does
  the reflection reference prior weeks accurately?
- **Long-session compaction** — have a long conversation that forces
  compaction. Does context survive? (Silent pre-compaction memory flush.)
- **Plugin tools** — exercise one tool from each registered plugin:
  `nbhd_journal_*`, `nbhd_google_*`, `nbhd_finance_*`, `nbhd_reddit_*`,
  `nbhd_image_gen`, `nbhd_send_to_user`.

### 6. Health gate

From the admin's personal OpenClaw (`agent.bywayofmj.com`) or directly:

```bash
curl -sf \
  -H "X-Deploy-Secret: ${DEPLOY_SECRET}" \
  "https://nbhd-django-westus2.victoriousocean-5cdd2683.westus2.azurecontainerapps.io/api/v1/cron/admin-health/" \
  | jq '.tenants[] | select(.container_id == "oc-148ccf1c-ef13-47f8-a")'
```

Must report `"healthy": true` with no `config_drift` or `response_time`
anomalies. Admin-health alerts auto-route to `agent.bywayofmj.com` via
Cloudflare tunnel (see `59a926e`) — a silent hour with no alerts is a
good sign.

### 7. Decision

- **Pass** — merge the PR. `.github/workflows/ci-cd.yml` builds
  `nbhd-openclaw:<full-sha>` and `:latest`, bumps pending configs for
  all tenants, and the `apply-pending-configs` cron rolls the new image
  to the whole fleet. The canary container reverts from `canary-<short>`
  to the canonical `<full-sha>` tag on its next image update, so no
  lingering canary state.
- **Fail** — do NOT merge. Roll back the canary first (see §8), fix on
  the branch, rebuild, redeploy to canary, retest.

### 8. Rollback

```bash
# Find the last known-good image tag for this tenant
az containerapp revision list \
  --name ${CANARY_CONTAINER} \
  --resource-group rg-nbhd-prod \
  --query "[?properties.active].{name:name, image:properties.template.containers[0].image, created:properties.createdTime}" \
  -o table

# Roll back by redeploying the previous tag
az containerapp update \
  --name ${CANARY_CONTAINER} \
  --resource-group rg-nbhd-prod \
  --image nbhdunited.azurecr.io/nbhd-openclaw:<previous-sha>
```

The previous image's revision is also still around for the Container App's
retention window; activating that revision is an even faster rollback:

```bash
az containerapp revision activate \
  --name ${CANARY_CONTAINER} \
  --resource-group rg-nbhd-prod \
  --revision <previous-revision-name>
```

## Cleanup

Canary ACR tags (`canary-<shortsha>`) accumulate. Periodically prune with:

```bash
az acr repository show-tags \
  --name nbhdunited \
  --repository nbhd-openclaw \
  --orderby time_desc \
  --query "[?starts_with(@, 'canary-')]" -o tsv
# delete old ones as appropriate, keeping the most recent 2-3
```

## Open improvements

- Wrap §3 in a `canary_tenant_image` management command so the runbook
  collapses to `make canary TAG=canary-abc123`.
- Add a `make canary-health TENANT_ID=<uuid>` target that polls admin-health
  for a specific tenant until healthy or timeout.
- Consider storing the "known-good tag per tenant" on the `Tenant` model
  so rollback is one command instead of querying revisions.
