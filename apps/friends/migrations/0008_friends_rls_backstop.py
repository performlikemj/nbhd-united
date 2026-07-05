"""PR8 — the friends DB backstop: FORCE ROW LEVEL SECURITY + tenant-scoped
policies on the three highest-blast-radius cross-tenant tables.

Defense in depth. The audited accessor (apps/friends/access.py) is and remains
the PRIMARY cross-tenant boundary; these policies are a second net that begins
enforcing the moment the app's Postgres role stops bypassing RLS (run
``manage.py check_friends_rls`` for the live verdict). While Django connects as a
BYPASSRLS superuser they are INERT — no behaviour change.

Design (design §11 decision 6, adapted to the reality check):
  * Policies are named ``TO app_user`` (never ``TO public`` — that would re-expose
    them via the anon Supabase Data API; the lockdown test forbids it). The role
    is ensured to exist idempotently so the CREATE POLICY can name it; in prod
    ``app_user`` already exists with full grants.
  * SELECT is locked to the tenant GUC ``app.tenant_id`` (set per request by
    TenantContextMiddleware, or ``app.service_role`` for trusted background work).
    It FAILS CLOSED: an unset/empty GUC → ``nullif(...,'')::uuid`` → NULL → no
    rows. Visibility mirrors the accessor: owner sees own; a friend/circle sees a
    snapshot reached by an ACTIVE grant they are a party to / member of; a thread
    member sees its messages.
  * NON-RECURSIVE by construction: ``lesson_share_grants`` visibility keys only on
    ``friendships`` / ``friend_circle_memberships`` (both RLS-off in prod via the
    boot ``disable_rls`` sweep — only these three tables are exempt), and because
    the owner is itself a party of the grant's friendship / a member of its
    circle, that single rule covers owner AND recipient with no reference back to
    ``shared_lessons``. ``shared_lessons`` then references ``lesson_share_grants``
    one-directionally and lets the grant policy do the reach-filtering.
  * Writes are permissive (WITH CHECK true): the accessor + the AST chokepoint
    govern writes; the backstop guards the READ leak (the actual blast radius).
    A cross-tenant write can't exfiltrate, and locking writes here would risk
    breaking legitimate accessor writes for no isolation gain.
  * FORCE covers the owner-bypass case (if the app role also OWNS the tables).

Idempotent (DROP POLICY IF EXISTS before CREATE) and reversible (drop policies +
NO FORCE; RLS-enabled state is owned by the relock migrations, left untouched).
The lockdown test allowlist + the ``disable_rls`` RLS_KEEP_ENABLED set are kept
in sync with the three table names here.
"""

from django.db import migrations

FRIENDS_TABLES = ("shared_lessons", "lesson_share_grants", "friend_messages")

# Reused GUC fragments. `true` = missing_ok so an unset var yields '' (→ NULL via
# nullif → fail closed) instead of erroring.
_GUC_TENANT = "nullif(current_setting('app.tenant_id', true), '')::uuid"
_GUC_SERVICE = "coalesce(current_setting('app.service_role', true), '') = 'true'"

# A grant's audience reaches the GUC tenant — and because the owner is a party of
# the friendship / a member of the circle, this covers the owner too.
_GRANT_REACHES_GUC = f"""(
    EXISTS (
        SELECT 1 FROM friendships f
        WHERE f.id = lesson_share_grants.friendship_id
          AND f.status = 'accepted'
          AND {_GUC_TENANT} IN (f.requester_id, f.addressee_id)
    )
    OR EXISTS (
        SELECT 1 FROM friend_circle_memberships m
        WHERE m.circle_id = lesson_share_grants.circle_id
          AND m.status = 'active'
          AND m.tenant_id = {_GUC_TENANT}
    )
)"""


def _write_policies(table: str) -> str:
    """Permissive INSERT/UPDATE/DELETE — the accessor governs writes; RLS guards
    reads. INSERT ... RETURNING still needs the new row SELECT-visible, which the
    writer's own tenant GUC (owner) or service_role satisfies."""
    return f"""
DROP POLICY IF EXISTS friends_{table}_ins ON {table};
CREATE POLICY friends_{table}_ins ON {table} FOR INSERT TO app_user WITH CHECK (true);
DROP POLICY IF EXISTS friends_{table}_upd ON {table};
CREATE POLICY friends_{table}_upd ON {table} FOR UPDATE TO app_user USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS friends_{table}_del ON {table};
CREATE POLICY friends_{table}_del ON {table} FOR DELETE TO app_user USING (true);
"""


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

-- shared_lessons: owner OR (an active grant on this snapshot that the GUC can
-- see — the grant policy does the reach-filtering, so no reach clause here and
-- no recursion). service_role for trusted background work.
ALTER TABLE shared_lessons ENABLE ROW LEVEL SECURITY;
ALTER TABLE shared_lessons FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS friends_shared_lessons_sel ON shared_lessons;
CREATE POLICY friends_shared_lessons_sel ON shared_lessons FOR SELECT TO app_user
USING (
    {_GUC_SERVICE}
    OR owner_tenant_id = {_GUC_TENANT}
    OR EXISTS (
        SELECT 1 FROM lesson_share_grants g
        WHERE g.shared_lesson_id = shared_lessons.id
          AND g.status = 'active'
    )
);
{_write_policies("shared_lessons")}

-- lesson_share_grants: self-contained (friendships + circle memberships only) →
-- non-recursive. Covers owner (a party/member) and recipient alike.
ALTER TABLE lesson_share_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE lesson_share_grants FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS friends_lesson_share_grants_sel ON lesson_share_grants;
CREATE POLICY friends_lesson_share_grants_sel ON lesson_share_grants FOR SELECT TO app_user
USING (
    {_GUC_SERVICE}
    OR {_GRANT_REACHES_GUC}
);
{_write_policies("lesson_share_grants")}

-- friend_messages: visible to active members of the message's thread.
ALTER TABLE friend_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE friend_messages FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS friends_friend_messages_sel ON friend_messages;
CREATE POLICY friends_friend_messages_sel ON friend_messages FOR SELECT TO app_user
USING (
    {_GUC_SERVICE}
    OR EXISTS (
        SELECT 1 FROM friend_thread_memberships tm
        WHERE tm.thread_id = friend_messages.thread_id
          AND tm.tenant_id = {_GUC_TENANT}
          AND tm.left_at IS NULL
    )
);
{_write_policies("friend_messages")}
"""


def _reverse_for(table: str) -> str:
    return f"""
DROP POLICY IF EXISTS friends_{table}_sel ON {table};
DROP POLICY IF EXISTS friends_{table}_ins ON {table};
DROP POLICY IF EXISTS friends_{table}_upd ON {table};
DROP POLICY IF EXISTS friends_{table}_del ON {table};
ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;
"""


REVERSE_SQL = "".join(_reverse_for(t) for t in FRIENDS_TABLES)


class Migration(migrations.Migration):
    dependencies = [
        ("friends", "0007_circle_circlemembership_contentreport_and_more"),
        ("tenants", "0103_relock_after_circles"),
    ]

    operations = [
        migrations.RunSQL(APPLY_SQL, REVERSE_SQL),
    ]
