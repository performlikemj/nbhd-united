# Row-Level Security & Tenant Isolation

## Overview

This project uses PostgreSQL Row-Level Security (RLS) to enforce tenant
isolation at the database level. Every table that stores tenant data has RLS
policies that restrict which rows a given database session can read, insert,
update, or delete.

RLS is a defence-in-depth measure. Even if application-layer bugs allow
cross-tenant queries to reach the database, Postgres itself prevents data
leakage.

## Threat Model

| Threat | Mitigation |
|--------|------------|
| Application bug leaks cross-tenant data | RLS policies filter rows by `app.tenant_id` |
| ORM query missing a tenant filter | Postgres rejects/hides rows that don't match the session context |
| Compromised request with a spoofed tenant | Auth middleware validates identity before setting RLS context |
| Background task accessing wrong tenant | Service-role flag grants explicit cross-tenant access only when needed |

## Database Roles

Two Postgres roles are used:

| Role | Purpose | RLS |
|------|---------|-----|
| `app_user` | Runtime application queries | **Enforced** — policies restrict every SELECT/INSERT/UPDATE/DELETE |
| `postgres` | Migrations, schema changes, management commands | **Bypassed** — superuser is exempt from RLS |

The application connects as `app_user` for all normal request handling. The
`postgres` role is used only during deployment (migrations) and manual
administrative tasks.

## How `set_rls_context()` Works

Before any tenant-scoped database query, the application calls
`set_rls_context()` (defined in `apps/tenants/middleware.py`) to set Postgres
session variables:

```python
set_rls_context(tenant_id=..., user_id=..., service_role=...)
```

This executes `SELECT set_config('app.tenant_id', '<uuid>', false)` (and
similarly for `app.user_id` and `app.service_role`). The third argument
(`false`) makes the variable **session-scoped** — it persists for the
lifetime of the database connection. Variables are explicitly cleared by
`reset_rls_context()` in middleware `process_response`. Django's default
`CONN_MAX_AGE=0` also closes connections after each request as a safety net.

RLS policies reference these variables via `current_setting('app.tenant_id')`.

### Session Variables

| Variable | Type | Set By | Purpose |
|----------|------|--------|---------|
| `app.tenant_id` | UUID string | Middleware / view auth | Restricts rows to a single tenant |
| `app.user_id` | UUID string | Middleware | Restricts rows to a single user (where applicable) |
| `app.service_role` | `'true'` or unset | Internal/cron auth | Grants cross-tenant access for background tasks |

## Authentication Paths

Three authentication paths set RLS context, each suited to a different caller:

### 1. JWT (Browser / Frontend)

- **Caller:** Authenticated users via the frontend.
- **Mechanism:** SimpleJWT validates the `Authorization: Bearer <token>` header.
  `TenantContextMiddleware` then calls `set_rls_context(tenant_id=...,
  user_id=...)`.
- **Context set:** `app.tenant_id` + `app.user_id`.
- **Service role:** Not set — user sees only their own tenant's data.

### 2. Internal Key (Agent Runtime)

- **Caller:** OpenClaw agent containers calling back to the control plane.
- **Mechanism:** `X-NBHD-Internal-Key` header validated against a per-tenant
  SHA-256 hash (with optional shared-key fallback). `X-NBHD-Tenant-Id` header
  must match the URL path tenant.
- **Context set:** `app.tenant_id` + `app.service_role = 'true'`.
- **Service role:** Set — agent needs full access within the tenant scope.

### 3. QStash (Cron / Scheduled Tasks)

- **Caller:** Upstash QStash webhook delivery for scheduled jobs.
- **Mechanism:** `Upstash-Signature` JWT header verified via the QStash SDK
  (supports key rotation).
- **Context set:** `app.service_role = 'true'` only.
- **Service role:** Set — cron tasks operate across all tenants.

### Summary Table

| Path | `app.tenant_id` | `app.user_id` | `app.service_role` |
|------|-----------------|---------------|---------------------|
| JWT | tenant UUID | user UUID | — |
| Internal Key | tenant UUID | — | `'true'` |
| QStash | — | — | `'true'` |

## Deployment Architecture

### Two Connection Strings

Production uses two database connection strings, both stored in Azure Key
Vault and mapped to Container App environment variables:

| Env Var | Connects As | Used For |
|---------|-------------|----------|
| `DATABASE_URL` | `app_user` (restricted) | Runtime application queries — RLS enforced |
| `ADMIN_DATABASE_URL` | `postgres` (superuser) | Migrations and schema changes — RLS bypassed |

### Startup Sequence

`startup.sh` runs migrations using the admin connection, then starts the
application using the restricted connection:

```bash
# Migrations run as postgres (bypasses RLS)
DATABASE_URL="${ADMIN_DATABASE_URL:-$DATABASE_URL}" python manage.py migrate --noinput

# Application runs as app_user (RLS enforced)
exec gunicorn config.wsgi:application ...
```

The `${ADMIN_DATABASE_URL:-$DATABASE_URL}` fallback means local development
works unchanged — both variables can point to the same database.

### Key Vault Integration

Both connection strings are stored as Key Vault secrets and referenced by the
Container App as secret-backed environment variables. This follows the same
pattern used for other sensitive configuration (API keys, bot tokens).

No secret values, vault names, or connection strings should appear in code or
documentation.

## Verifying RLS Is Working

### Check That Policies Exist

```sql
SELECT schemaname, tablename, policyname
FROM   pg_policies
WHERE  schemaname = 'public'
ORDER  BY tablename, policyname;
```

### Check That RLS Is Enabled on a Table

```sql
SELECT relname, relrowsecurity, relforcerowsecurity
FROM   pg_class
WHERE  relname = '<table_name>';
```

- `relrowsecurity = true` → RLS is enabled.
- `relforcerowsecurity = true` → RLS is enforced even for table owners.

### Test Isolation as `app_user`

```sql
-- Connect as app_user
SET ROLE app_user;

-- Without context: should return zero rows
SELECT count(*) FROM <table_name>;

-- With context: should return only that tenant's rows
SELECT set_config('app.tenant_id', '<some-tenant-uuid>', true);
SELECT count(*) FROM <table_name>;

-- Reset
RESET ROLE;
```

## Adding RLS to New Tables

When creating a new model that stores tenant data, follow this checklist:

1. **Add a `tenant` foreign key** to the model (FK to `tenants_tenant.id`).

2. **Enable RLS** on the table:
   ```sql
   ALTER TABLE <table_name> ENABLE ROW LEVEL SECURITY;
   ALTER TABLE <table_name> FORCE ROW LEVEL SECURITY;
   ```

3. **Create policies** for SELECT, INSERT, UPDATE, DELETE:
   ```sql
   CREATE POLICY tenant_select ON <table_name>
     FOR SELECT USING (
       tenant_id::text = current_setting('app.tenant_id', true)
       OR current_setting('app.service_role', true) = 'true'
     );
   -- Repeat for INSERT (WITH CHECK), UPDATE, DELETE
   ```

4. **Grant table permissions** to `app_user`:
   ```sql
   GRANT SELECT, INSERT, UPDATE, DELETE ON <table_name> TO app_user;
   ```

5. **Test** using the verification queries above to confirm isolation works.

6. **Apply via migration** using `RunSQL` in a Django migration, or apply
   directly in the database and track in your deployment runbook.
