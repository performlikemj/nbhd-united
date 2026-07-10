# Encryption-at-Rest — Phase 1 Status & Azure Activation Runbook

**As of 2026-07-11.** Companion to `docs/encryption-at-rest-directive.md` (master design) and `CONTINUITY_encryption-phase1.md` (Phase 1 PR plan). This is the live handoff doc: what's done, what's left, and the exact commands to finish.

---

## Current state

**Phase 1 code: COMPLETE + deployed, DARK.** Six PRs merged to main (#1101 KEK SDK, #1102 CI predicate guard, #1103 tenant_deks+keys, #1105 box/codec/cache/RedactedStr/audit, #1106 provision wiring+backfill, #1114 prewarm). Two Fable-5 reviews fixed 3 crypto-core bugs + 1 live provisioning regression. Nothing encrypts user data yet; no `_enc` column exists.

**Hotfix live (#1115):** the provision-time DEK mint is currently NON-FATAL (dark-window band-aid) so a missing vault can't break signups. Carries `# TODO(encryption Phase 2): restore fail-closed mint once kv-nbhd-keks + broker RBAC exist` in `apps/orchestrator/services.py`. **Must be reverted to fail-closed after the backfill covers all tenants.**

**Azure Phase A + 4a: DONE + independently verified (2026-07-11).**
- Vault `kv-nbhd-keks` — standard SKU, RBAC auth, 7-day retention, **purge-protection OFF** (`enablePurgeProtection: null`; Azure rejects explicit `false`, so the flag is omitted — off is correct and required for crypto-shred).
- 3 custom roles, exact dataActions:
  - `nbhd-kek-provisioner`: keys/read, create/action, rotate/action, wrap/action, delete — **NO unwrap, NO purge**
  - `nbhd-kek-decrypt-broker`: keys/read, unwrap/action — **only these two**
  - `nbhd-kek-breakglass-purge`: keys/read, purge/action — **assigned to NOBODY**
- Identity `mi-nbhd-decrypt`: clientId `92660daf-e31d-458c-bcc3-7906938bca28`, principalId `ed997d5a-7211-4171-845a-9278a95f35ee`.
- 2 role assignments at vault scope: provisioner MI (`1b3a79e1-...`) → provisioner role; broker (`ed997d5a-...`) → broker role. No break-glass assignment.
- `mi-nbhd-decrypt` **attached** to `nbhd-django-westus2` (4a — NO revision bump; still `--0001064`).

Discovery constants: SUB `63ceeeac-fe3f-4bcb-b6d2-b7aa7fd6bf52`, RG `rg-nbhd-prod`, LOC `westus2`, provisioner MI `mi-nbhd-provisioner` clientId `eee53161-93b7-4f8e-b0ed-a339dbf3d7f6`.

---

## Remaining steps to ACTIVATE (each still DARK — nothing gets encrypted)

Recommended order bundles the one restart with the backfill so production restarts ONCE and self-verifies.

### 1. Mint the 33 real keys (backfill) — RESOLVED: in-container via QStash trigger
Decision (2026-07-11, Fable): run the backfill inside the running container through the
existing QStash-signed task dispatcher — no new auth surface, no secret leaves Azure, and
the container already holds prod DB access + the provisioner MI. Operator-local was
rejected (it would put the prod `DATABASE_URL` on a laptop). Two zero-arg `TASK_MAP`
entries (this PR) because the QStash publish path can't carry a body:
- `POST {base}/api/cron/trigger/backfill_tenant_deks_dry_run/` — logs the candidate list (expect 33 — verified against prod 2026-07-11: 33 active+suspended tenants, 0 DEK rows), zero Azure/DB writes.
- `POST {base}/api/cron/trigger/backfill_tenant_deks/` — real run (idempotent, per-tenant isolated, safe under QStash retries).
Publish with no body via the upstash QStash tooling. Output returns in the HTTP response
AND as an INFO line in `ContainerAppConsoleLogs_CL`. Verify after the real run:
`SELECT count(*), min(dek_epoch), max(dek_epoch) FROM tenants_tenantdek` ≈ fleet size, all epoch 0.

### 2. 4b — env var (GATED: this is the app restart)
```bash
az containerapp update -n nbhd-django-westus2 -g rg-nbhd-prod \
  --set-env-vars AZURE_DECRYPT_BROKER_CLIENT_ID=92660daf-e31d-458c-bcc3-7906938bca28
```
Value = broker **clientId** (not principal/resource id). Do NOT set `AZURE_KEK_VAULT_NAME` (default already matches). **Creates a new revision — single-revision app, wedge risk** ([[project memory: same-tag re-bump wedges single-revision apps]]).
- VERIFY: revision advanced past `--0001064`, `provisioningState: Succeeded`, env var present, exactly one active healthy revision at traffic 100.
- WEDGE RECOVERY: recover-at-point-of-failure via direct `az containerapp update` re-apply (OPENCLAW_IMAGE_TAG+SENTRY_RELEASE pattern), NOT rollback-then-retry.
- ROLLBACK: `--remove-env-vars AZURE_DECRYPT_BROKER_CLIENT_ID` (harmless to leave — dark code no-ops on empty TenantDek lookup).

### 3. Broker gate (the real proof) — after keys exist + 4b restart
Prewarm runs on gunicorn/poller start; with keys present + the env var set, it unwraps via the broker identity.
```bash
az monitor log-analytics query --workspace 035a49db-1da5-452d-8b32-b074d7a5d606 \
  --analytics-query "ContainerAppConsoleLogs_CL | where ContainerAppName_s == 'nbhd-django-westus2' | where TimeGenerated > ago(15m) | where Log_s has 'DEK warm failed' or Log_s has 'Unwrapped DEK' | project TimeGenerated, Log_s" -o table
```
PASS = **zero** `DEK warm failed` warnings + ~fleet-size `Unwrapped DEK for tenant …` INFO lines. Proves the CONTAINER's `mi-nbhd-decrypt` identity unwraps through real RBAC (a laptop round-trip proves nothing — the operator holds no data-plane key role).

### 4. Restore fail-closed provisioning
Revert the hotfix soft-fail in `apps/orchestrator/services.py` (the `try/except` around `mint_and_wrap_dek`) back to a raising call — safe once every tenant has a DEK. Remove the TODO.

---

## Then: Phase 2 preconditions (before the FIRST real ciphertext)
From the Fable-5 holistic review — settle each before any column flips:
1. **Key-derivation format:** `box.encrypt` uses the RAW DEK; directive §3.1 says content encrypts under `subkey(dek, "content-v1")`. Whatever encrypts the first row is the format forever (short of a re-encrypt migration). **Decide first.**
2. **Wire `audit.set_principal`** — zero callers today; define per-request boundaries (admin middleware, owner-export, cron) or every human read audits as silent "system".
3. **RedactedStr raw-buffer leak guard** — it leaks through `json.dumps`/DRF JSONRenderer, `.encode()`, `+`/`.join`/slice; add a CI/lint guard before the first read-flip.
4. **Re-subscribe DEK-liveness (Finding 2):** re-provisioning a cancelled tenant reuses the same row with a soft-deleted/purged KEK → every crypto op fails forever. Verify KEK liveness at provision (recover-in-window / new epoch) or retire TenantDek rows on deprovision.
5. Fix the mock's `begin_delete` semantics (real KV disables crypto on delete immediately; grace = recoverable, not usable).
6. `reply_text` writer locking + `has_text`/`has_parsed_items` sidecars + empty-string convention; route `keys.unwrap_dek_for` through the cache.

Then Phase 2 encrypts `AppChatMessage.user_text` first (the one still-verbatim column), behind a flag, backfill, flip. Phases 3–6 per the directive. Phase 0b (storage CMK) is independent, ~1h, whenever.

**Decision already locked:** software keys (not HSM) — amend directive §3.1 "HSM-resident" wording and the Phase-4 user claim ("hardware-backed" → "a dedicated key vault, separate from your data").
