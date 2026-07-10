# Encryption-at-Rest — Phase 1 Status & Azure Activation Runbook

**As of 2026-07-11 — Phase 1 ACTIVATED.** Companion to `docs/encryption-at-rest-directive.md` (master design) and `CONTINUITY_encryption-phase1.md` (Phase 1 PR plan). This is the live handoff doc: what's done, what's left, and the exact commands to finish.

---

## Current state

**Phase 1 code: COMPLETE + deployed, DARK.** Six PRs merged to main (#1101 KEK SDK, #1102 CI predicate guard, #1103 tenant_deks+keys, #1105 box/codec/cache/RedactedStr/audit, #1106 provision wiring+backfill, #1114 prewarm). Two Fable-5 reviews fixed 3 crypto-core bugs + 1 live provisioning regression. Nothing encrypts user data yet; no `_enc` column exists.

**Provisioning mint: FAIL-CLOSED (restored by PR B, this change).** The #1115 dark-window soft-fail is gone. Now that the vault + RBAC are live and every tenant has a DEK (2026-07 backfill), a provision-time mint failure raises and aborts provisioning (tenant resets to PENDING) rather than stranding a container-having, DEK-less tenant. The `# TODO(encryption Phase 2)` in `apps/orchestrator/services.py` is removed.

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

## ACTIVATED 2026-07-11 — Phase 1 keys are live (still no ciphertext)

All four activation steps ran and self-verified on 2026-07-11. Nothing encrypts user data
yet — this only means every tenant now HAS a DEK and the container can unwrap it.

**1. Backfill — DONE.** Ran in-container 2026-07-11 (~17:08 UTC) via a no-body QStash
publish to `/api/cron/trigger/backfill_tenant_deks/`: `Minted: 33, Failed: 0` (HTTP 200 in
22.8s). DB verified: 33 rows / 33 distinct tenants / all `dek_epoch=0` in `tenants_tenantdek`.
The two backfill `TASK_MAP` entries (`backfill_tenant_deks` + `backfill_tenant_deks_dry_run`,
PR #1116) STAY REGISTERED — the command is idempotent, so a re-fire is now a harmless no-op;
no de-registration needed.

**2. 4b env var — DONE.** `AZURE_DECRYPT_BROKER_CLIENT_ID=92660daf-e31d-458c-bcc3-7906938bca28`
(broker **clientId**) set on `nbhd-django-westus2`. Revision `--0001066` is Healthy and the
sole active revision at 100% traffic. Kept for posterity — rollback is
`--remove-env-vars AZURE_DECRYPT_BROKER_CLIENT_ID` (harmless to leave; dark code no-ops on an
empty TenantDek lookup); a wedge on the restart is recovered at the point of failure via a
direct `az containerapp update` re-apply (OPENCLAW_IMAGE_TAG+SENTRY_RELEASE pattern), NOT
rollback-then-retry.

**3. Broker gate — PASS.** On `--0001066`, Log Analytics showed 99 `Unwrapped DEK` INFO lines
and 0 `DEK warm failed` warnings (3 gunicorn workers × 33 tenants). The draining `--0001065`
logged 99 warm-fails (keys existed, env var did not) — expected fallback noise that died with
that revision. Proves the container's `mi-nbhd-decrypt` identity unwraps through real RBAC
(a laptop round-trip would prove nothing — the operator holds no data-plane key role).

**4. Fail-closed provisioning — DONE (this PR).** The #1115 dark-window soft-fail around
`mint_and_wrap_dek` in `apps/orchestrator/services.py` is reverted to a plain raising call and
the `TODO(encryption Phase 2)` is removed: a provision-time mint failure now aborts
provisioning (tenant resets to PENDING) instead of stranding a container-having, DEK-less
tenant. Safe now that every tenant has a DEK.

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
