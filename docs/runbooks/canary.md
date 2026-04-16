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
dogfooding. Failures here reach only one human. This is the default the
`make canary*` targets use; override with `CANARY_CONTAINER=...` if the
canary subject changes.

## Prerequisites

The Make targets run locally. You need:

- `az` logged in to the prod subscription (`az login`)
- A working local Django env that can talk to the prod DB and Azure
  (the same env you use for `make tenants` / `make health`)
- `DEPLOY_SECRET` exported in your shell (only required for `canary-health`)
- `jq` installed (only required for `canary-health`)

## Procedure

### 1. Open the PR first

CI runs tests/lint/validator on every branch, but deploy is gated on
`github.ref == 'refs/heads/main'` (`.github/workflows/ci-cd.yml:211`).
A branch push alone doesn't touch production. Confirm CI is green on the
PR before proceeding.

### 2. Build + deploy the canary

From the PR branch:

```bash
make canary
```

That runs `canary-build` followed by `canary-deploy`:

- `canary-build` calls `az acr build` against the live ACR, tagging the
  image `canary-<short-sha>` so it can't collide with CI's `<full-sha>`
  / `latest` tags. Nothing else in the fleet picks it up.
- `canary-deploy` shells out to `python manage.py canary_tenant_image`
  locally, which calls `apps.orchestrator.azure_client.update_container_image`
  to flip just the canary container onto the new tag.

> **Why we don't use `az containerapp exec` to invoke Django:** that
> command requires a TTY and the nested-quoting around
> `shell -c "..."` is fragile. Running `manage.py` locally uses the same
> Azure SDK credentials and DB connection without those problems.

> **Why the DB tag is deliberately left alone:** `canary_tenant_image`
> updates the container image but does NOT update
> `Tenant.container_image_tag`. When the PR merges, the normal
> `apply-pending-configs` cron sees the canary tenant's stored tag is out
> of date and rolls it onto the canonical fleet tag — no manual cleanup
> of the canary state.

Override defaults if needed:

```bash
make canary CANARY_CONTAINER=oc-<other> CANARY_TAG=canary-myhotfix
```

### 3. Watch startup

```bash
make canary-logs
```

(Equivalent to `az containerapp logs show --name <CANARY_CONTAINER> --resource-group rg-nbhd-prod --tail 200 --follow`.)

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

### 4. Real-usage verification

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

### 5. Health gate

```bash
DEPLOY_SECRET=... make canary-health
```

Polls `/api/v1/cron/admin-health/` and filters to the canary container.
Must report `"healthy": true` with no `config_drift` or `response_time`
anomalies. Admin-health alerts auto-route to `agent.bywayofmj.com` via
Cloudflare tunnel (see `59a926e`) — a silent hour with no alerts is a
good sign.

### 6. Decision

- **Pass** — merge the PR. `.github/workflows/ci-cd.yml` builds
  `nbhd-openclaw:<full-sha>` and `:latest`, bumps pending configs for
  all tenants, and the `apply-pending-configs` cron rolls the new image
  to the whole fleet. Because the canary's DB `container_image_tag` was
  left untouched in step 2, that same cron also reconciles the canary
  container off the `canary-*` tag back onto the canonical tag — no
  lingering canary state.
- **Fail** — do NOT merge. Roll back the canary first (see §7), fix on
  the branch, rebuild, redeploy to canary, retest.

### 7. Rollback

Find the last known-good tag this tenant was on (the SHA the rest of
the fleet is currently running is a safe choice):

```bash
az containerapp revision list \
  --name oc-148ccf1c-ef13-47f8-a \
  --resource-group rg-nbhd-prod \
  --query "[?properties.active].{name:name, image:properties.template.containers[0].image, created:properties.createdTime}" \
  -o table
```

Then redeploy that tag to the canary container:

```bash
make canary-rollback PREV_TAG=<previous-sha>
```

For the absolute fastest rollback (if the previous revision is still
within the Container App's retention window), activating it skips the
image pull entirely:

```bash
az containerapp revision activate \
  --name oc-148ccf1c-ef13-47f8-a \
  --resource-group rg-nbhd-prod \
  --revision <previous-revision-name>
```

## Cleanup

Canary ACR tags accumulate. To inspect:

```bash
make canary-prune
```

That lists all `canary-*` tags newest-first and prints the delete command
template. Keep the most recent 2–3, delete the rest by hand.

## Open improvements

- Add an automatic poll-until-healthy mode to `make canary-health`
  (currently a one-shot query).
- Consider storing the "known-good tag per tenant" on the `Tenant` model
  so `make canary-rollback` can look up `PREV_TAG` automatically instead
  of asking the operator to query revisions.
- Auto-prune `canary-*` tags older than N days from CI on a schedule.
