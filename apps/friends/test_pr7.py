"""PR7 behavioral tests — Circles (groups on edges) + circle chat + circle shares.

A Circle is a named set of accepted neighbors, built ON edges: joining needs an
invite code (and neighbor-of-creator) OR a wave-in from a member you already
neighbor. Membership IS the consent grant inside a Circle — so a share is visible
only between two people who are BOTH active members, and leaving/removal purges
(or, by explicit choice, keeps) what the agent absorbed FROM that Circle. None of
the cross-tenant model access lives here; it stays in apps.friends.access.
"""

from __future__ import annotations

from django.db import IntegrityError, transaction
from django.test import TestCase
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.lessons.models import Lesson
from apps.orchestrator import personas
from apps.tenants.models import Tenant, User

from . import access, circles, services
from .models import (
    AbsorbedItem,
    Circle,
    CircleMembership,
    ContentReport,
    Friendship,
    FriendThread,
    FriendThreadMembership,
    LessonShareGrant,
    NeighborProfile,
    PendingShare,
    SharedLesson,
)


def _tenant(username: str, *, friends_enabled: bool = True) -> Tenant:
    user = User.objects.create_user(username=username, password="pass", display_name=username.title())
    return Tenant.objects.create(user=user, status="active", friends_enabled=friends_enabled)


def _profile(tenant, handle) -> NeighborProfile:
    return NeighborProfile.objects.create(tenant=tenant, handle=handle, display_name=handle.title())


def _edge(a, b) -> Friendship:
    return Friendship.objects.create(requester=a, addressee=b, status=Friendship.Status.ACCEPTED)


def _lesson(tenant, text="Batch-cook Sundays.") -> Lesson:
    return Lesson.objects.create(tenant=tenant, text=text, source_type="experience", status="approved", tags=[])


def _ready_shared_lesson(owner, lesson=None) -> SharedLesson:
    lesson = lesson or _lesson(owner)
    sl, _ = access.ensure_shared_lesson(lesson, owner)
    access.save_scrub_ready(sl, redacted_text="someone batch-cooks", content_hash="h")
    return sl


# ── Model constraints: exactly-one-audience + circle partial-unique ──────────


class GrantAudienceConstraintTest(TestCase):
    def setUp(self):
        self.owner = _tenant("g_owner")
        self.other = _tenant("g_other")
        self.edge = _edge(self.owner, self.other)
        self.circle = Circle.objects.create(name="Nishi-ku", created_by=self.owner, invite_code="code-g")
        self.sl = _ready_shared_lesson(self.owner)

    def test_grant_with_both_audiences_violates_xor(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            LessonShareGrant.objects.create(shared_lesson=self.sl, friendship=self.edge, circle=self.circle)

    def test_grant_with_no_audience_violates_xor(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            LessonShareGrant.objects.create(shared_lesson=self.sl, friendship=None, circle=None)

    def test_circle_grant_partial_unique(self):
        LessonShareGrant.objects.create(shared_lesson=self.sl, circle=self.circle)
        with self.assertRaises(IntegrityError), transaction.atomic():
            LessonShareGrant.objects.create(shared_lesson=self.sl, circle=self.circle)


# ── Create / join / add / cap ────────────────────────────────────────────────


class CircleCreateJoinTest(TestCase):
    def setUp(self):
        self.creator = _tenant("c_creator")
        self.friend = _tenant("c_friend")
        self.stranger = _tenant("c_stranger")
        _profile(self.creator, "creator")
        _profile(self.friend, "friendly")
        _profile(self.stranger, "stranger")
        _edge(self.creator, self.friend)  # creator ↔ friend are neighbors

    def test_create_makes_admin_and_circle_thread(self):
        circle = circles.create_circle(self.creator, self.creator.user, name="Nishi-ku")
        membership = CircleMembership.objects.get(circle=circle, tenant=self.creator)
        self.assertEqual((membership.role, membership.status), ("admin", "active"))
        thread = FriendThread.objects.get(circle=circle, kind=FriendThread.Kind.CIRCLE)
        self.assertTrue(
            FriendThreadMembership.objects.filter(thread=thread, tenant=self.creator, left_at__isnull=True).exists()
        )

    def test_join_requires_neighbor_of_creator(self):
        circle = circles.create_circle(self.creator, self.creator.user, name="Nishi-ku")
        with self.assertRaises(PermissionDenied):
            circles.join_circle(self.stranger, self.stranger.user, circle.invite_code)
        # A neighbor of the creator can join via the code.
        circles.join_circle(self.friend, self.friend.user, circle.invite_code)
        self.assertEqual(CircleMembership.objects.get(circle=circle, tenant=self.friend).status, "active")

    def test_join_bad_code_404_no_reveal(self):
        with self.assertRaises(NotFound):
            circles.join_circle(self.friend, self.friend.user, "not-a-real-code")

    def test_add_member_requires_neighbor_of_adder(self):
        circle = circles.create_circle(self.creator, self.creator.user, name="Nishi-ku")
        # creator is NOT a neighbor of stranger → can't wave them in.
        with self.assertRaises(PermissionDenied):
            circles.add_circle_member(self.creator, self.creator.user, circle.id, "stranger")
        # creator IS a neighbor of friend → can.
        circles.add_circle_member(self.creator, self.creator.user, circle.id, "friendly")
        self.assertEqual(CircleMembership.objects.get(circle=circle, tenant=self.friend).status, "active")

    def test_cap_enforced_at_max(self):
        for i in range(circles.MAX_CIRCLES_PER_TENANT):
            circles.create_circle(self.creator, self.creator.user, name=f"Circle {i}")
        with self.assertRaises(ValidationError):
            circles.create_circle(self.creator, self.creator.user, name="one too many")


# ── Circle shares: visibility needs BOTH active memberships ──────────────────


class CircleShareVisibilityTest(TestCase):
    def setUp(self):
        self.owner = _tenant("cs_owner")
        self.member = _tenant("cs_member")
        self.outsider = _tenant("cs_outsider")
        _profile(self.owner, "owner")
        _profile(self.member, "member")
        _profile(self.outsider, "outsider")
        _edge(self.owner, self.member)
        _edge(self.owner, self.outsider)  # a neighbor, but NOT in the circle
        self.circle = circles.create_circle(self.owner, self.owner.user, name="Nishi-ku")
        circles.join_circle(self.member, self.member.user, self.circle.invite_code)
        self.sl = _ready_shared_lesson(self.owner)
        access.create_grant(self.sl, circle=self.circle, granted_by=self.owner.user)

    def test_both_active_members_see_the_share(self):
        self.assertEqual(access.shared_star_qs(self.member, self.owner).count(), 1)

    def test_neighbor_outside_circle_sees_nothing(self):
        # A circle-only grant is invisible to an edge-only neighbor (no friendship grant).
        self.assertEqual(access.shared_star_qs(self.outsider, self.owner).count(), 0)

    def test_leaving_drops_the_share_instantly(self):
        self.assertEqual(access.shared_star_qs(self.member, self.owner).count(), 1)
        circles.leave_circle(self.member, self.circle.id)
        self.assertEqual(access.shared_star_qs(self.member, self.owner).count(), 0)

    def test_leaving_revokes_my_own_circle_grants(self):
        # The member shares THEIR own lesson to the circle; leaving takes it out of
        # the circle instantly (revoked, and the orphaned snapshot is deleted).
        member_sl = _ready_shared_lesson(self.member)
        grant = access.create_grant(member_sl, circle=self.circle, granted_by=self.member.user)
        circles.leave_circle(self.member, self.circle.id)
        self.assertFalse(LessonShareGrant.objects.filter(id=grant.id, status=LessonShareGrant.Status.ACTIVE).exists())


# ── Leave / removal: purge-or-keep the circle's absorbed items ───────────────


class CircleLeavePurgeTest(TestCase):
    def setUp(self):
        self.owner = _tenant("lp_owner")
        self.member = _tenant("lp_member")
        _profile(self.owner, "owner")
        _profile(self.member, "member")
        _edge(self.owner, self.member)
        self.circle = circles.create_circle(self.owner, self.owner.user, name="Nishi-ku")
        circles.join_circle(self.member, self.member.user, self.circle.invite_code)

    def _absorbed(self):
        return AbsorbedItem.objects.create(
            tenant=self.member,
            source_kind=AbsorbedItem.SourceKind.SHARED_LESSON,
            source_id=_ready_shared_lesson(self.owner).id,
            from_tenant=self.owner,
            circle=self.circle,
            label="a circle spark",
        )

    def test_leave_default_purges_circle_items(self):
        item = self._absorbed()
        circles.leave_circle(self.member, self.circle.id)
        item.refresh_from_db()
        self.assertIsNotNone(item.purged_at)

    def test_leave_keep_retains_circle_items(self):
        item = self._absorbed()
        circles.leave_circle(self.member, self.circle.id, purge=False)
        item.refresh_from_db()
        self.assertIsNone(item.purged_at)

    def test_removed_by_admin_purges(self):
        item = self._absorbed()
        circles.remove_circle_member(self.owner, self.circle.id, "member")
        item.refresh_from_db()
        self.assertIsNotNone(item.purged_at)
        self.assertEqual(CircleMembership.objects.get(circle=self.circle, tenant=self.member).status, "removed")

    def test_admin_cannot_remove_self(self):
        with self.assertRaises(ValidationError):
            circles.remove_circle_member(self.owner, self.circle.id, "owner")


# ── Circle chat: membership gates sends; leaving drops it ─────────────────────


class CircleChatTest(TestCase):
    def setUp(self):
        self.owner = _tenant("ch_owner")
        self.member = _tenant("ch_member")
        self.stranger = _tenant("ch_stranger")
        _profile(self.owner, "owner")
        _profile(self.member, "member")
        _edge(self.owner, self.member)
        self.circle = circles.create_circle(self.owner, self.owner.user, name="Nishi-ku")
        circles.join_circle(self.member, self.member.user, self.circle.invite_code)
        self.thread = FriendThread.objects.get(circle=self.circle, kind=FriendThread.Kind.CIRCLE)

    def test_member_can_send_nonmember_cannot(self):
        msg, created = services.send_friend_message(self.owner, self.owner.user, self.thread.id, "c1", "hi crew")
        self.assertTrue(created)
        with self.assertRaises(NotFound):
            services.send_friend_message(self.stranger, self.stranger.user, self.thread.id, "c2", "let me in")

    def test_leaving_drops_chat_membership(self):
        services.send_friend_message(self.member, self.member.user, self.thread.id, "c1", "present")
        circles.leave_circle(self.member, self.circle.id)
        self.assertTrue(
            FriendThreadMembership.objects.filter(
                thread=self.thread, tenant=self.member, left_at__isnull=False
            ).exists()
        )
        with self.assertRaises(NotFound):
            services.send_friend_message(self.member, self.member.user, self.thread.id, "c2", "still here?")

    def test_absorb_toggle_persists(self):
        result = services.patch_thread_membership(self.member, self.thread.id, muted=True, agent_absorb_enabled=False)
        self.assertEqual((result["muted"], result["agent_absorb_enabled"]), (True, False))


# ── Cross-Circle leakage guard lives in the agent's AGENTS.md gate ────────────


class AgentsMdLeakGuardTest(TestCase):
    def test_cross_circle_leak_line_present_when_enabled(self):
        # The cross-Circle leak guard is present regardless of the propose flag
        # (PR9 split): assert on the always-rendered core wording.
        tenant = _tenant("md_on", friends_enabled=True)
        files = personas.render_workspace_files("neighbor", tenant=tenant)
        agents_md = files["NBHD_AGENTS_MD"]
        self.assertIn("NEVER surface one Circle's learning as another Circle's", agents_md)
        self.assertIn("confidences do not travel between groups", agents_md)

    def test_gate_absent_when_disabled(self):
        tenant = _tenant("md_off", friends_enabled=False)
        files = personas.render_workspace_files("neighbor", tenant=tenant)
        self.assertNotIn("confidences do not travel between groups", files["NBHD_AGENTS_MD"])


# ── Report → reporter-side hide (shares + chat) ──────────────────────────────


class ReportHideTest(TestCase):
    def setUp(self):
        self.owner = _tenant("r_owner")
        self.viewer = _tenant("r_viewer")
        _profile(self.owner, "owner")
        _profile(self.viewer, "viewer")
        self.edge = _edge(self.owner, self.viewer)

    def test_report_shared_lesson_hides_for_reporter(self):
        sl = _ready_shared_lesson(self.owner)
        access.create_grant(sl, friendship=self.edge, granted_by=self.owner.user)
        self.assertEqual(access.shared_star_qs(self.viewer, self.owner).count(), 1)
        circles.report_content(
            self.viewer, self.viewer.user, target_kind="shared_lesson", target_id=str(sl.id), reason="spam"
        )
        self.assertEqual(access.shared_star_qs(self.viewer, self.owner).count(), 0)
        self.assertEqual(ContentReport.objects.filter(reporter_tenant=self.viewer, status="hidden").count(), 1)

    def test_report_friend_message_hidden_only_for_reporter(self):
        thread = FriendThread.objects.create(kind=FriendThread.Kind.DIRECT, friendship=self.edge, created_by=self.owner)
        FriendThreadMembership.objects.create(thread=thread, tenant=self.owner, user=self.owner.user)
        FriendThreadMembership.objects.create(thread=thread, tenant=self.viewer, user=self.viewer.user)
        msg, _ = access.create_friend_message(thread, self.owner, self.owner.user, "m1", "hello there")
        circles.report_content(
            self.viewer, self.viewer.user, target_kind="friend_message", target_id=str(msg.public_id), reason="rude"
        )
        # Hidden for the reporter, still present for the sender.
        self.assertEqual(len(access.thread_messages_page(thread, 0, 50, viewer_tenant_id=self.viewer.id)), 0)
        self.assertEqual(len(access.thread_messages_page(thread, 0, 50, viewer_tenant_id=self.owner.id)), 1)


# ── Agent propose-share to a circle (human-gated, same PendingShare) ─────────


class ProposeCircleShareTest(TestCase):
    def setUp(self):
        self.owner = _tenant("ps_owner")
        self.member = _tenant("ps_member")
        _profile(self.owner, "owner")
        _profile(self.member, "member")
        _edge(self.owner, self.member)
        self.circle = circles.create_circle(self.owner, self.owner.user, name="Nishi-ku")
        circles.join_circle(self.member, self.member.user, self.circle.invite_code)

    def test_propose_to_circle_creates_pending_only(self):
        lesson = _lesson(self.owner)
        pending, created = services.propose_share(self.owner, lesson, circle=self.circle)
        self.assertTrue(created)
        self.assertEqual(pending.target_circle_id, self.circle.id)
        self.assertIsNone(pending.target_friendship_id)
        self.assertEqual(pending.status, PendingShare.Status.PENDING)
        self.assertEqual(pending.proposed_by, "agent")
        # No grant yet — a human must approve.
        self.assertEqual(LessonShareGrant.objects.filter(circle=self.circle).count(), 0)

    def test_propose_requires_exactly_one_audience(self):
        lesson = _lesson(self.owner)
        with self.assertRaises(ValidationError):
            services.propose_share(self.owner, lesson)  # neither friendship nor circle

    def test_preview_audience_names_the_circle(self):
        lesson = _lesson(self.owner)
        services.share_lesson(self.owner, self.owner.user, lesson, circle_id=str(self.circle.id))
        payload, code = services.preview_share(self.owner, str(lesson.id), circle_id=str(self.circle.id))
        self.assertEqual(code, 200)
        # 2 members (owner + member) → "your 1 Nishi-ku neighbor".
        self.assertIn("Nishi-ku", payload["audience"])

    def test_pending_queue_surfaces_circle_audience(self):
        # The Approvals queue must carry circle_id + a circle audience label so the
        # console can preview a circle-proposed share (not just friendship ones).
        lesson = _lesson(self.owner)
        services.share_lesson(self.owner, self.owner.user, lesson, circle_id=str(self.circle.id))
        rows = services.list_pending_shares(self.owner)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["circle_id"], str(self.circle.id))
        self.assertIsNone(rows[0]["friendship_id"])
        self.assertIn("Nishi-ku", rows[0]["audience"])
