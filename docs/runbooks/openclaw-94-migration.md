# OpenClaw 2026.9.4 tenant migration

This command changes tenant runtimes. Implementation/testing must use mocked
transports and local databases. Production execution belongs to the release
orchestrator, after the draft PR is reviewed, merged and deployed. Do not use
`rollout_byo_image_bump`, `bump_openclaw_version`, or an image-only rollback to
perform this migration.

## Preflight

Merge/deploy main including #1651 before canaries, so regenerated prompts use
the current panel instruction. No image rebuild is required by this rescope.

- Keep `OPENCLAW_IMAGE_ROLLOUT_TENANT_IDS` **empty**. The migration command uses
  an explicit UUID list, independent of that image rollout allowlist.
- Choose an immutable `2026.9.4-<sha>` built with #1623, #1626, #1627 and #1635
  (including the 26,000-character USER.md patch). A green CI run can have skipped
  its image build: confirm the actual ACR push and manifest digest.
- Management environment prerequisites: deployed Django migrations, Django
  system-assigned managed identity with ACR data-plane pull access, provisioner identity allowed to read/update
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
  hibernate → wake → delivery. File-cron tenants compute their next wake from
  the same Postgres rows as the signed writer and skip blocked HTTP suspension.
  Boot sync reinstalls signed jobs; disabled jobs stay disabled.
- Resolve all agent re-sync IDs and review any one-shots whose fire time elapsed
  during maintenance. Do not blindly replay a reminder that may already have fired.
- Require stable cron IDs, clean console/error telemetry and no user-visible
  regression during the canary soak; health alone is insufficient.

Expand with explicit lists in sizes **1 → 3 → 10 → rest**, verifying and soaking
each batch before the next. There is no `--all` option. Save each batch's UUID
list and metadata-only results in the release record. Keep the image rollout allowlist empty so sleeping legacy tenants use main's
existing wake behavior on their current image until an explicit awake migration.

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
  Earlier exports remain in private `cron_export_history`; only unchanged
  import-owned rows are refreshed. Newer canonical edits and dashboard deletions
  win over stale runtime exports; retries never resurrect deleted imported rows.
- Only enabled **one-shot** jobs running, lacking a parseable due time, or due
  within 20 minutes defer cutover (`cron_running`, `cron_next_fire_unknown`,
  `cron_imminent`). Eligibility runs before import or canonical changes.
- Recurring jobs are not paused and do not trigger the imminence guard. A fire
  during the approximately five-minute cutover may be skipped; missed recurring
  fires are not replayed. Image/config failures can extend this interruption.
- Unsupported declarations fail closed. The migration-only comparator checks
  delivery destinations (`delivery.to`) and other execution fields inside the
  replica. It does not change the image's signed poller. If the deployed image
  cannot reproduce a captured declaration, verification stops; resolve that
  image compatibility separately before expansion.
- Every captured one-shot is audited, including historical exports. Absence
  from both current Postgres truth (including deleted/tombstoned rows) and the
  live runtime at verification is `cancelled`, with an observation timestamp.
  A remaining canonical row with missing runtime delivery still fails as
  `one_shot_pending_missing` or `one_shot_expired_undelivered`. Never infer delivery
  from absence or silently mark a missing canonical reminder delivered.

## Scope and lifecycle limitations

This PR changes no OpenClaw image files. Automatic image tasks, message updates,
wake, suspension/resume, runtime capture and delayed restore retain main behavior.
Keep the image rollout allowlist empty during migration. The storage retrofit
is explicitly enabled only by the migration, never ordinary image updates.

The sole new lifecycle behavior is in hibernation for `tenant_uses_file_cron_sync`
tenants: compute cron wake from the signed writer's canonical Postgres set and
skip HTTP suspend. Scale-to-zero stops firing and boot sync restores the signed
set. The payload-free `noncanonical_jobs_not_restored` count uses Postgres plus
the last snapshot: it is an estimate, not a fresh runtime inventory. Uncaptured
native/operator jobs are unknown and may be lost on scale-to-zero.

Manual fleet tools are the approved exception: `bump_all_tenant_images`, its
`rollout-byo-image-bump` endpoint, `rollout-atomic-bump`, and
`bump_openclaw_version --all` refuse family crossings or ambiguous image tags.
The atomic endpoint requires a valid explicit `tenant_id`; empty/malformed JSON,
empty/unknown scope, and mismatched image/version families are rejected before
publication. Use the migration command for 5.x/bare-SHA → 9.x.

Follow-ups, outside this PR:
- Poller error logging can expose reminder names/argv (pre-existing REVIEW3 #8).
  Replace it with fixed reason codes and safe counts in a separate image change.
- Persist noncanonical jobs by making every creator canonical or defining a
  separate runtime persistence contract. This PR adds no private restore store.
- Harden lifecycle queue failures, ambiguous Azure outcomes and concurrent wakes
  separately. No recovery callbacks, sleep-intent markers or pause machinery are
  introduced here.

## Exact deployed-container execution (release orchestrator only)

Do not run migration from a laptop Python environment: registry resolution uses
**the deployed Django system-assigned identity**, not a developer login. No Azure
CLI is required or invoked inside the Django container. The following workstation
Azure CLI commands only establish a revision-pinned console (the same pattern as
prod-exec); an approved Azure Portal console can be used instead.

```bash
APP=nbhd-django-westus2
RG=rg-nbhd-prod
REV=$(az containerapp show -g "$RG" -n "$APP" --query properties.latestReadyRevisionName -o tsv)
test -n "$REV"
az containerapp revision show -g "$RG" -n "$APP" --revision "$REV" \
  --query '{active:properties.active,image:properties.template.containers[].image,containers:properties.template.containers[].name}' -o json
az containerapp show -g "$RG" -n "$APP" --query properties.configuration.ingress.traffic -o json
# Require this ready, active revision to be the intended serving Django build.
# Copy the Django container name from the metadata above (not a sidecar).
CONTAINER=nbhd-django-westus2
az containerapp replica list -g "$RG" -n "$APP" --revision "$REV" \
  --query '[].{name:name,containers:properties.containers[].name}' -o json
# Pin one ready replica from that revision; do not let exec choose another build.
REPLICA='<ready-replica-name>'
az containerapp exec -g "$RG" -n "$APP" --revision "$REV" \
  --replica "$REPLICA" --container "$CONTAINER" --command /bin/bash
```

Inside that console:

```bash
cd /app
python manage.py showmigrations tenants
python manage.py check
# Confirm 0168/0169 are applied, and command help includes --verify-only.
python manage.py migrate_tenant_openclaw --help
TAG='2026.9.4-<approved-build-sha>'
TENANT_ID=4e13ec0e-08d8-4999-8070-d446475f29e4
python manage.py migrate_tenant_openclaw --tenant "$TENANT_ID" --tag "$TAG" --dry-run
```

Before executing, verify each prerequisite from this same container:

- **Identity/ACR:** confirm the system identity has `AcrPull` (or the registry's
  equivalent repository pull role). Test the actual token exchange and manifest
  HEAD, not just ARM role listings: in `python manage.py shell`, call
  `registry_digest("nbhdunited.azurecr.io/nbhd-openclaw:<approved-tag>")` from
  `apps.orchestrator.openclaw_migration`. Print only the digest. Require outbound
  HTTPS/DNS to `nbhdunited.azurecr.io` and the managed-identity endpoint. Never
  print credentials, request bodies, tokens or environment dumps.
- **ARM/console:** provisioner identity can read/update tenant Container Apps,
  list revisions/replicas and obtain console auth tokens. Use
  `runtime_operator.run_node(tenant, "return {ok:true};")` as a payload-free
  console WSS probe and `console_error_counts(tenant, since=...)` for log HTTPS.
  Both must succeed, not merely ARM's token call. No `cron.list` payload output.
- **Health:** from Django, both tenant HTTPS `/proxy-health` and `/healthz` return
  200. Use `wait_healthy(tenant, timeout=30)` for a bounded read-only check.
- **Share/key:** provisioner can read/write/delete the selected tenant's `ws-*`
  share over HTTPS; verify a uniquely named, payload-free probe via
  `_put_share_file`, `download_workspace_file_binary`, `delete_workspace_file`
  in a `try/finally`. Verify signing-key bindings without displaying their values.
- **Queue:** QStash token and callback URL are configured for cron-aware wake.
- **Privacy:** require encrypted database/backups and Azure Files at rest, TLS in
  transit, restricted service-identity access and no payload logging. These are
  release prerequisites; local tests do not establish production configuration.

Then execute the approved canary command above without `--dry-run`. After the
legacy canary passes, run these exact **read-only** checks inside `/app`:

```bash
python manage.py migrate_tenant_openclaw --tenant 148ccf1c-ef13-47f8-ada1-a98fa90e14a0 --verify-only
python manage.py migrate_tenant_openclaw --tenant 1c77c8c1-f721-4f5b-9b98-403ac25b070c --verify-only
```

## Private migration record retention

Raw migration JSON is excluded from Django admin forms and public serializers.
No private lifecycle declarations or temporary cron transfer files are created.
Retain failed exports while recovery remains unresolved. After successful canary
soak and 7 days, an authorized operator should remove `cron_export` and
`cron_export_history` while retaining metadata, `preserved_unmanaged_ids`,
dispositions and audit timestamps; apply the retention policy to backups too.
Never delete preserved unmanaged IDs while those jobs remain in the signed set.
