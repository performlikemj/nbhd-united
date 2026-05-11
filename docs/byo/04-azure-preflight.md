# Azure pre-flight — production is in a clean baseline

Verified `2026-05-11` against `rg-nbhd-prod` (subscription `63ceeeac-fe3f-4bcb-b6d2-b7aa7fd6bf52`). The implementing agent can rely on these invariants when designing the rollout.

## What's clean

**All 26 tenant containers are on the platform key path:**
```
26 of 26 oc-* containers have:
  env: ANTHROPIC_API_KEY → secretRef: anthropic-key
  no CLAUDE_CODE_OAUTH_TOKEN env var
  no claude-code-oauth-token secret
```

**KV has zero BYO secrets:**
```
$ az keyvault secret list --vault-name kv-nbhd-prod \
    --query "[?contains(name, 'byo')].name" -o tsv
(empty)
```

**No tenant container has a stale `/home/node/.claude/` mount:**
```
Canary oc-148ccf1c volumes:
  workspace        → ws-148ccf1c-... (AzureFile, /home/node/.openclaw)
  sessions-scratch → EmptyDir (/home/node/.openclaw/agents)
  tasks-scratch    → EmptyDir (/home/node/.openclaw/tasks)
  plugin-runtime-deps → EmptyDir (/home/node/.openclaw/plugin-runtime-deps)
```
No claude-credentials mount exists on any container. New mount can be added without coordinating against an existing one.

## What this means for rollout

1. **No legacy migration needed.** The `cli_subscription` mode in `BYOCredential` has zero rows. The new `oauth_credentials` mode can be the canonical path on day one. Don't waste cycles on a back-compat shim.

2. **`apply_byo_credentials_to_container` reconciliation simplifies.** Since all 26 tenants are in identical state (`ANTHROPIC_API_KEY` bound, no BYO), the Phase 5 platform-key removal can run as a single fleet-wide bump with predictable behavior.

3. **Canary `oc-148ccf1c-ef13-47f8-a` is a safe test target.** It's representative of every other tenant. End-to-end verification here covers the fleet.

4. **No KV secret cleanup hazards.** When Phase 5 soft-deletes `anthropic-api-key`, it's a single secret across the platform — not per-tenant. KV soft-delete is enabled (90-day retention).

## Rollout sequence the data supports

| Step | Why safe |
|---|---|
| 1. Deploy backend with `oauth_credentials` mode support, keep `ANTHROPIC_API_KEY` injection alive | Additive change; no existing flow disturbed |
| 2. Build + push new OpenClaw image (cleaned entrypoint, no wrapper) | Image change only takes effect on revision bump |
| 3. Bump canary container via `bump_all_tenant_images --only oc-148ccf1c-...` | Canary on new entrypoint; still falls back to `ANTHROPIC_API_KEY` since no BYO yet |
| 4. Manually upload OAuth credentials via the new UI on canary | Tests Phase 1+2+3+4 end-to-end |
| 5. Verify with `claude live session start: provider=claude-cli` log line | Empirical confirmation |
| 6. Fleet rollout: `bump_all_tenant_images` | All tenants on cleaned entrypoint |
| 7. Phase 5 PR: remove `ANTHROPIC_API_KEY` injection + KV secret + settings | Independent PR, separately reviewable |
| 8. Soft-delete `anthropic-api-key` from KV | 90-day window for accidental rollback |

## Commands the implementing agent should run before merging

```sh
# Re-verify clean state before rollout (snapshot can drift)
az containerapp list --resource-group rg-nbhd-prod \
  --query "[?starts_with(name, 'oc-')] | length([])"
# Expect 26

az keyvault secret list --vault-name kv-nbhd-prod \
  --query "[?contains(name, 'byo') || contains(name, 'claude-code-oauth')]" -o table
# Expect empty

# Sanity check: anthropic-key is the only Anthropic secret
az keyvault secret list --vault-name kv-nbhd-prod \
  --query "[?contains(name, 'anthropic')].name" -o tsv
# Expect: anthropic-api-key (one line)
```

## Storage account state (informational, not gating)

Captured for the parallel hardening workstream — not blocking this PR:

- `stnbhdprod` is on Consumption-profile Container Apps env (no VNet integration possible without env migration)
- Shared-key auth only (Container Apps env storage REST API supports only `accountName + accountKey + shareName` at API version `2025-04-01`)
- Public network access enabled, no IP rules, TLS 1.0 minimum, no diagnostic logging, 7-day soft delete

The user has hardening (TLS 1.2, diagnostic logging, soft-delete bump, KV purge protection) tracked as a separate workstream — this BYO refactor does not depend on it.
