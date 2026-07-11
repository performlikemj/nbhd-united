"""PR11 behavioral tests — signup invite auto-accept + the iOS enablers
(home BFF, blocked list, consent, typed push, report-general, me flags, preview
status contract)."""

from __future__ import annotations

from unittest import mock

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.lessons.models import Lesson
from apps.tenants.models import Tenant, User
from apps.tenants.serializers import TenantRegistrationSerializer, TenantSerializer
from apps.tenants.views import OnboardTenantView

from . import access, services
from .models import (
    ContentReport,
    Friendship,
    FriendThread,
    FriendThreadMembership,
    NeighborProfile,
    PendingShare,
    SharedLesson,
)


def _tenant(username, *, friends_enabled=True) -> Tenant:
    user = User.objects.create_user(username=username, password="pass", display_name=username.title())
    return Tenant.objects.create(user=user, status="active", friends_enabled=friends_enabled)


def _profile(tenant, handle):
    return NeighborProfile.objects.create(tenant=tenant, handle=handle, display_name=handle.title())


def _accepted(a, b):
    return Friendship.objects.create(requester=a, addressee=b, status=Friendship.Status.ACCEPTED)


def _ready_shared_lesson(owner):
    lesson = Lesson.objects.create(tenant=owner, text="x", source_type="experience", status="approved", tags=[])
    sl, _ = access.ensure_shared_lesson(lesson, owner)
    access.save_scrub_ready(sl, redacted_text="someone did a thing", content_hash="h")
    return sl


def _client(user):
    c = APIClient()
    c.force_authenticate(user=user)
    return c


# ── 1. Signup invite auto-accept (PR1.5 seam) ─────────────────────────────────


class SignupInviteTest(TestCase):
    def test_serializer_accepts_invite_token(self):
        s = TenantRegistrationSerializer(data={"display_name": "New", "invite_token": "abc"})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(s.validated_data["invite_token"], "abc")

    def test_claim_quietly_accepts_valid_invite(self):
        inviter = _tenant("si_inviter")
        _profile(inviter, "inviter")
        invite = services.create_invite(inviter, max_uses=5)
        newbie = _tenant("si_newbie")
        OnboardTenantView._claim_invite_quietly(newbie, newbie.user, invite.token)
        edge = Friendship.objects.get(pair_key__isnull=False, requester=inviter, addressee=newbie)
        self.assertEqual(edge.status, Friendship.Status.ACCEPTED)

    def test_claim_quietly_swallows_bad_token(self):
        newbie = _tenant("si_newbie2")
        # Must NOT raise — a bad invite can never block a signup.
        OnboardTenantView._claim_invite_quietly(newbie, newbie.user, "not-a-real-token")
        self.assertEqual(Friendship.objects.filter(addressee=newbie).count(), 0)


# ── 2. Home BFF ───────────────────────────────────────────────────────────────


class HomeBffTest(TestCase):
    def setUp(self):
        self.me = _tenant("h_me")
        _profile(self.me, "hme")
        self.n1 = _tenant("h_n1")
        _profile(self.n1, "hn1")
        self.edge = _accepted(self.me, self.n1)
        # n1 shares a spark to me
        sl = _ready_shared_lesson(self.n1)
        access.create_grant(sl, friendship=self.edge, granted_by=self.n1.user)
        # a 1:1 thread with an unread message from n1
        self.thread = FriendThread.objects.create(
            kind=FriendThread.Kind.DIRECT, friendship=self.edge, created_by=self.n1
        )
        FriendThreadMembership.objects.create(thread=self.thread, tenant=self.me, user=self.me.user)
        FriendThreadMembership.objects.create(thread=self.thread, tenant=self.n1, user=self.n1.user)
        access.create_friend_message(self.thread, self.n1, self.n1.user, "m1", "hi")
        # a pending incoming wave + a pending outgoing wave
        self.waver = _tenant("h_waver")
        _profile(self.waver, "hwaver")
        self.wave_in = Friendship.objects.create(
            requester=self.waver, addressee=self.me, status=Friendship.Status.PENDING
        )
        self.target = _tenant("h_target")
        _profile(self.target, "htarget")
        Friendship.objects.create(requester=self.me, addressee=self.target, status=Friendship.Status.PENDING)
        # an agent share-proposal awaiting my approval
        my_lesson = Lesson.objects.create(
            tenant=self.me, text="my note", source_type="experience", status="approved", tags=[]
        )
        self.proposal = PendingShare.objects.create(
            tenant=self.me,
            source_lesson=my_lesson,
            proposed_by="agent",
            target_friendship=self.edge,
            status=PendingShare.Status.PENDING,
            expires_at=timezone.now(),
        )

    def test_home_payload_shape(self):
        data = _client(self.me.user).get("/api/v1/friends/home/").json()
        self.assertEqual(data["profile"]["handle"], "hme")
        self.assertEqual(len(data["neighbors"]), 1)
        n = data["neighbors"][0]
        self.assertEqual(n["handle"], "hn1")
        self.assertEqual(n["spark_count"], 1)
        self.assertTrue(n["has_unread_thread"])
        self.assertIsNotNone(n["thread_id"])
        self.assertEqual(len(data["pending_in"]), 1)
        self.assertEqual(len(data["pending_out"]), 1)
        kinds = {m["kind"] for m in data["moments"]}
        self.assertEqual(kinds, {"wave", "share_proposal"})
        self.assertIsNotNone(data["cursor"])

    def test_share_moment_carries_preview_and_status(self):
        moments = _client(self.me.user).get("/api/v1/friends/home/").json()["moments"]
        share = next(m for m in moments if m["kind"] == "share_proposal")
        self.assertEqual(share["pending_share_id"], str(self.proposal.id))
        self.assertIn("scrub_status", share)
        self.assertIn("audience_label", share)
        # PR12: preview-poll ids for the iOS SharePreviewSheet (proposal path).
        self.assertEqual(share["lesson_id"], str(self.proposal.source_lesson_id))
        self.assertEqual(share["friendship_id"], str(self.proposal.target_friendship_id))

    def test_since_filters_moments(self):
        from urllib.parse import quote

        future = quote((timezone.now() + timezone.timedelta(hours=1)).isoformat())  # encode the +00:00
        data = _client(self.me.user).get(f"/api/v1/friends/home/?since={future}").json()
        self.assertEqual(data["moments"], [])


# ── 3. Blocked list ───────────────────────────────────────────────────────────


class BlockedListTest(TestCase):
    def test_lists_only_my_blocks(self):
        me = _tenant("b_me")
        blocked = _tenant("b_blocked")
        _profile(blocked, "bblocked")
        edge = _accepted(me, blocked)
        services.respond_to_wave(me, edge.id, "block")
        rows = _client(me.user).get("/api/v1/friends/blocked/").json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["handle"], "bblocked")
        self.assertEqual(rows[0]["friendship_id"], str(edge.id))
        # the blocked side does NOT see it as their block
        self.assertEqual(_client(blocked.user).get("/api/v1/friends/blocked/").json(), [])


# ── 4. Consent ────────────────────────────────────────────────────────────────


class ConsentTest(TestCase):
    def test_consent_records_and_clears_needs_consent(self):
        me = _tenant("c_me")
        _profile(me, "cme")
        resp = _client(me.user).post("/api/v1/friends/consent/", {}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["accepted_terms_version"], services.FRIENDS_TERMS_VERSION)
        home = _client(me.user).get("/api/v1/friends/home/").json()
        self.assertFalse(home["profile"]["needs_consent"])

    def test_needs_consent_true_before_accept(self):
        me = _tenant("c_me2")
        _profile(me, "cme2")
        home = _client(me.user).get("/api/v1/friends/home/").json()
        self.assertTrue(home["profile"]["needs_consent"])


# ── 5. Report general ─────────────────────────────────────────────────────────


class ReportGeneralTest(TestCase):
    def test_general_report_creates_open_row_no_hide(self):
        me = _tenant("r_me")
        _profile(me, "rme")
        resp = _client(me.user).post(
            "/api/v1/friends/report/",
            {"target_kind": "general", "reason": "creepy DM", "detail": "from a stranger"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["hidden"])
        report = ContentReport.objects.get(reporter_tenant=me, target_kind="general")
        self.assertEqual(report.status, "open")
        self.assertIn("creepy DM", report.reason)
        self.assertIn("from a stranger", report.reason)


# ── 6. Typed push payloads ────────────────────────────────────────────────────


class TypedPushTest(TestCase):
    def _capture(self):
        return mock.patch("apps.router.push_views._push_to_user_devices")

    def test_friend_message_push_is_typed(self):
        a = _tenant("p_a")
        b = _tenant("p_b")
        _profile(a, "pa")
        edge = _accepted(a, b)
        thread = FriendThread.objects.create(kind=FriendThread.Kind.DIRECT, friendship=edge, created_by=a)
        FriendThreadMembership.objects.create(thread=thread, tenant=a, user=a.user)
        FriendThreadMembership.objects.create(thread=thread, tenant=b, user=b.user)
        msg, _ = access.create_friend_message(thread, a, a.user, "m1", "hey")
        from .notifications import _deliver_friend_push

        with mock.patch("apps.common.apns.apns_configured", return_value=True), self._capture() as push:
            _deliver_friend_push(msg)
        _, kwargs = push.call_args
        self.assertEqual(kwargs["extra"]["type"], "friend_message")
        self.assertEqual(kwargs["extra"]["thread_id"], str(thread.id))

    def test_wave_push_is_typed(self):
        a = _tenant("p_wa")
        b = _tenant("p_wb")
        edge = Friendship.objects.create(requester=a, addressee=b, status=Friendship.Status.PENDING)
        from .notifications import notify_wave_app

        with mock.patch("apps.common.apns.apns_configured", return_value=True), self._capture() as push:
            notify_wave_app(edge)
        _, kwargs = push.call_args
        self.assertEqual(kwargs["extra"]["type"], "wave")
        self.assertEqual(kwargs["extra"]["friendship_id"], str(edge.id))

    def test_share_proposal_push_is_typed(self):
        a = _tenant("p_sa")
        lesson = Lesson.objects.create(tenant=a, text="x", source_type="experience", status="approved", tags=[])
        share = PendingShare.objects.create(
            tenant=a,
            source_lesson=lesson,
            proposed_by="agent",
            status=PendingShare.Status.PENDING,
            expires_at=timezone.now(),
        )
        from .notifications import notify_share_proposal

        with mock.patch("apps.common.apns.apns_configured", return_value=True), self._capture() as push:
            notify_share_proposal(share)
        _, kwargs = push.call_args
        self.assertEqual(kwargs["extra"]["type"], "share_approval")
        self.assertEqual(kwargs["extra"]["pending_share_id"], str(share.id))


# ── 7. me flags + preview status contract ─────────────────────────────────────


class MeFlagsTest(TestCase):
    def test_me_serializer_exposes_friends_flags(self):
        tenant = _tenant("me_flags")
        data = TenantSerializer(tenant).data
        self.assertIn("friends_enabled", data)
        self.assertIn("friends_agent_propose_enabled", data)
        self.assertTrue(data["friends_enabled"])
        self.assertFalse(data["friends_agent_propose_enabled"])  # getattr default (PR9 field absent here)


class PreviewContractTest(TestCase):
    def setUp(self):
        self.owner = _tenant("pv_owner")
        self.viewer = _tenant("pv_viewer")
        _profile(self.viewer, "pvviewer")
        self.edge = _accepted(self.owner, self.viewer)
        self.lesson = Lesson.objects.create(
            tenant=self.owner, text="batch cook", source_type="experience", status="approved", tags=[]
        )
        self.sl, _ = access.ensure_shared_lesson(self.lesson, self.owner)

    def _get(self):
        return _client(self.owner.user).get(
            f"/api/v1/friends/shares/preview/?lesson_id={self.lesson.id}&friendship_id={self.edge.id}"
        )

    def test_202_while_scrubbing_with_retry_after(self):
        access.mark_scrub_pending(self.sl)
        resp = self._get()
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp["Retry-After"], "2")

    def test_409_on_failed(self):
        SharedLesson.objects.filter(id=self.sl.id).update(scrub_status=SharedLesson.ScrubStatus.FAILED)
        self.assertEqual(self._get().status_code, 409)

    def test_200_on_ready_with_text(self):
        access.save_scrub_ready(self.sl, redacted_text="someone batch-cooks", content_hash="h")
        resp = self._get()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["redacted_text"], "someone batch-cooks")
