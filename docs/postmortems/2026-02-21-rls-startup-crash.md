# Post-Mortem: RLS Disable Command Crash Loop

**Date:** 2026-02-21  
**Severity:** High (container crash loop, API unavailable)  
**Duration:** ~3 deploys over ~1 hour  
**Impact:** Telegram link generation broken, constellation endpoint returning 404

## Summary

Supabase re-enables Row-Level Security (RLS) on new tables created by Django migrations. A management command (`disable_rls`) was added to `startup.sh` to automatically disable RLS after each migration. The command failed on a Supabase-owned internal table, causing a container crash loop.

## Timeline

1. **RLS blocking writes** — Telegram link generation failing with `InsufficientPrivilege: new row violates row-level security policy`. A user hit this error.
2. **Deploy 1** — Added `disable_rls` management command. It queried all tables with RLS enabled and ran `ALTER TABLE ... DISABLE ROW LEVEL SECURITY`. Failed on `saml_relay_states` (Supabase internal table not owned by our DB user).
3. **Deploy 2** — Wrapped each `ALTER TABLE` in a savepoint to catch permission errors. The `ROLLBACK TO SAVEPOINT` itself failed, crashing the command. Since `startup.sh` uses `set -e`, this killed the container before gunicorn started → crash loop.
4. **Deploy 3** — Added `AND tableowner = current_user` to the SQL query. No exception handling needed — simply doesn't select tables it can't modify. Clean fix.

## Root Cause

Two compounding issues:

1. **Supabase enables RLS by default** on all new tables, including those created by Django migrations. Our application connects as a non-superuser that can't bypass RLS policies.
2. **Shared database contains tables owned by other roles** (e.g. `saml_relay_states` owned by Supabase auth). The initial query didn't filter by ownership.

## What Went Wrong

- The `disable_rls` command was not tested against the actual production database schema before deploying.
- `startup.sh` uses `set -e`, meaning any command failure prevents gunicorn from starting.
- The savepoint recovery approach (Deploy 2) introduced a worse failure mode than the original problem.

## Lessons Learned

1. **Filter the problem out of the query, don't catch it.** `AND tableowner = current_user` is simpler and more reliable than try/catch with savepoints.
2. **`set -e` in startup scripts means every command must be bulletproof.** Non-critical commands should use `|| true` to prevent container crash loops.
3. **Shared databases have shared tables.** Always account for tables you didn't create when writing broad DDL operations.
4. **PostgreSQL transaction state after errors is tricky.** Savepoints inside `except` blocks can fail if the transaction is already aborted. Avoid using exceptions for flow control in database operations.
5. **Test DDL commands against production-like environments.** A local PostgreSQL instance won't have Supabase's internal tables.

## CI Issues Fixed (Pre-existing)

These were broken before this incident but blocked deployment:

- **pgvector missing in CI** — Swapped `postgres:16-alpine` → `pgvector/pgvector:pg16`
- **Smoke job using SQLite** — Added PostgreSQL service, made script respect `DATABASE_URL` from environment
- **Lesson test type mismatch** — Fixed int vs string comparison in serializer assertions
- **Clustering test threshold** — Test created fewer lessons than the minimum required for clustering

## Action Items

- [x] Fix `disable_rls` to filter by `tableowner = current_user`
- [x] Fix CI to use pgvector-enabled PostgreSQL image
- [x] Fix smoke job to use PostgreSQL instead of SQLite
- [ ] Add `|| true` to `disable_rls` call in `startup.sh` as a safety net
- [ ] Add CI status badge to README
