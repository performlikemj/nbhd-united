# PII W4 historical-row migration runbook

This runbook is operator-only. The migration is dry-run by default, acts only on
the stores and paths in `apps/pii/store_registry.py`, and must never be fired in
production without an explicit MJ gate. Production has no container exec, so
the production surface is the signed QStash trigger below.

## Non-negotiable preflight

1. Verify Supabase PITR/backup state from the Management API. Use a token with
   `backups_read`; do not paste the response into application logs.

   ```bash
   export SUPABASE_PROJECT_REF='dljqtpunnobyztampxus'
   export SUPABASE_ACCESS_TOKEN='set-in-operator-shell'
   curl -fsS \
     -H "Authorization: Bearer ${SUPABASE_ACCESS_TOKEN}" \
     "https://api.supabase.com/v1/projects/${SUPABASE_PROJECT_REF}/database/backups" \
     | jq '{pitr_enabled,walg_enabled,backups,physical_backup_data}'
   ```

   Confirm `pitr_enabled=true`, `walg_enabled=true`, at least one physical
   backup has `status="COMPLETED"`, and
   `physical_backup_data.latest_physical_backup_date_unix` is at or after the
   latest production write (or, during a quiet database, that the displayed
   latest recovery point represents current state). Record the backup id and
   earliest/latest recovery timestamps outside application logs. Stop if any
   field is absent or stale; do not treat a successful `curl` as proof of a
   restorable point.
2. Confirm `layer1_placeholder_writes` is enabled for the selected tenant. Live
   writes must not reintroduce legacy raw rows while history is being migrated.
   Commit mode also refuses the tenant at engine level when this flag is off.
   Add only the approved tenant UUID to the Container App setting
   `W4_MIGRATION_TENANT_IDS`; empty means no commit tenant. Migration dry-runs
   are deliberately allowlist-ungated because they are read-only. Both the
   driver and every batch re-check the allowlist when they fire, so an old or
   stray commit publish is inert after the gate is removed.
3. Run the deterministic `pii_junk_sweep` to completion, then finish the
   tenant's PERSON/LOCATION entity-map review queue (`GET
   /api/v1/tenants/settings/pii-review-queue/`; keep confirmed bindings and
   clean junk through the existing bulk endpoint with `deny=true`). Re-run the
   sweep and confirm `errors=0`. This is the P1-3 preflight: migrating against
   known junk would mint/reuse bad identity bindings and make the rewrite harder
   to interpret.
4. Run the receipt-demotion dry-run and approved commit described below. Its
   deploy cutoff is the actual production deployment time of commit `d24cf4b5`;
   it is a required operator parameter and must not be inferred from Git time.
5. Let `placeholder_repair_sweep` drain after receipt demotion. The W4 driver also checks each store:
   any `unconfirmed` or `residual` receipt causes that store to emit
   `state=repair_pending_skipped` and be skipped while later stores continue.
   `terminal` is deliberately not repair-eligible and is migrated by W4.
6. Confirm no historical encrypted-column backfill has run ahead of W4. W4
   rewrites the registry's plaintext fields; it does not rewrite encrypted
   sidecars. The directive ordering is W4 first, encryption backfill second.
7. Run off-peak. A batch performs local full NER before a short tenant-map lock;
   the default batch is 25 rows and the hard maximum is 100.
8. Export the production API base URL and QStash token in the operator shell.
   Never echo either value and never use shell tracing (`set -x`).

```bash
export W4_API_BASE_URL='https://api.example.invalid'
export QSTASH_TOKEN='set-in-operator-shell'
```

## Required lying-receipt demotion preflight

Set `W4_DEPLOY_CUTOFF` to the recorded timezone-aware production deployment
time of `d24cf4b5` (example shape only: `2026-08-08T00:00:00Z`). The task rejects
a missing or timezone-naive cutoff. It scans only flag-on tenants and demotes a
`state=placeholder` receipt to `unconfirmed` when either (a) its writer is
`runtime` or `background` and the row's `updated_at` (falling back to
`created_at`) predates the cutoff, or (b) a registered non-`**` JSON field
exposes zero selected string leaves. Stores with neither time column are named
as `time_discriminator_missing_skipped` and skipped; reports are counts-only.

```bash
export W4_DEPLOY_CUTOFF='REQUIRED_ACTUAL_D24CF4B5_DEPLOY_TIME'
curl -fsS -X POST \
  "https://qstash.upstash.io/v2/publish/${W4_API_BASE_URL}/api/cron/trigger/w4_receipt_demotion/" \
  -H "Authorization: Bearer ${QSTASH_TOKEN}" \
  -H 'Content-Type: application/json' \
  -H 'Upstash-Retries: 3' \
  --data "{\"args\":[\"TENANT_UUID\",\"${W4_DEPLOY_CUTOFF}\"],\"kwargs\":{\"commit\":false,\"batch_size\":25}}"
```

Reconcile `w4_receipt_demotion_report` for every store. After explicit approval,
repeat the exact call with JSON boolean `"commit":true`, then drain
`placeholder_repair_sweep` before starting the historical migration dry-run.
The commit task uses the same `W4_MIGRATION_TENANT_IDS` allowlist as W4.

## Exact QStash dry-run invocation

Replace only `TENANT_UUID`. The JSON boolean `false` is explicit even though it
is the default.

```bash
curl -fsS -X POST \
  "https://qstash.upstash.io/v2/publish/${W4_API_BASE_URL}/api/cron/trigger/historical_placeholder_migration_driver/" \
  -H "Authorization: Bearer ${QSTASH_TOKEN}" \
  -H 'Content-Type: application/json' \
  -H 'Upstash-Retries: 3' \
  --data '{"args":["TENANT_UUID"],"kwargs":{"commit":false,"batch_size":25}}'
```

The driver chains exactly one registered store at a time. Each store's batch
task chains bounded batches until complete, then returns to the driver for the
next store. Do not publish the batch task manually during an ordinary run.

## Reading the dry-run

Query logs by the literal prefix `w4_migration_report`. Every report is
counts-only and has this stable shape:

```text
w4_migration_report tenant=… store=… field=… state=… rows=…
```

Interpret states using `docs/pii-w4-residuals-ledger.md`. Before approval:

- every registered store must either complete or have an understood
  `repair_pending_skipped` report;
- re-drive after repairs until `repair_pending_skipped` is absent;
- investigate and re-drive any `changed_skipped` row during a quieter window;
- `placeholder` means already migrated/current and is expected;
- `absent`, `bypass`, `unconfirmed`, `residual`, and `terminal` are all migration
  candidates. None is a fence; only `placeholder` is fenced out.

`changed_skipped` counts attempts, not unique rows. The QStash chain retries the
same cursor after 30 seconds; the management command retries it immediately in
the current process. Both cap re-chaining at three attempts. If the row still changes, they emit
`changed_skipped_advanced`, leave that row untouched, advance the cursor, and
continue so one hot row cannot stall a tenant indefinitely.

## Live DLQ and stall watch

While either preflight or migration is active, run both checks every five
minutes. Stop publishing immediately on any matching DLQ entry, any
`state=error`, or no new W4 batch/report line for 20 minutes (the lease is 15
minutes, so 20 minutes is the first genuine stall threshold).

```bash
curl -fsS -G 'https://qstash.upstash.io/v2/dlq' \
  -H "Authorization: Bearer ${QSTASH_TOKEN}" \
  --data-urlencode 'count=100' \
  | jq '[.messages[] | select(.url | contains("historical_placeholder_migration") or contains("w4_receipt_demotion")) | {messageId,url,responseStatus,createdAt}]'

az monitor log-analytics query \
  --workspace 035a49db-1da5-452d-8b32-b074d7a5d606 \
  --analytics-query "ContainerAppConsoleLogs_CL | where ContainerAppName_s == 'nbhd-django-westus2' | where TimeGenerated > ago(30m) | where Log_s contains 'w4_migration' or Log_s contains 'w4_receipt_demotion' | project TimeGenerated, Log_s | order by TimeGenerated desc" \
  -o table
```

The QStash result must be `[]`. In the log result, grep visually for
`state=error`, `changed_skipped`, `changed_skipped_advanced`,
`repair_pending_skipped`, and `authoring_unconfirmed`; confirm timestamps keep
advancing at least once per 20-minute window. Do not retry a DLQ item until its
report/cursor state and root cause are reconciled.

## Canary → Kiho → fleet ladder

No rung authorizes the next one automatically.

1. **Canary dry-run.** Fire the dry-run invocation for `<CANARY_TENANT_UUID>`.
   Reconcile every store report and the residuals ledger.
2. **MJ gate 1.** Obtain explicit MJ approval for canary commit. Record the
   approved tenant UUID and backup identifier in the private ops record.
3. **Canary commit.** Fire the commit invocation below for the canary. Verify
   owner reads rehydrate, runtime reads remain placeholder-space, receipts say
   `state=placeholder`, `writer=owner`, `migrated=true`, and repair stays drained.
4. **MJ gate 2.** Obtain explicit MJ approval for Kiho.
5. **Kiho dry-run then commit.** Repeat the complete dry-run/reconcile/commit/
   verify cycle for `<KIHO_TENANT_UUID>` during an off-peak window.
6. **MJ gate 3.** Obtain explicit MJ fleet approval. Freeze the exact active and
   suspended tenant UUID list in a private operator file; do not discover or
   broaden the audience inside a task.
7. **Fleet.** For each frozen tenant, dry-run and reconcile first, then publish
   commit one tenant at a time. Pause on any new state, repair skip, error-rate
   alert, or owner/runtime read regression.

## Exact QStash commit invocation

This is the only difference from dry-run: `commit` is the JSON boolean `true`.

```bash
curl -fsS -X POST \
  "https://qstash.upstash.io/v2/publish/${W4_API_BASE_URL}/api/cron/trigger/historical_placeholder_migration_driver/" \
  -H "Authorization: Bearer ${QSTASH_TOKEN}" \
  -H 'Content-Type: application/json' \
  -H 'Upstash-Retries: 3' \
  --data '{"args":["TENANT_UUID"],"kwargs":{"commit":true,"batch_size":25}}'
```

For an MJ-approved frozen fleet file, use this exact one-at-a-time publisher.
The one-second spacing controls publish pressure; store execution remains
sequential inside each tenant.

```bash
while IFS= read -r W4_TENANT_ID; do
  test -n "${W4_TENANT_ID}" || continue
  curl -fsS -X POST \
    "https://qstash.upstash.io/v2/publish/${W4_API_BASE_URL}/api/cron/trigger/historical_placeholder_migration_driver/" \
    -H "Authorization: Bearer ${QSTASH_TOKEN}" \
    -H 'Content-Type: application/json' \
    -H 'Upstash-Retries: 3' \
    --data "{\"args\":[\"${W4_TENANT_ID}\"],\"kwargs\":{\"commit\":true,\"batch_size\":25}}"
  sleep 1
done < approved-w4-tenant-ids.txt
```

## Resume after interruption

Re-publish the same driver invocation with the same tenant and mode. The
per-(tenant, store, mode) cursor resumes after the last completed primary key.
A worker killed after rewriting rows but before advancing its cursor safely
re-scans them: `state=placeholder` prevents a second rewrite, and canonical map
dedup prevents a second mint. A 15-minute lease prevents overlapping retries;
an abandoned lease expires automatically.

An abort after pre-scan can leave an orphan identity binding: the tenant map may
have minted a candidate before the corresponding content-row CAS lost or the
worker died. Do not delete it ad hoc. Record the counts, resume/re-drive the row,
then let the normal entity-map review plus junk retirement path decide whether
the unused binding is real or junk.

A previously repair-skipped store is reset to pending only by a fresh driver
invocation, so re-driving after the repair sweep is the intended recovery.
Never use `--reset` merely to resume. Reset exists on the CI/local management
command for deliberate report regeneration and should be treated as an
operator decision.

## CI/local management command

The command uses the identical batch engine and defaults to dry-run:

```bash
python manage.py migrate_placeholder_history --tenant-id TENANT_UUID
python manage.py migrate_placeholder_history --tenant-id TENANT_UUID --store journal.Task
python manage.py migrate_placeholder_history --tenant-id TENANT_UUID --commit
python manage.py w4_receipt_demotion --tenant-id TENANT_UUID --deploy-cutoff "$W4_DEPLOY_CUTOFF"
python manage.py w4_receipt_demotion --tenant-id TENANT_UUID --deploy-cutoff "$W4_DEPLOY_CUTOFF" --commit
```

Use `--reset` only to discard that mode's cursor and rescan from the first PK.
Dry-run and commit have separate cursors.

## Accepted W1c receipt-value residual

The bounded W1c canary wrote some receipt `redactions[]` entries with embedded
real `value` strings. W2 stopped creating that shape, but W4 does not scrub old
receipt payloads. Fields fenced by `state=placeholder` therefore keep those
embedded values at rest permanently; receipt demotion changes only state/reason
and intentionally preserves the rest of the receipt. This named residual is
bounded to W1c canary volume and accepted for this migration. A future receipt
payload scrub is a separate migration, not an implicit W4 side effect.

## Rollback truth

There is **no un-migrate**. Commit rewrites historical text to placeholders and
mints tenant identity bindings; reversing those substitutions in place is not a
safe or complete rollback. The rollback is **restore the verified pre-run
Postgres backup**. Stop further QStash publishes, preserve logs/DLQ metadata,
restore the backup, and re-verify live-write flags and read behavior before
resuming traffic.
