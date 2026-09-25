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
- Management environment prerequisites: all Django web/task workers on this
  revision, deployed Django migrations, Django
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
  exports only add missing names. For noncanonical tenants the complete live
  export is the only authority: cache-only rows are moved into the private
  `openclaw_migration.quarantined_cache` field and removed from `CronJob` in the
  same transaction as promotion. Quarantine is never signed or replayed.
  Reports preview `quarantined_cache=N`; execution reports the retained count.
  Incomplete/paginated/malformed HTTP lists, duplicate names, and noncanonical
  tenants with typed rows are refused. Resolve provenance before continuing.
- Preview the explicit scope (no DB writes, Azure, ACR, HTTP or console calls):

  ```bash
  python manage.py migrate_tenant_openclaw --tenant "$TENANT_ID" --tag "$TAG" --dry-run
  python manage.py migrate_tenant_openclaw --tenants "$ID1,$ID2,$ID3" --tag "$TAG" --dry-run
  ```

Read-only fleet sizing (per explicitly listed tenant, with live reads):

```bash
python manage.py migrate_tenant_openclaw --tenants "$ID1,$ID2,$ID3" --report
```

`--report` performs no checkpoint, import, canonical flip, image, config or cron
writes. It prints `READY`, `BLOCKED_UNSUPPORTED` plus reason counts, `DEFER`
plus fixed reason counts, `ALREADY_94`, or `RUNNING` with owner/lease metadata.
It continues across the complete scope.
`READY` establishes declaration compatibility at observation time, not registry,
health or release approval. Source failures and unavailable/hibernated tenants
report `DEFER`. Use `--verify-only` to inspect preservation on already-9.4 tenants.
Counts cover captured and canonical declarations separately; the same logical
job can contribute once from each authority. No reminder payloads are printed.

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
2. `capture`: complete live cron export; canonical additive import or noncanonical
   cache reconciliation/quarantine, then promotion and equivalent timestamp preparation.
3. `image`: fresh recapture/import/preparation, then persist the tenant's cron
   edit fence **before pre-staging or image submission**. Fence activation drains
   previously admitted writers. Write and read back `nbhd-crons.json` from the unchanged canonical selector.
   Persist `signed_prestaged` with the file SHA256, exact canonical content digests,
   row revision and count; persist recovery instructions and owner identity.
   5.28 ignores this file; the 9.4 entrypoint installs these jobs even if the
   management process dies immediately after image apply. Then submit one
   revision containing the digest-pinned image, oc-state EmptyDir,
   four state/config/workspace/cache variables, node-owned 0700/0600 share mount
   options, plugin-runtime-deps and index-cache. Wait for that latest revision,
   `/proxy-health` and `/healthz` (bounded to five minutes). After health, rebuild
   the expected signed bytes from current canonical truth, compare with the share
   and atomically replace only if the digest changed; record `signed_after_health`. Config regeneration
   remains after the image is live. Failed readback or changing canonical truth
   stops image submission. Immediately before submission, rebuild the signed
   digest from current canonical truth and re-read the share; re-stage if needed.
4. `version`: commit image tag, `openclaw_version=2026.9.4` and fence release in
   the same row update. Reconciliation now selects the file transport.
5. `config`: strict config/workspace refresh, version stamp, then wait for the
   gateway's resolved/applied config revision tokens to match and health to pass.
6. `crons`: compare the signed-file digest, atomically publish only changed Postgres truth, wait for CLI reconciliation, remove a
   legacy duplicate only after a matching signed job exists. Matching happens
   inside the replica, including payload and scheduling parameters.
7. `verify`: compare private normalized content digests per declaration key
   against **current canonical Postgres**, including schedule/timezone, payload,
   delivery mode/channel/to/account/thread and enabled state. Check two CLI polls
   25 seconds apart, stable IDs, health and the last five minutes of console
   logs for fs-safe/SQLite/config/proxy/signature errors. Persist counts only.
   An unavailable or saturated log window fails closed. Re-read canonical truth
   at the end, including row versions/ownership and time-filtered desired state.
   If it changed, repeat the comparison once; repeated changes fail with
   `canonical_changed_during_verification`. Stable content mismatch fails with
   `canonical_content_mismatch`. Automatic reconciliation remains enabled.

A successful record is `PASS`. Already-9.4 tenants without a partial migration
receive read-only verification of their current Azure/DB image identity, applied
config, signed cron set/stable IDs, health and console window, and report
"Already on 9.4; verification PASS". Dry-run skips those remote checks. A failed
check stops the batch without changing their runtime or migration record. This
is not a repair command for undocumented image-only upgrades.
An existing incomplete record always takes precedence over matching image tags.
A retry reuses the saved revision rather than creating a second revision.
Every non-PASS resume runs verification again, even when the historical
`completed` list contains `verify`; only mutation steps are resumable checkpoints.

Before any initial checkpoint/import, and again immediately before image
submission, the migration checks **all** runtime and canonical declarations
under **DEFAULT-DENY** against the unchanged `share_cron_sync` selector and `nbhd-cron-sync.mjs` writer.
Unsupported delivery destinations/accounts/threads, authored anchors, six-field cron,
authored stagger, systemEvent declarations (the unchanged writer emits the CLI-rejected
`--no-deliver` combination), thinking/pacing/session/retention controls, disabled declarations,
unmanaged recurrences and unknown fields block the tenant. Source cancellations
that would require creating an unprojectable disabled declaration also block.
No migration-only selector exemptions or agent re-sync waiver remain.
`cron-normalization.json` is the shared Python/JavaScript normalization contract.
`cron-proven-shapes.json` admits only exact normalized control combinations with
committed golden evidence of equal semantic digests, unchanged-writer acceptance,
and two mutation-free reconciliation passes with stable IDs. Message text,
description text and validated schedule values occupy typed data slots; model,
tool-list values/order, fallback values, timeout, flags and enums remain literal.
Unrecorded combinations return `BLOCKED_UNSUPPORTED` / `unproven_shape` even when
individual controls have separate evidence. Never infer a cross-product of support.

Initial `BLOCKED_UNSUPPORTED` and `DEFER` results are non-mutating. A retry can
also stop on a newly incompatible declaration; already-completed work remains
intact. Resolve compatibility in a separately approved change, then report again.
This tool never widens the deployed writer's capabilities.

## Read migration records safely

`Tenant.openclaw_migration` contains `status`, `step`, `completed`, per-step
start/completion timestamps, evidence, source cron export and failure metadata.
It is private operational data, not a subscriber-facing response. Print only
selected metadata, never the full JSON (cron payloads are private):

```python
from apps.tenants.models import Tenant
r = Tenant.objects.get(pk=TENANT_ID).openclaw_migration
print({k: r.get(k) for k in ("tag", "status", "step", "completed", "updated_at", "owner", "owner_token", "lease_until", "failure")})
print("Captured jobs:", len(r.get("cron_export", [])))
```

`RUNNING` claims have a renewable **10-minute diagnostic lease** and a unique
`owner_token`. **Expiry never authorizes automatic takeover.** Hard death (SIGKILL,
host loss, killed container exec) leaves the record RUNNING, not FAILED, and leaves
an active cutover fence in place. `--report` returns RUNNING, owner token, seconds
since the last lease renewal, and lease-expired status without runtime/Azure calls.

All checkpoints compare ownership, including the stale-connection retry. A stale
owner cannot save or start a later mutation; the atomic publisher rechecks ownership
before rename. External writes cannot be revoked by a DB token alone, so takeover
requires the exact token **and** an explicit attestation that the owner is dead:

```bash
python manage.py migrate_tenant_openclaw --tenant "$TENANT_ID" --tag "$TAG" \
  --takeover "$CONFIRMED_DEAD_OWNER_TOKEN" --confirm-owner-dead
```

Both flags are required, even when the lease expired or is missing. Identify the
recorded host/PID and verify the process is gone on that host (including its start
time, to avoid PID reuse). For container exec, verify that exact exec session and
its process are gone, or that its containing replica is terminated. If that cannot
be established, do not take over. Let already-submitted Azure image/storage requests
settle before proceeding; a closed local terminal alone is not termination evidence. Never infer termination from health failure or console timeout. The claim
compares the token again under a row lock, installs a new fencing token and records
the attested takeover. A mismatched token is refused. Recovery accepts one tenant,
reuses the saved revision and verifies afresh; it performs no image-only rollback.
The already-staged signed file makes cron installation independent of this retry.
`FAILED` resumes immediately from its failed checkpoint. A different target tag
is refused. The first failure stops an explicit batch; later tenants are untouched.

## Verification and expansion

The automated command is an infrastructure gate. The orchestrator must also:

- Confirm Azure image/digest and DB tag/version agree, storage/env/mount options
  are present, and signing-key bindings match. No provider/BYO or Brave key was
  silently replaced; secret references are preserved by the revision update.
- Send a synthetic chat turn and run a **Brave search**; verify actual responses.
- Make a controlled scheduled delivery, check no duplicates, then test
  hibernate → wake → delivery. Lifecycle behavior is unchanged from main;
  croner/croniter semantics, HTTP suspend and idle guards remain a separate
  follow-up and must be assessed by the release orchestrator.
- Review any one-shots whose fire time elapsed
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
  Postgres rows and duplicate metadata. Unknown
  legacy jobs are never deleted by name alone. Fix the mismatch, then resume.
- If old runtime recovery is unavoidable, stop the tenant under orchestrator
  control; independently identify its old binary version (especially bare SHA),
  recover the old compatible config/workspace from authoritative sources, restore
  the saved storage template/secret references and old image, restore matching DB
  version/tag, and restore cron truth over the old supported transport. Review
  resurrected SMB cron state and duplicates **before** reactivating deliveries.
  There is no implemented inverse migration guaranteeing this recovery.

## Runtime-backed cron contract

The cron contract is recorded from the real fleet image
`nbhdunited.azurecr.io/nbhd-openclaw:2026.9.4-8ceb89f`, manifest
`sha256:c244ff686863c1e9e4fc9fc55d1fac3b6a746e76039a9632db8ae082a0f21ddc`.
See [raw fixtures and capture instructions](../../apps/orchestrator/fixtures/openclaw_94_cron_contract/README.md).
The local capture used `--network none`, no ports/mounts, a throwaway token,
plugins and scheduled firing disabled, and `NODE_OPTIONS=` for every CLI call.
The image writer's SHA256 matched the unchanged repository writer.

- List rows include `configRevision`, `effectiveAgentId`, timestamps/state and
  a generated `scheduledToolPolicy: {version: 1, mode: "trusted"}`. Known
  observations are ignored. Only that exactly reproduced tool-policy default
  is accepted; other policies and unknown definition fields still block.
- Five-field cron/timezone and unpinned intervals are accepted. Top-of-hour
  cron acquires `staggerMs: 300000`; intervals acquire `anchorMs`. Verify-only
  ignores those generated fields only with an exact canonical declaration-key
  match that leaves them unpinned. Source timing requirements still block.
- ISO offset and UTC one-shots both become UTC with millisecond precision;
  `tz` disappears. The migration compares instants and prepares canonical
  one-shots in this representation during capture **before image submission**,
  then again before signing. Preparation derives the instant only from authored
  `schedule.at`, `atMs`, or `expr`; conflicting instants are refused. Runtime
  `state.nextRunAtMs` cannot supply or replace it. A rendered declaration digest
  assertion aborts/rolls back preparation if semantics change. The selector and runtime writer remain unchanged.
  Both prepared forms passed two real writer reconciliation passes with zero
  mutations and stable IDs. Empty optional delivery values equal absence.
- Explicit destinations/accounts/threads and interval anchors are silently
  dropped by the writer; disabled rows are not selected (a direct writer probe
  creates an enabled job). These remain pre-cutover refusals.
- Both systemEvent target declarations are rejected with the writer's
  `--no-deliver`. They block before any capture/checkpoint/image mutation.
- The runtime automatically lists heartbeat and skill-collection-review
  monitor rows. Real CLI probes refuse both creating jobs in their reserved
  namespaces and removing those rows. The 9.4 operator inventory excludes
  those exact namespace/agent pairs from migration ownership; unknown legacy
  declarations remain visible and block. No monitor is deleted or recreated.

Round six adds [75 captures and the final shape matrix](../../apps/orchestrator/fixtures/openclaw_94_cron_contract/round_six/README.md):

| Shape family | Result |
|---|---|
| Ordinary five-field cron, unpinned every, authored ISO/numeric/atMs/expr at | ACCEPT; one-shots prepared before signing |
| All six typed patterns × cron/every/at × with/without recorded fallbacks | ACCEPT for the exact recorded control combinations |
| Recorded model, restricted tools, lightContext, timeout, wakeMode, description, main agent and text alias | ACCEPT for recorded combinations/values |
| Destination-free announce (default/Telegram channel), empty delivery defaults, nullable optionals | ACCEPT with the shared evidenced normalization |
| Disabled, systemEvent, authored anchor/stagger, explicit destinations/accounts/threads | BLOCKED_UNSUPPORTED |
| Unknown fields/policies, unsupported controls, unrecorded values or combinations | BLOCKED_UNSUPPORTED (`unproven_shape` for an otherwise valid unrecorded shape) |

The 70 accepted cases map to 43 normalized shapes; five captured cases are blocked.
Null `model`, `toolsAllow`, `lightContext`, `timeoutSeconds`, `fallbacks` and the
recorded optional fields equal absence. Literal null timing pins remain unpinned.

Round seven adds [the pre-staged boot capture](../../apps/orchestrator/fixtures/openclaw_94_cron_contract/prestaged-boot.json).
The exact image above ran its unchanged default entrypoint with a signed recurring
job and one-shot copied in **before container start**. Both appeared with matching
canonical digests and stable IDs in two CLI polls, without any manual cron add or
reconcile call. The entrypoint and writer hashes match `runtime/`. Reproduce using
`scripts/capture_openclaw_94_prestaged_boot.py` locally; it enforces `--network none`,
no ports/mounts, synthetic credentials, disabled scheduled firing/plugins, and cleans
only its own container. The crash-after-image-apply database test separately proves
the checkpoint survives process death and fenced resume completes on the same revision.
This proves installation and recovery ordering, not real reminder delivery.

Scalar types are checked before normalization. Python uses a type-tagged JSON tree,
so booleans, integers and floats are distinct; generated trusted-policy version must
be an actual Python integer. JS checks booleans strictly and uses `Number.isSafeInteger`
for integer controls (`Number.isInteger` for policy version). JavaScript JSON parsing
cannot distinguish the numeric spellings `1` and `1.0`; both are Numbers, but neither
can stand in for a boolean. Python rejects float controls before submission. Runtime
verify-only evaluates exact model/agent/flag combinations inside the replica and
exports only fixed reason codes and hashed identifiers, never redacted stand-in controls.

These fixtures test CLI acceptance, projection and comparison. They do not
establish production delivery or lifecycle behavior; the release canary checks
below remain required.

## Review-round safety checks and read-only verification

For an already-9.4 tenant, use `--verify-only` (in the approved management
context, by the release orchestrator):

```sh
python manage.py migrate_tenant_openclaw --tenant <uuid> --verify-only
```

This performs no tenant, image, config, cron, or migration-record writes.
It also inspects current canonical and runtime declarations for fields the
unchanged image cannot preserve. Unsupported declarations report
`BLOCKED_UNSUPPORTED`, reason counts and canonical keys/hashed runtime identifiers (no names
or payloads), then exit nonzero. Otherwise output is `PASS verified` or
`FAIL <reason_code>`; failure exits nonzero and stops
the explicit batch. It checks the tenant's existing image, regardless of the
configured fleet target. Use this for the MJ and `1c77c8c1` health checks.

- ACR digest resolution uses the Django container's **system-assigned managed
  identity**, requiring AcrPull on `nbhdunited`. It uses the ACR token exchange
  and manifest HEAD APIs, with no Azure CLI dependency or token logging.
- Every capture retry and the final pre-image check read live HTTP truth again.
  Earlier exports remain in private `cron_export_history`; only unchanged
  import-owned rows are refreshed. Newer canonical edits and dashboard deletions
  win over stale runtime exports; retries never resurrect deleted imported rows.
- Enabled canonical Postgres **and** runtime one-shots running, lacking a parseable due time, or due
  within 60 minutes defer cutover (`cron_running`, `cron_next_fire_unknown`,
  `cron_imminent`). Eligibility runs before import, enabled/time filtering or canonical changes.
- Recurring jobs are not paused and do not trigger the imminence guard. A fire
  during the approximately five-minute cutover may be skipped; missed recurring
  fires are not replayed. Image/config failures can extend this interruption.
- Unsupported declarations are refused **before image submission**, with
  payload-free reason codes and counts. Verification is an additional check,
  never a substitute for the preservation precheck.
- Every captured and canonical one-shot is audited, including historical exports
  and canonical obligations that later expire during cutover. Historical source
  resolutions are hints only: acceptance rechecks both current authorities. Absence
  from both current Postgres truth (including deleted/tombstoned rows) and the
  live runtime at verification is `cancelled`, with an observation timestamp.
  A remaining canonical row with missing runtime delivery still fails as
  `one_shot_pending_missing` or `one_shot_expired_undelivered`. Never infer delivery
  from absence or silently mark a missing canonical reminder delivered.

## Operational logging

Migration source/report/capture calls opt into `invoke_gateway_tool(...,
metadata_only=True)`. HTTP/tool/transport/Key Vault failures use reason codes and
status metadata; response bodies and raw exceptions are withheld. Other gateway
callers retain their prior logging behavior. Strict config refresh also requests
metadata-only envelope-render errors and propagates the opt-in through
`render_workspace_files`, SOUL/AGENTS loaders and nested Key Vault reads. Cold-cache
Key Vault failures and outer loader exceptions withhold private error text;
shared callers retain their previous defaults. Manual canary failures and image/version
command summaries contain fixed reason codes. Local capture progress prints
case/status/reason only; its committed raw CLI recordings contain synthetic data.
The unchanged runtime poller's separate logging limitation below still applies.

## Scope and lifecycle limitations

This PR changes no OpenClaw image files. Automatic image tasks, message updates,
wake, suspension/resume, runtime capture and delayed restore retain main behavior.
Keep the image rollout allowlist empty during migration. The storage retrofit
is explicitly enabled only by the migration, never ordinary image updates.

Hibernation, reconciliation logic, signed selection, image tasks, router image
updates, suspension and runtime image files match main. The Round 8 shared write
guard refuses cron edits and reconciliation writes for the migrating tenant from
pre-staging through version commit. Absent/PASS records follow main behavior.
After version commit, concurrent canonical edits are allowed and verification
checks current truth; active-record signed publication remains crash-safe.

Manual tools require an explicit non-empty UUID scope. Both image-bump and
version-bump commands take `--tenant UUID` or `--tenants UUID,UUID`.
`bump_openclaw_version --all` filters only within that supplied scope; `--all`
alone is refused. Every selected tenant, including single-tenant mode and
same-tag partial upgrades, must pass the runtime-family guard before mutation.
A non-PASS migration with `image_submitted=True` refuses ordinary manual updates,
even if both DB version fields still say 5.28. The guard reads the live Azure
OpenClaw image and requires an unambiguous same-family tag (optionally followed
by a validated `@sha256:<64 lowercase hex>` digest); unreadable identities,
digest-only references and mismatches refuse. Resolve partial migrations using
the controlled migration recovery procedure.
`canary_tenant_image --container ...` resolves that container to exactly one
tenant and applies the same family/ambiguity guard. Unversioned canary tags,
unknown/duplicate container mappings and alternate repositories are refused.
Both rollout endpoints accept `tenant_id` or a non-empty `tenant_ids` JSON list.
Missing, malformed, empty, unknown or mixed-family scope returns HTTP 400 before
command invocation/publication. Use the migration for 5.x/bare-SHA → 9.x.

Follow-ups, outside this PR:
- 9.4 lifecycle (croner vs croniter semantics, suspend, idle guards).
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
# Confirm 0168/0169 are applied, and command help includes --verify-only and --report.
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
`cron_export_history`, `canonical_one_shots`, and `quarantined_cache` while retaining metadata,
dispositions and audit timestamps; apply the retention policy to backups too.


## Round 8 cron edit fence and signed-file publication

Apply schema migration `tenants.0170_openclaw_migration_cron_fence` before running
this command. It also fences older non-PASS pre-staged records whose version has
not committed; recovery reasserts the fence on an already-submitted revision. The central `nbhd_migration_cron_guard` reads the indexed tenant key
and indexed `openclaw_migration_cron_fenced` field; it refuses only when the private
record exists, is non-PASS, and its cutover fence is set. The CronJob database trigger
covers insert/update/delete, ORM saves, bulk operations and raw SQL. The same guard
surrounds gateway mutations, signed-file reconciliation and queued cron proposals.
It holds the tenant row lock through admitted transport writes, so pre-staging
cannot race a writer that passed the check earlier. No owner bypass permits cron
edits under the fence. Failed and hard-dead migrations retain it until version commit.

Dashboard/API callers receive HTTP 409 with `{"error":"assistant_updating","retry_after":60}`.
Assistant runtime callers also receive a friendly `detail` asking them to retry in
one minute. The HTTP adapter retains this result even if a legacy handler catches
the underlying refusal. Read-only cron tools remain available; a read path which
also updates canonical cache rows is refused at its write boundary. Absent/PASS
migration records retain main's database and transport behavior, tested explicitly.
Unrelated tenants remain editable. The deployed runtime and automatic image/wake
paths remain unchanged.

For active migrations, signed publication compares SHA256 first and skips identical
bytes. Changed bytes go to a unique sibling `nbhd-crons.json.migration-<uuid>.tmp`,
are read back, then published with Azure Files `rename_file(..., overwrite=True)`.
See the [Azure SDK rename contract](https://learn.microsoft.com/en-us/python/api/azure-storage-file-share/azure.storage.fileshare.sharefileclient?view=azure-python#azure-storage-fileshare-sharefileclient-rename-file).
There is no in-place fallback: upload/readback failure preserves the previous valid
file; successful rename exposes the completed replacement. Hard death may leave an
inert unique temp file. After confirming the owner is dead and stopping concurrent
reconciliation writers, an operator may remove abandoned temp files by exact name;
never delete the live signed file.
This applies to migration steps and reconciliation while the migration is active.
After PASS, ordinary publication follows main. Python declaration integers now
require JavaScript safe-integer bounds before shape matching; unproven values
remain DEFAULT-DENY.
