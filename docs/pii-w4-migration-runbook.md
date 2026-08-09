# PII W4 historical-row migration runbook

This runbook is operator-only. The migration is dry-run by default, acts only on
the stores and paths in `apps/pii/store_registry.py`, and must never be fired in
production without an explicit MJ gate. Production has no container exec, so
the production surface is the signed QStash trigger below.

## Non-negotiable preflight

1. Take and verify a restorable Postgres backup. Record its identifier outside
   application logs.
2. Confirm `layer1_placeholder_writes` is enabled for the selected tenant. Live
   writes must not reintroduce legacy raw rows while history is being migrated.
3. Let `placeholder_repair_sweep` drain. The W4 driver also checks each store:
   any `unconfirmed` or `residual` receipt causes that store to emit
   `state=repair_pending_skipped` and be skipped while later stores continue.
   `terminal` is deliberately not repair-eligible and is migrated by W4.
4. Confirm no historical encrypted-column backfill has run ahead of W4. W4
   rewrites the registry's plaintext fields; it does not rewrite encrypted
   sidecars. The directive ordering is W4 first, encryption backfill second.
5. Run off-peak. A batch performs local full NER before a short tenant-map lock;
   the default batch is 25 rows and the hard maximum is 100.
6. Export the production API base URL and QStash token in the operator shell.
   Never echo either value and never use shell tracing (`set -x`).

```bash
export W4_API_BASE_URL='https://api.example.invalid'
export QSTASH_TOKEN='set-in-operator-shell'
```

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
```

Use `--reset` only to discard that mode's cursor and rescan from the first PK.
Dry-run and commit have separate cursors.

## Rollback truth

There is **no un-migrate**. Commit rewrites historical text to placeholders and
mints tenant identity bindings; reversing those substitutions in place is not a
safe or complete rollback. The rollback is **restore the verified pre-run
Postgres backup**. Stop further QStash publishes, preserve logs/DLQ metadata,
restore the backup, and re-verify live-write flags and read behavior before
resuming traffic.
