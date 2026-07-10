"""BN-PR6 — extend the friends DB backstop to the "My sky" table: FORCE ROW
LEVEL SECURITY + tenant-scoped policies on ``friend_sky_memberships``.

Mirrors ``0008_friends_rls_backstop`` (read its docstring for the full design
rationale). Same two-regime posture: the audited accessor
(apps/friends/access.py) is and remains the PRIMARY boundary; this policy is a
second net that binds because the app's Postgres role (``app_user``) is
non-BYPASSRLS and does not own the tables (run ``manage.py check_friends_rls``
for the live verdict).

Design, following 0008 exactly:
  * Policies are named ``TO app_user`` (never ``TO public`` — the lockdown test
    forbids it); the role is ensured idempotently.
  * SELECT is locked to ``viewer_tenant_id = `` the tenant GUC ``app.tenant_id``
    — SkyMembership is a PRIVATE, ONE-WAY curation and every accessor read is
    self-scoped (a viewer only ever sees their own picks; the 0106 relock
    docstring records the same intent), so unlike the grant/message tables
    there is NO recipient-reach clause at all. FAILS CLOSED: unset/empty GUC →
    ``nullif(...,'')::uuid`` → NULL → no rows. ``app.service_role`` bypasses for
    trusted control-plane/background work, same as 0008.
  * NON-RECURSIVE trivially: the predicate is a plain column comparison, no
    subqueries.
  * Writes are permissive (WITH CHECK true): the accessor + the AST chokepoint
    govern writes; the backstop guards the READ leak.
  * FORCE covers the owner-bypass case.

Idempotent (DROP POLICY IF EXISTS before CREATE) and reversible (drop policies +
NO FORCE; RLS-enabled state is owned by the relock migrations — tenants.0106
already covers this table — and left untouched). The lockdown test allowlist +
``disable_rls``'s RLS_KEEP_ENABLED set + ``check_friends_rls``'s table set are
kept in sync with the table name here.
"""

from django.db import migrations

SKY_TABLE = "friend_sky_memberships"

# Same GUC fragments as 0008. `true` = missing_ok so an unset var yields ''
# (→ NULL via nullif → fail closed) instead of erroring.
_GUC_TENANT = "nullif(current_setting('app.tenant_id', true), '')::uuid"
_GUC_SERVICE = "coalesce(current_setting('app.service_role', true), '') = 'true'"

APPLY_SQL = f"""
-- Ensure the app role exists so `CREATE POLICY ... TO app_user` can name it. In
-- prod app_user already exists with grants; this is a no-op there. NOLOGIN + no
-- grants here — the negative tests grant what they need under SET ROLE.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
    CREATE ROLE app_user NOLOGIN;
  END IF;
END
$$;

-- friend_sky_memberships: strictly self-scoped — the viewer's own rows only.
-- service_role for trusted background work.
ALTER TABLE {SKY_TABLE} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {SKY_TABLE} FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS friends_{SKY_TABLE}_sel ON {SKY_TABLE};
CREATE POLICY friends_{SKY_TABLE}_sel ON {SKY_TABLE} FOR SELECT TO app_user
USING (
    {_GUC_SERVICE}
    OR viewer_tenant_id = {_GUC_TENANT}
);
DROP POLICY IF EXISTS friends_{SKY_TABLE}_ins ON {SKY_TABLE};
CREATE POLICY friends_{SKY_TABLE}_ins ON {SKY_TABLE} FOR INSERT TO app_user WITH CHECK (true);
DROP POLICY IF EXISTS friends_{SKY_TABLE}_upd ON {SKY_TABLE};
CREATE POLICY friends_{SKY_TABLE}_upd ON {SKY_TABLE} FOR UPDATE TO app_user USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS friends_{SKY_TABLE}_del ON {SKY_TABLE};
CREATE POLICY friends_{SKY_TABLE}_del ON {SKY_TABLE} FOR DELETE TO app_user USING (true);
"""

REVERSE_SQL = f"""
DROP POLICY IF EXISTS friends_{SKY_TABLE}_sel ON {SKY_TABLE};
DROP POLICY IF EXISTS friends_{SKY_TABLE}_ins ON {SKY_TABLE};
DROP POLICY IF EXISTS friends_{SKY_TABLE}_upd ON {SKY_TABLE};
DROP POLICY IF EXISTS friends_{SKY_TABLE}_del ON {SKY_TABLE};
ALTER TABLE {SKY_TABLE} NO FORCE ROW LEVEL SECURITY;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("friends", "0010_skymembership"),
        ("tenants", "0106_relock_after_sky"),
    ]

    operations = [
        migrations.RunSQL(APPLY_SQL, REVERSE_SQL),
    ]
