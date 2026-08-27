# Provisional PII bindings

Single-token PERSON and LOCATION values first seen at explicitly identified owner-chat ingress can be provisional. While active they use the same placeholder, masking, rehydration, reply, receipt, and transcript paths as permanent bindings. Three distinct provider events across at least two tenant-local dates promote the binding; owner **Keep** promotes immediately.

The accepted privacy trade-off is explicit: a weak detector guess is still stored in `pii_entity_map`, retained for historical rehydration after retirement, and masks tenant-wide until expiry. The short fuse fixes permanence, not that temporary storage window: stale junk stops rewriting future text after at most the configured TTL.

## Configuration

- `PII_PROVISIONAL_TENANT_IDS`: comma-separated tenant UUIDs allowed to create new provisional bindings. Empty disables creation. Removing a tenant does not disable processing of lifecycle state already stored.
- `PII_PROVISIONAL_SWEEP_ENABLED`: independently enables the hourly expiry task. Default is false.
- `PII_PROVISIONAL_TTL_HOURS`: positive integer, default `72`. Zero and invalid values fail settings startup.

No lifecycle migration exists. Legacy object and bare-string entries remain permanent because they have no trustworthy creation timestamp.

## Operations

The hourly QStash schedule is `expire-provisional-bindings` at minute 17. It considers every active tenant independently, orders eligible entries by `last_seen_at`, and processes a bounded batch. Every expiry is re-read under the tenant row lock; a concurrent sighting that wins first slides the fuse, while a sighting after expiry reactivates the same placeholder unless denylist, owner retirement, or the global stoplist/junk rules block it.

Useful commands:

```sh
python manage.py expire_provisional_bindings --dry-run
python manage.py expire_provisional_bindings --report
python manage.py expire_provisional_bindings --promote-all --dry-run
python manage.py expire_provisional_bindings --promote-all
python manage.py expire_provisional_bindings --retire-all --dry-run
python manage.py expire_provisional_bindings --retire-all
```

`--report` is a heuristic inventory of legacy single-token PERSON/LOCATION candidates that are unreviewed and not denylisted. It makes no age claim and prints tenant plus placeholder only, keeping raw values out of logs. Any legacy bulk decision remains an operator action.

Telemetry is content-free:

- `pii_policy_mint` with permanent/provisional outcome
- `pii_policy_recurrence` with counts and promotion state
- `pii_policy_expire`
- `pii_policy_reactivate`
- `pii_policy_owner_action` for keep, stop-hiding, and always-hide

These events never include raw values, placeholders, provider event IDs, or event digests.

## Rollout

1. Deploy with both gates off.
2. Set `PII_PROVISIONAL_TENANT_IDS` to the canary tenant UUID.
3. Enable `PII_PROVISIONAL_SWEEP_ENABLED=true` once the hourly task is registered.
4. Observe mint outcomes, recurrence promotions, expirations, reactivations, and active PERSON/LOCATION totals for one week.
5. Run the report for manual legacy review, then expand the creation allowlist only with operator approval.

## Rollback

Order is load-bearing:

1. Disable creation by clearing `PII_PROVISIONAL_TENANT_IDS`.
2. Leave the sweep enabled long enough to drain remaining provisionals, or explicitly choose `--promote-all` or `--retire-all` after reviewing the corresponding dry run.
3. Unregister the `expire-provisional-bindings` cron.
4. Only then roll back the code.

If creation is off while live provisionals remain, they continue masking like permanent bindings until the independent sweep processes them. This is the safe failure mode.
