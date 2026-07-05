"""Report the live Postgres RLS reality for the friends cross-tenant tables.

PR8 of the Neighborhood layer adds a defence-in-depth DB backstop: FORCE ROW
LEVEL SECURITY + tenant-scoped policies on the three highest-blast-radius
cross-tenant tables (``shared_lessons``, ``lesson_share_grants``,
``friend_messages``). Those policies BIND only when the app's Postgres role is
NON-superuser and NON-BYPASSRLS. If Django connects as a BYPASSRLS role
(``postgres``/``service_role``), the policies are inert belt-and-suspenders that
start enforcing the moment the connection role is switched.

This command prints — via SQL introspection only, never env/secret access — the
facts that decide which regime we're in, so the answer lives in code and can be
run in prod after deploy:

  * the role Django is actually connected as, and its rolsuper / rolbypassrls;
  * the owner of each of the three tables (owner-bypass matters even for a
    non-superuser unless FORCE is set);
  * relrowsecurity / relforcerowsecurity on each table;
  * the policies currently attached to each table;
  * a one-line verdict: do the PR8 policies BIND on this connection, or are they
    inert (and would only bind after a role switch)?

Read it as: "inert" ⇒ the feature is unchanged and the remaining hardening is a
one-line MJ infra decision (point the app at the non-BYPASSRLS role). "binds"
⇒ every friends read path must carry a GUC (app.tenant_id) or service_role, or
reads fail closed.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import connection

FRIENDS_TABLES = ("shared_lessons", "lesson_share_grants", "friend_messages")


class Command(BaseCommand):
    help = "Print the live RLS reality for the friends cross-tenant tables (introspection only)."

    def handle(self, *args, **options):
        with connection.cursor() as cur:
            cur.execute("SELECT current_user, session_user")
            current_user, session_user = cur.fetchone()

            cur.execute(
                "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user",
            )
            row = cur.fetchone()
            rolsuper, rolbypassrls = row if row else (None, None)

            self.stdout.write("── friends RLS reality ──────────────────────────────")
            self.stdout.write(f"current_user   : {current_user}")
            self.stdout.write(f"session_user   : {session_user}")
            self.stdout.write(f"rolsuper       : {rolsuper}")
            self.stdout.write(f"rolbypassrls   : {rolbypassrls}")

            bypasses = bool(rolsuper) or bool(rolbypassrls)
            self.stdout.write("")

            cur.execute(
                """
                SELECT c.relname,
                       pg_get_userbyid(c.relowner) AS owner,
                       c.relrowsecurity,
                       c.relforcerowsecurity
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND c.relname = ANY(%s)
                ORDER BY c.relname
                """,
                [list(FRIENDS_TABLES)],
            )
            table_rows = cur.fetchall()
            self.stdout.write("table                | owner        | rls  | force")
            self.stdout.write("---------------------+--------------+------+------")
            owners = {}
            for relname, owner, rls, force in table_rows:
                owners[relname] = owner
                self.stdout.write(f"{relname:<20} | {str(owner):<12} | {str(bool(rls)):<4} | {bool(force)}")
            missing = [t for t in FRIENDS_TABLES if t not in owners]
            if missing:
                self.stdout.write(self.style.WARNING(f"(tables not present yet: {', '.join(missing)})"))
            self.stdout.write("")

            cur.execute(
                """
                SELECT tablename, policyname, roles, cmd
                FROM pg_policies
                WHERE schemaname = 'public' AND tablename = ANY(%s)
                ORDER BY tablename, policyname
                """,
                [list(FRIENDS_TABLES)],
            )
            policies = cur.fetchall()
            if policies:
                self.stdout.write("policies:")
                for tablename, policyname, roles, cmd in policies:
                    self.stdout.write(f"  {tablename}.{policyname}  roles={roles} cmd={cmd}")
            else:
                self.stdout.write("policies: (none on the friends tables)")
            self.stdout.write("")

            # Verdict. A BYPASSRLS/superuser connection ignores RLS entirely
            # (even FORCE). A non-bypassing role obeys policies; if it also OWNS
            # the table, only FORCE makes the owner obey.
            owner_is_current = any(owner == current_user for owner in owners.values())
            if bypasses:
                verdict = (
                    "INERT — this connection BYPASSES RLS (superuser/bypassrls). The PR8 policies "
                    "do NOT restrict it; they are belt-and-suspenders that begin enforcing the "
                    "moment the app connects as a non-BYPASSRLS role. Feature behavior unchanged. "
                    "Remaining hardening = the one MJ infra decision to point the app at that role."
                )
            elif owner_is_current:
                verdict = (
                    "BINDS ONLY IF FORCED — this non-bypassing role OWNS the table(s); a table "
                    "owner bypasses its own RLS UNLESS `FORCE ROW LEVEL SECURITY` is set (see the "
                    "force column above). With FORCE the policies enforce and every friends read "
                    "path must carry app.tenant_id or app.service_role, or reads fail closed."
                )
            else:
                verdict = (
                    "BINDS — this is a non-bypassing, non-owner role. The PR8 policies enforce on "
                    "this connection: every friends read path must carry app.tenant_id or "
                    "app.service_role, or reads fail closed."
                )
            self.stdout.write(self.style.SUCCESS("VERDICT: " + verdict))
