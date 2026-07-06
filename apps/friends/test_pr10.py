"""PR10 behavioral tests — blocking quietly hides a counterpart within shared
Circles (bidirectional, no ejection, no reveal), plus the unblock action.

The block is a Friendship row flipped to ``blocked``. Inside a shared Circle it
suppresses what the pair sees of each other — circle-granted sparks, circle-chat
messages, agent absorb, APNs — both directions, while everyone else is
unaffected and the composer stays open. Unblock (blocker-only) flips the edge to
``revoked`` (re-wave to resume), never silently restoring.
"""

from __future__ import annotations

from unittest import mock

from django.test import TestCase
from rest_framework.exceptions import NotFound
from rest_framework.test import APIClient

from apps.lessons.models import Lesson
from apps.tenants.models import Tenant, User

from . import access, circles, services
from .models import (
    AbsorbedItem,
    Friendship,
    FriendThread,
    NeighborProfile,
)


def _tenant(username) -> Tenant:
    user = User.objects.create_user(username=username, password="pass", display_name=username.title())
    return Tenant.objects.create(user=user, status="active", friends_enabled=True)


def _profile(tenant, handle):
    return NeighborProfile.objects.create(tenant=tenant, handle=handle, display_name=handle.title())


def _accepted(a, b) -> Friendship:
    return Friendship.objects.create(requester=a, addressee=b, status=Friendship.Status.ACCEPTED)


def _ready_shared_lesson(owner):
    lesson = Lesson.objects.create(tenant=owner, text="x", source_type="experience", status="approved", tags=[])
    sl = access.ensure_shared_lesson(lesson, owner)
    access.save_scrub_ready(sl, redacted_text="someone did a thing", content_hash="h")
    return sl


class _CircleTrio(TestCase):
    """A creator + two neighbors, all in one Circle; the creator blocks one."""

    def setUp(self):
        self.a = _tenant("t_a")  # creator + blocker
        self.b = _tenant("t_b")  # gets blocked
        self.c = _tenant("t_c")  # bystander
        for t, h in ((self.a, "aa"), (self.b, "bb"), (self.c, "cc")):
            _profile(t, h)
        self.edge_ab = _accepted(self.a, self.b)
        self.edge_ac = _accepted(self.a, self.c)
        self.circle = circles.create_circle(self.a, self.a.user, name="Nishi-ku")
        circles.join_circle(self.b, self.b.user, self.circle.invite_code)
        circles.join_circle(self.c, self.c.user, self.circle.invite_code)
        self.thread = FriendThread.objects.get(circle=self.circle, kind=FriendThread.Kind.CIRCLE)

    def _block_ab(self):
        services.respond_to_wave(self.a, self.edge_ab.id, "block")


# ── Circle-share visibility ──────────────────────────────────────────────────


class CircleShareBlockHideTest(_CircleTrio):
    def setUp(self):
        super().setUp()
        self.sl_a = _ready_shared_lesson(self.a)
        access.create_grant(self.sl_a, circle=self.circle, granted_by=self.a.user)
        self.sl_b = _ready_shared_lesson(self.b)
        access.create_grant(self.sl_b, circle=self.circle, granted_by=self.b.user)

    def test_visible_before_block(self):
        self.assertEqual(access.shared_star_qs(self.b, self.a).count(), 1)
        self.assertEqual(access.shared_star_qs(self.a, self.b).count(), 1)

    def test_bidirectional_hide_after_block(self):
        self._block_ab()
        self.assertEqual(access.shared_star_qs(self.b, self.a).count(), 0)  # B can't see A's
        self.assertEqual(access.shared_star_qs(self.a, self.b).count(), 0)  # A can't see B's

    def test_bystander_unaffected(self):
        self._block_ab()
        self.assertEqual(access.shared_star_qs(self.c, self.a).count(), 1)
        self.assertEqual(access.shared_star_qs(self.c, self.b).count(), 1)

    def test_unblock_restores_circle_visibility(self):
        self._block_ab()
        self.assertEqual(access.shared_star_qs(self.b, self.a).count(), 0)
        services.unblock(self.a, self.edge_ab.id)  # → revoked; both still circle members
        self.assertEqual(access.shared_star_qs(self.b, self.a).count(), 1)
        self.assertEqual(access.shared_star_qs(self.a, self.b).count(), 1)

    def test_inbound_grants_absorb_excludes_blocked(self):
        # A's agent absorbs circle sparks; after blocking B, B's spark drops out.
        owners_before = {g.shared_lesson.owner_tenant_id for g in access.inbound_shared_grants(self.a)}
        self.assertIn(self.b.id, owners_before)
        self._block_ab()
        owners_after = {g.shared_lesson.owner_tenant_id for g in access.inbound_shared_grants(self.a)}
        self.assertNotIn(self.b.id, owners_after)


# ── Circle chat ──────────────────────────────────────────────────────────────


class CircleChatBlockHideTest(_CircleTrio):
    def setUp(self):
        super().setUp()
        self.m_a, _ = access.create_friend_message(self.thread, self.a, self.a.user, "a1", "from A")
        self.m_b, _ = access.create_friend_message(self.thread, self.b, self.b.user, "b1", "from B")

    def _seqs_for(self, viewer):
        return {m.seq for m in access.thread_messages_page(self.thread, 0, 50, viewer_tenant_id=viewer.id)}

    def test_history_hidden_both_directions_after_block(self):
        self._block_ab()
        self.assertNotIn(self.m_b.seq, self._seqs_for(self.a))  # A stops seeing B's (incl history)
        self.assertNotIn(self.m_a.seq, self._seqs_for(self.b))  # B stops seeing A's
        self.assertIn(self.m_a.seq, self._seqs_for(self.a))  # still sees own

    def test_bystander_sees_everything(self):
        self._block_ab()
        seqs = self._seqs_for(self.c)
        self.assertIn(self.m_a.seq, seqs)
        self.assertIn(self.m_b.seq, seqs)

    def test_composer_open_send_still_stored_and_visible_to_others(self):
        self._block_ab()
        msg, created = services.send_friend_message(self.b, self.b.user, self.thread.id, "b2", "still here")
        self.assertTrue(created)  # send is not rejected
        self.assertIn(msg.seq, self._seqs_for(self.c))  # bystander sees it
        self.assertNotIn(msg.seq, self._seqs_for(self.a))  # blocked counterpart does not

    def test_absorb_pending_chat_excludes_blocked_sender(self):
        # C also posts so there's a non-blocked message proving absorb still runs.
        # absorb_pending_chat advances the cursor, so assert on a SINGLE call after
        # the block (per-message senders, since a circle thread's from_id is None).
        access.create_friend_message(self.thread, self.c, self.c.user, "c1", "from C")
        self._block_ab()
        senders = {m.sender_tenant_id for row in access.absorb_pending_chat(self.a) for m in row["messages"]}
        self.assertNotIn(self.b.id, senders)  # blocked → not absorbed
        self.assertIn(self.c.id, senders)  # bystander still absorbed


class CircleChatPushBlockTest(_CircleTrio):
    def test_apns_fanout_skips_blocked_counterpart(self):
        self._block_ab()
        msg, _ = access.create_friend_message(self.thread, self.b, self.b.user, "b1", "hi crew")
        pushed_users = []
        with (
            mock.patch("apps.common.apns.apns_configured", return_value=True),
            mock.patch(
                "apps.router.push_views._push_to_user_devices",
                side_effect=lambda user, **kw: pushed_users.append(user.id),
            ),
        ):
            from .notifications import _deliver_friend_push

            _deliver_friend_push(msg)
        self.assertIn(self.c.user.id, pushed_users)  # bystander pushed
        self.assertNotIn(self.a.user.id, pushed_users)  # blocked counterpart skipped
        self.assertNotIn(self.b.user.id, pushed_users)  # sender never pushed


# ── Block purges absorbed items ───────────────────────────────────────────────


class BlockPurgeAbsorbedTest(_CircleTrio):
    def test_block_purges_absorbed_both_directions(self):
        a_from_b = AbsorbedItem.objects.create(
            tenant=self.a,
            source_kind=AbsorbedItem.SourceKind.SHARED_LESSON,
            source_id=_ready_shared_lesson(self.b).id,
            from_tenant=self.b,
            circle=self.circle,
            label="spark from B",
        )
        b_from_a = AbsorbedItem.objects.create(
            tenant=self.b,
            source_kind=AbsorbedItem.SourceKind.SHARED_LESSON,
            source_id=_ready_shared_lesson(self.a).id,
            from_tenant=self.a,
            circle=self.circle,
            label="spark from A",
        )
        c_from_b = AbsorbedItem.objects.create(
            tenant=self.c,
            source_kind=AbsorbedItem.SourceKind.SHARED_LESSON,
            source_id=_ready_shared_lesson(self.b).id,
            from_tenant=self.b,
            circle=self.circle,
            label="spark from B (C's)",
        )
        self._block_ab()
        a_from_b.refresh_from_db()
        b_from_a.refresh_from_db()
        c_from_b.refresh_from_db()
        self.assertIsNotNone(a_from_b.purged_at)  # A's absorbed-from-B tombstoned
        self.assertIsNotNone(b_from_a.purged_at)  # and B's absorbed-from-A (bidirectional)
        self.assertIsNone(c_from_b.purged_at)  # C untouched


# ── Unblock ───────────────────────────────────────────────────────────────────


class UnblockTest(TestCase):
    def setUp(self):
        self.blocker = _tenant("ub_blocker")
        self.blocked = _tenant("ub_blocked")
        self.stranger = _tenant("ub_stranger")
        self.edge = _accepted(self.blocker, self.blocked)
        services.respond_to_wave(self.blocker, self.edge.id, "block")

    def test_blocker_can_unblock_to_revoked(self):
        edge = services.unblock(self.blocker, self.edge.id)
        self.assertEqual(edge.status, Friendship.Status.REVOKED)
        self.assertIsNone(edge.blocked_by_id)

    def test_blocked_side_cannot_unblock_no_reveal(self):
        with self.assertRaises(NotFound):
            services.unblock(self.blocked, self.edge.id)

    def test_non_party_404(self):
        with self.assertRaises(NotFound):
            services.unblock(self.stranger, self.edge.id)

    def test_unblock_endpoint(self):
        client = APIClient()
        client.force_authenticate(user=self.blocker.user)
        resp = client.post(f"/api/v1/friends/{self.edge.id}/unblock/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "revoked")

    def test_unblock_non_blocked_edge_404(self):
        fresh = _accepted(_tenant("ub_x"), self.blocker)
        with self.assertRaises(NotFound):
            services.unblock(self.blocker, fresh.id)
