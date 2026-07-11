"""PR8 behavioral tests — the friends DB backstop (FORCE RLS) + rate limits.

The payoff test proves the claim the whole PR exists for: *even if the audited
accessor were bypassed*, a non-BYPASSRLS Postgres role obeys the FORCE-RLS
policies — no GUC → zero rows, wrong-tenant GUC → zero rows, correct GUC + an
active grant/membership → exactly the right rows.

Locally (and in CI) Django connects as a superuser, which BYPASSES RLS, so the
policies are inert on the normal connection. To exercise the bound regime we
``SET ROLE app_user`` (the NOLOGIN role the migration ensures exists), mirror the
prod runtime posture (``disable_rls`` leaves every friends table RLS-off EXCEPT
the exempt backstop tables), grant the role read access, and query raw SQL — the
most bypassing query possible. Everything runs inside the TestCase transaction,
so the GRANT / SET ROLE / RLS toggles roll back automatically.

BN-PR6 extends the same proof to the "My sky" table (``friend_sky_memberships``,
friends.0011): strictly self-scoped, so even the OTHER PARTY of the same
friendship edge sees zero rows.
"""

from __future__ import annotations

import contextlib

from django.db import connection
from django.test import TestCase, override_settings

from apps.tenants.models import Tenant, User

from . import access
from .models import (
    Friendship,
    FriendThread,
    FriendThreadMembership,
)

# The friends tables that KEEP RLS in prod; everything else is RLS-off via the
# boot disable_rls sweep. The negative test mirrors that so the policy subqueries
# (against friendships / memberships) can read under a bound role.
_RLS_OFF_IN_PROD = (
    "friendships",
    "friend_circle_memberships",
    "friend_thread_memberships",
    "neighbor_profiles",
)
_READABLE_BY_APP_USER = (
    "shared_lessons",
    "lesson_share_grants",
    "friend_messages",
    "friend_sky_memberships",
    *_RLS_OFF_IN_PROD,
)


def _tenant(username: str) -> Tenant:
    user = User.objects.create_user(username=username, password="pass", display_name=username.title())
    return Tenant.objects.create(user=user, status="active", friends_enabled=True)


def _ready_shared_lesson(owner):
    from apps.lessons.models import Lesson

    lesson = Lesson.objects.create(tenant=owner, text="x", source_type="experience", status="approved", tags=[])
    sl = access.ensure_shared_lesson(lesson, owner)
    access.save_scrub_ready(sl, redacted_text="someone did a thing", content_hash="h")
    return sl


class _AppUserRoleMixin:
    """Run queries as the bound ``app_user`` role, mirroring prod's RLS posture
    and GUC. Shared by the PR8 and BN-PR6 (sky) binding tests."""

    @contextlib.contextmanager
    def _app_user(self, *, tenant_id=None, service_role=False):
        """Restores role + GUC on exit."""
        cur = connection.cursor()
        try:
            # Flush the setUp inserts' deferred FK trigger events so ALTER TABLE
            # (RLS toggle) isn't rejected with "pending trigger events".
            cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
            for table in _RLS_OFF_IN_PROD:
                cur.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
            cur.execute("GRANT USAGE ON SCHEMA public TO app_user")
            cur.execute(f"GRANT SELECT ON {', '.join(_READABLE_BY_APP_USER)} TO app_user")
            cur.execute("SELECT set_config('app.tenant_id', %s, false)", [str(tenant_id) if tenant_id else ""])
            cur.execute("SELECT set_config('app.service_role', %s, false)", ["true" if service_role else ""])
            cur.execute("SET ROLE app_user")
            yield cur
        finally:
            cur.execute("RESET ROLE")
            cur.execute("SELECT set_config('app.tenant_id', '', false), set_config('app.service_role', '', false)")


class RlsBackstopBindsTest(_AppUserRoleMixin, TestCase):
    """Prove the FORCE-RLS policies bind for a non-BYPASSRLS role."""

    def setUp(self):
        self.owner = _tenant("rls_owner")
        self.friend = _tenant("rls_friend")
        self.stranger = _tenant("rls_stranger")
        self.edge = Friendship.objects.create(
            requester=self.owner, addressee=self.friend, status=Friendship.Status.ACCEPTED
        )
        self.sl = _ready_shared_lesson(self.owner)
        self.grant = access.create_grant(self.sl, friendship=self.edge, granted_by=self.owner.user)
        # A direct chat thread owner↔friend with one message from the owner.
        self.thread = FriendThread.objects.create(
            kind=FriendThread.Kind.DIRECT, friendship=self.edge, created_by=self.owner
        )
        FriendThreadMembership.objects.create(thread=self.thread, tenant=self.owner, user=self.owner.user)
        FriendThreadMembership.objects.create(thread=self.thread, tenant=self.friend, user=self.friend.user)
        self.msg, _ = access.create_friend_message(self.thread, self.owner, self.owner.user, "m1", "hi")

    def _visible_lessons(self, cur):
        cur.execute("SELECT id FROM shared_lessons")
        return {str(r[0]) for r in cur.fetchall()}

    def _visible_messages(self, cur):
        cur.execute("SELECT seq FROM friend_messages")
        return {r[0] for r in cur.fetchall()}

    def test_no_guc_yields_zero_rows_even_raw(self):
        # The most bypassing query possible (raw SELECT *, no accessor filter),
        # no GUC → the policy fails closed → nothing.
        with self._app_user() as cur:
            self.assertEqual(self._visible_lessons(cur), set())
            self.assertEqual(self._visible_messages(cur), set())

    def test_wrong_tenant_guc_yields_zero_rows(self):
        with self._app_user(tenant_id=self.stranger.id) as cur:
            self.assertEqual(self._visible_lessons(cur), set())
            self.assertEqual(self._visible_messages(cur), set())

    def test_recipient_guc_sees_granted_snapshot(self):
        with self._app_user(tenant_id=self.friend.id) as cur:
            self.assertEqual(self._visible_lessons(cur), {str(self.sl.id)})

    def test_owner_guc_sees_own_snapshot(self):
        with self._app_user(tenant_id=self.owner.id) as cur:
            self.assertEqual(self._visible_lessons(cur), {str(self.sl.id)})

    def test_thread_member_guc_sees_messages_nonmember_does_not(self):
        with self._app_user(tenant_id=self.friend.id) as cur:
            self.assertEqual(self._visible_messages(cur), {self.msg.seq})
        with self._app_user(tenant_id=self.stranger.id) as cur:
            self.assertEqual(self._visible_messages(cur), set())

    def test_service_role_sees_rows_without_tenant_guc(self):
        # The background-work path: service_role, no tenant GUC → rows visible.
        with self._app_user(service_role=True) as cur:
            self.assertEqual(self._visible_lessons(cur), {str(self.sl.id)})
            self.assertEqual(self._visible_messages(cur), {self.msg.seq})

    def test_revoked_grant_hides_snapshot_from_recipient(self):
        access.revoke_grant(self.grant)
        with self._app_user(tenant_id=self.friend.id) as cur:
            self.assertEqual(self._visible_lessons(cur), set())


class SkyRlsBackstopBindsTest(_AppUserRoleMixin, TestCase):
    """BN-PR6 payoff: the sky backstop (friends.0011) is STRICTLY self-scoped.

    Both parties keep the very same friendship edge in their private skies; the
    binding assertion is that each viewer's GUC sees ONLY their own row — the
    other party of the edge is as blind as a stranger ("whom you keep close is
    yours alone", and now the DB enforces it even past the accessor)."""

    def setUp(self):
        self.owner = _tenant("sky_owner")
        self.friend = _tenant("sky_friend")
        self.stranger = _tenant("sky_stranger")
        self.edge = Friendship.objects.create(
            requester=self.owner, addressee=self.friend, status=Friendship.Status.ACCEPTED
        )
        created, _ = access.add_to_sky(self.owner, self.edge)
        assert created
        created, _ = access.add_to_sky(self.friend, self.edge)
        assert created

    def _visible_sky_viewers(self, cur):
        cur.execute("SELECT viewer_tenant_id FROM friend_sky_memberships")
        return {str(r[0]) for r in cur.fetchall()}

    def test_no_guc_yields_zero_rows_even_raw(self):
        with self._app_user() as cur:
            self.assertEqual(self._visible_sky_viewers(cur), set())

    def test_stranger_guc_yields_zero_rows(self):
        with self._app_user(tenant_id=self.stranger.id) as cur:
            self.assertEqual(self._visible_sky_viewers(cur), set())

    def test_each_viewer_sees_only_their_own_rows_not_the_other_partys(self):
        with self._app_user(tenant_id=self.owner.id) as cur:
            self.assertEqual(self._visible_sky_viewers(cur), {str(self.owner.id)})
        with self._app_user(tenant_id=self.friend.id) as cur:
            self.assertEqual(self._visible_sky_viewers(cur), {str(self.friend.id)})

    def test_service_role_sees_all_rows_without_tenant_guc(self):
        with self._app_user(service_role=True) as cur:
            self.assertEqual(self._visible_sky_viewers(cur), {str(self.owner.id), str(self.friend.id)})


class BackstopServiceContextTest(TestCase):
    """The service-role context manager sets + clears only app.service_role."""

    @override_settings(FRIENDS_DB_BACKSTOP=True)
    def test_sets_and_clears_service_role_only(self):
        with connection.cursor() as cur:
            cur.execute("SELECT set_config('app.tenant_id', %s, false)", ["11111111-1111-1111-1111-111111111111"])
        with access.backstop_service_context():
            with connection.cursor() as cur:
                cur.execute("SELECT current_setting('app.service_role', true), current_setting('app.tenant_id', true)")
                service, tenant = cur.fetchone()
            self.assertEqual(service, "true")
            self.assertEqual(tenant, "11111111-1111-1111-1111-111111111111")  # tenant GUC untouched
        with connection.cursor() as cur:
            cur.execute("SELECT current_setting('app.service_role', true), current_setting('app.tenant_id', true)")
            service, tenant = cur.fetchone()
        self.assertIn(service, ("", None))  # service_role cleared
        self.assertEqual(tenant, "11111111-1111-1111-1111-111111111111")  # tenant GUC still intact

    @override_settings(FRIENDS_DB_BACKSTOP=False)
    def test_noop_when_disabled(self):
        with access.backstop_service_context(), connection.cursor() as cur:
            cur.execute("SELECT current_setting('app.service_role', true)")
            self.assertIn(cur.fetchone()[0], ("", None))


class CheckFriendsRlsCommandTest(TestCase):
    def test_command_runs_and_reports_a_verdict(self):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("check_friends_rls", stdout=out)
        text = out.getvalue()
        self.assertIn("VERDICT:", text)
        self.assertIn("current_user", text)


class DisableRlsExemptionTest(TestCase):
    """The boot-time disable_rls sweep must NOT strip the backstop tables (trap
    2a: without the exemption, every deploy would wipe the PR8 policies'
    enforcement)."""

    def test_backstop_tables_keep_rls_after_disable_sweep(self):
        from io import StringIO

        from django.core.management import call_command

        call_command("disable_rls", stdout=StringIO())
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT tablename FROM pg_tables
                WHERE schemaname = 'public'
                  AND tablename = ANY(%s)
                  AND rowsecurity = true
                ORDER BY tablename
                """,
                [["shared_lessons", "lesson_share_grants", "friend_messages", "friend_sky_memberships"]],
            )
            still_enabled = {r[0] for r in cur.fetchall()}
        self.assertEqual(
            still_enabled,
            {"shared_lessons", "lesson_share_grants", "friend_messages", "friend_sky_memberships"},
        )


class RateLimitWiringTest(TestCase):
    def test_throttles_are_declared(self):
        from apps.friends.throttling import (
            AdoptDayThrottle,
            MessageSendHourThrottle,
            ShareSendDayThrottle,
        )
        from apps.friends.views import AdoptShareView, ThreadMessagesView
        from apps.lessons.views import LessonViewSet

        self.assertIn(AdoptDayThrottle, AdoptShareView.throttle_classes)
        self.assertEqual(AdoptDayThrottle.rate, "60/day")
        self.assertEqual(ShareSendDayThrottle.rate, "30/day")
        self.assertEqual(MessageSendHourThrottle.rate, "60/hour")
        # message-send throttle applies to POST only
        view = ThreadMessagesView()
        view.request = type("R", (), {"method": "POST"})()
        self.assertTrue(any(isinstance(t, MessageSendHourThrottle) for t in view.get_throttles()))
        view.request = type("R", (), {"method": "GET"})()
        self.assertEqual(view.get_throttles(), [])
        # share throttle applies to the share action only
        vs = LessonViewSet()
        vs.action = "share"
        self.assertTrue(any(isinstance(t, ShareSendDayThrottle) for t in vs.get_throttles()))
