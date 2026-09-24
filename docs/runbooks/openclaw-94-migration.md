# OpenClaw 2026.9.4 tenant migration

This command changes tenant runtimes. Implementation/testing must use mocked
transports and local databases. Production execution belongs to the release
orchestrator, after the draft PR is reviewed, merged and deployed. Do not use
`rollout_byo_image_bump`, `bump_openclaw_version`, or an image-only rollback to
perform this migration.

## Preflight

- Keep `OPENCLAW_IMAGE_ROLLOUT_TENANT_IDS` **empty**. The migration command uses
  an explicit UUID list, independent of that image rollout allowlist.
- Choose an immutable `2026.9.4-<sha>` built with #1623, #1626, #1627 and #1635
  (including the 26,000-character USER.md patch). A green CI run can have skipped
  its image build: confirm the actual ACR push and manifest digest.
- Management environment prerequisites: deployed Django migrations, Azure CLI
  authenticated for ACR metadata, provisioner identity allowed to read/update
  Container Apps and obtain console tokens, console WebSocket connectivity,
  share access and working per-tenant signing keys. `AZURE_MOCK` must be false.
  No payloads or credentials should be copied into terminal output/tickets.
- Select one consenting, awake **legacy** canary. Verify the recorded binary
  version independently for bare-SHA images; do not infer it using the fallback
  image-tag mapper. Hibernated tenants are refused. Wake them on their current
  image first, then explicitly migrate the awake tenant.
- Avoid concurrent tenant config/image/lifecycle operations and user cron edits
  during the maintenance window. Do not run overlapping commands for a tenant.
- Check DB cron provenance. Already-canonical rows remain authoritative; live
  exports only add missing names. Legacy imports never delete absent rows.
  Incomplete/paginated/malformed HTTP lists, duplicate names, and noncanonical
  tenants with typed rows are refused. Resolve provenance before continuing.
- Preview the explicit scope (no DB writes, Azure, ACR, HTTP or console calls):

  ```bash
  python manage.py migrate_tenant_openclaw --tenant "$TENANT_ID" --tag "$TAG" --dry-run
  python manage.py migrate_tenant_openclaw --tenants "$ID1,$ID2,$ID3" --tag "$TAG" --dry-run
  ```

Dry-run lists remaining steps; it cannot certify remote health or registry
existence. Execution resolves and pins the target digest, records the source
image/digest, DB tag/version, awake state and a redacted template, then captures
**every** live HTTP job including disabled jobs and agent jobs before restarting.
Literal template environment values are deliberately redacted; recover secrets
from their authoritative config/Key Vault bindings, never from this record.

## One canary

Approved order: E2E tenant `4e13ec0e-08d8-4999-8070-d446475f29e4` first
(real 5.28 → 9.4; run the nbhd-e2e wake/chat/receipts harness). Next verify MJ's
already-9.4 tenants `148ccf1c-ef13-47f8-ada1-a98fa90e14a0` and
`1c77c8c1-f721-4f5b-9b98-403ac25b070c` as no-ops, with no downgrade. Then migrate
one real legacy user selected by MJ before batches of 3 → 10 → rest.

```bash
python manage.py migrate_tenant_openclaw --tenant "$TENANT_ID" --tag "$TAG"
```

The durable checkpoints are:

1. `preflight`: registry digests, original template and a fresh revision suffix.
2. `capture`: complete live cron export plus additive Postgres import.
3. `image`: one revision containing the digest-pinned image, oc-state EmptyDir,
   four state/config/workspace/cache variables, node-owned 0700/0600 share mount
   options, plugin-runtime-deps and index-cache. Wait for that latest revision,
   `/proxy-health` and `/healthz` (bounded to five minutes).
4. `version`: write image tag and `openclaw_version=2026.9.4` together.
5. `config`: strict config/workspace refresh, version stamp, then wait for the
   gateway's resolved/applied config revision tokens to match and health to pass.
6. `crons`: write signed Postgres truth, wait for CLI reconciliation, remove a
   legacy duplicate only after a matching signed job exists. Matching happens
   inside the replica, including payload and scheduling parameters.
7. `verify`: compare signed/actual jobs and IDs across two operator-CLI polls
   25 seconds apart, verify health and inspect the last five minutes of console
   logs for fs-safe/SQLite/config/proxy/signature errors. Persist counts only.
   An unavailable or saturated log window fails closed.

A successful record is `PASS`. Already-9.4 tenants without a partial migration
receive read-only verification of their current Azure/DB image identity, applied
config, signed cron set/stable IDs, health and console window, and report
"Already on 9.4; verification PASS". Dry-run skips those remote checks. A failed
check stops the batch without changing their runtime or migration record. This
is not a repair command for undocumented image-only upgrades.
An existing incomplete record always takes precedence over matching image tags.
A retry reuses the saved revision rather than creating a second revision.

Disabled jobs remain disabled in Postgres and in the full source export; the
signed writer does not schedule them. Non-agent unmanaged rows are explicitly
included in the signed set through the record's `preserved_unmanaged_ids`.
Agent-owned recurring jobs are preserved in the export and listed in
`needs_agent_resync`; the orchestrator must arrange that re-sync before fleet
expansion. Never delete the migration record: it also identifies preserved
unmanaged jobs for subsequent signed writes.

## Read migration records safely

`Tenant.openclaw_migration` contains `status`, `step`, `completed`, per-step
start/completion timestamps, evidence, source cron export and failure metadata.
It is private operational data, not a subscriber-facing response. Print only
selected metadata, never the full JSON (cron payloads are private):

```python
from apps.tenants.models import Tenant
r = Tenant.objects.get(pk=TENANT_ID).openclaw_migration
print({k: r.get(k) for k in ("tag", "status", "step", "completed", "updated_at", "failure")})
print("Agent re-sync required:", len(r.get("needs_agent_resync", [])))
print("Captured jobs:", len(r.get("cron_export", [])))
```

`RUNNING` claims have a renewable 30-minute lease. After an interrupted process,
confirm it has exited and wait for lease expiry before retrying the same command.
`FAILED` resumes immediately from its failed checkpoint. A different target tag
is refused. The first failure stops an explicit batch; later tenants are untouched.

## Verification and expansion

The automated command is an infrastructure gate. The orchestrator must also:

- Confirm Azure image/digest and DB tag/version agree, storage/env/mount options
  are present, and signing-key bindings match. No provider/BYO or Brave key was
  silently replaced; secret references are preserved by the revision update.
- Send a synthetic chat turn and run a **Brave search**; verify actual responses.
- Make a controlled scheduled delivery, check no duplicates, then test
  hibernate → wake → delivery. Inspect `cron_suspend_state` if wake fails:
  signed managed jobs are paused in Postgres-backed lifecycle state and rebuilt;
  only previously enabled operator jobs are re-enabled. Disabled jobs stay disabled.
- Resolve all agent re-sync IDs and review any one-shots whose fire time elapsed
  during maintenance. Do not blindly replay a reminder that may already have fired.
- Require stable cron IDs, clean console/error telemetry and no user-visible
  regression during the canary soak; health alone is insufficient.

Expand with explicit lists in sizes **1 → 3 → 10 → rest**, verifying and soaking
each batch before the next. There is no `--all` option. Save each batch's UUID
list and metadata-only results in the release record. Keep sleeping tenants on
their existing runtime until an explicit awake migration.

## Failure and manual recovery

Any failure stops, records its step and safe error evidence, and leaves the
tenant as it is. **There is no automatic rollback.** Do not clear the record,
reset completed steps, or run a competing image bump to force progress.

- Before image submission: fix registry access or cron provenance, inspect the
  saved export against current live truth if time has passed, then resume.
- After submission but before health: inspect the saved revision identity and
  metadata-only console errors. Retry the same tag/suffix after repairing the
  root cause; do not add another revision or overwrite the export.
- After version/config: repair the failing config/workspace write or gateway
  pickup, then resume. Failed config writes do not silently pass the checkpoint.
- After signed cutover: check the signing key, mapping of declaration keys to
  Postgres rows, agent re-sync requirements and duplicate metadata. Unknown
  legacy jobs are never deleted by name alone. Fix the mismatch, then resume.
- If old runtime recovery is unavoidable, stop the tenant under orchestrator
  control; independently identify its old binary version (especially bare SHA),
  recover the old compatible config/workspace from authoritative sources, restore
  the saved storage template/secret references and old image, restore matching DB
  version/tag, and restore cron truth over the old supported transport. Review
  resurrected SMB cron state and duplicates **before** reactivating deliveries.
  There is no implemented inverse migration guaranteeing this recovery.

Operational CLI contracts are checked against the upstream
[9.4 cron commands](https://github.com/openclaw/openclaw/blob/v2026.9.4/src/cli/cron-cli/register.cron-simple.ts)
and [config revision response](https://github.com/openclaw/openclaw/blob/v2026.9.4/src/gateway/config-get-response.ts).

## Review-round safety checks and read-only verification

For an already-9.4 tenant, use `--verify-only` (in the approved management
context, by the release orchestrator):

```sh
python manage.py migrate_tenant_openclaw --tenant <uuid> --verify-only
```

This performs no tenant, image, config, cron, or migration-record writes.
Output is `PASS verified` or `FAIL <reason_code>`; failure exits nonzero and stops
the explicit batch. It checks the tenant's existing image, regardless of the
configured fleet target. Use this for the MJ and `1c77c8c1` health checks.

- ACR digest resolution uses the Django container's **system-assigned managed
  identity**, requiring AcrPull on `nbhdunited`. It uses the ACR token exchange
  and manifest HEAD APIs, with no Azure CLI dependency or token logging.
- Every capture retry and the final pre-image check read live HTTP truth again.
  Earlier exports remain in private `cron_export_history`; import-owned canceled
  rows are disabled, and edited rows are refreshed without deleting other DB rows.
- Running jobs, unknown next-fire times, and jobs due within 20 minutes defer
  image replacement (`cron_running`, `cron_next_fire_unknown`, `cron_imminent`).
  Choose a quiet maintenance window; frequent recurring jobs may require an
  explicitly coordinated scheduling change before migration can proceed.
- Unknown execution fields and unsupported declarations fail before replacement
  (`unsupported_cron`). The shared field contract preserves delivery recipient,
  channel, account/thread, session, retention, thinking, pacing and other mapped
  execution controls. Arbitrary scripts and fixed interval anchors are refused.
- Verification records every captured one-shot, including historical exports.
  Expired reminders without positive delivery evidence fail with
  `one_shot_expired_undelivered`; missing future reminders fail with
  `one_shot_pending_missing`. Cancellation vs. delivery cannot be inferred from
  absence alone. Review private records and delivery evidence before recovery;
  never mark an absent reminder delivered merely to pass verification.
- Hibernation stores full noncanonical declarations in private
  `cron_suspend_state`, transfers them through authenticated share files, and
  recreates lost jobs after EmptyDir destruction. Payloads never cross
  `NBHD_RESULT` or appear in logs. Temporary files are removed after transfer.
  The recovery record clears only after restored declarations and signed jobs
  are verified. An aborted suspension resumes scheduling immediately; failed
  recovery retains the record and original schedule snapshot for retry.
- `idle_hibernate_skipped tenant=<short-id> reason=<code>` is one structured
  WARNING per failed capture/suspend/probe attempt. Monitor this in Log Analytics
  for unexpectedly awake containers and retained recovery records.

Deploy the orchestrator changes and build a fresh OpenClaw image from this
branch before migrating. The controller supplies its current comparison code
for read-only checks and restoration on existing 9.4 images; the image's signed
poller also needs the updated adapter to apply all execution fields.
