"""Behavioral tests for the Neighborhood PR0 foundation.

Covers the design's negative-test matrix (§4.5) that PR0 can exercise:
DB-level edge dedup + no-self, the audited accessor's deny paths (pending /
declined / revoked / blocked / non-existent / non-party IDOR), the
cross-tenant write gate (suspended vs hibernated target), and the envelope
section rendering empty whether the flag is off or on-with-no-data.

The AST architectural chokepoint lives in ``test_access_chokepoint.py``.
"""

from __future__ import annotations

import uuid

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from apps.tenants.models import Tenant, User

from . import access
from .models import FriendInvite, Friendship, NeighborProfile, compute_pair_key


def _make_tenant(username: str, status: str = "active") -> Tenant:
    user = User.objects.create_user(username=username, password="pass")
    return Tenant.objects.create(user=user, status=status)


class PairKeyDedupTest(TestCase):
    """The friendship edge is deduped at the DATABASE (``pair_key`` unique),
    never in a service method — the invariant that stops two concurrent waves
    racing into two rows."""

    def setUp(self):
        self.a = _make_tenant("pk_a")
        self.b = _make_tenant("pk_b")

    def test_pair_key_is_order_independent(self):
        self.assertEqual(
            compute_pair_key(self.a.id, self.b.id),
            compute_pair_key(self.b.id, self.a.id),
        )

    def test_reciprocal_wave_collides_on_same_row(self):
        Friendship.objects.create(requester=self.a, addressee=self.b)
        # The reciprocal wave B→A computes the SAME pair_key → violates the
        # unique constraint. Wrap in atomic so the broken savepoint rolls back
        # and the test connection stays usable.
        with self.assertRaises(IntegrityError), transaction.atomic():
            Friendship.objects.create(requester=self.b, addressee=self.a)
        self.assertEqual(Friendship.objects.filter(pair_key=compute_pair_key(self.a.id, self.b.id)).count(), 1)

    def test_transaction_level_race_second_writer_loses(self):
        """Simulate two waves that both pass any Python-level check and race to
        the DB: build two unsaved edges for the same pair, save both. The DB
        unique constraint — not app logic — serializes them, so the second
        save fails. This is why dedup must live at the constraint, not a
        service method."""
        f1 = Friendship(requester=self.a, addressee=self.b)
        f2 = Friendship(requester=self.b, addressee=self.a)  # reciprocal, same pair_key
        f1.save()
        with self.assertRaises(IntegrityError), transaction.atomic():
            f2.save()

    def test_no_self_friendship(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Friendship.objects.create(requester=self.a, addressee=self.a)


class AreNeighborsTest(TestCase):
    """``access.are_neighbors`` — True only for a live accepted edge."""

    def setUp(self):
        self.a = _make_tenant("an_a")
        self.b = _make_tenant("an_b")
        self.edge = Friendship.objects.create(requester=self.a, addressee=self.b)

    def _set_status(self, status, **extra):
        Friendship.objects.filter(id=self.edge.id).update(status=status, **extra)

    def test_accepted_is_true_and_symmetric(self):
        self._set_status(Friendship.Status.ACCEPTED)
        self.assertTrue(access.are_neighbors(self.a, self.b))
        self.assertTrue(access.are_neighbors(self.b, self.a))  # symmetric

    def test_pending_is_false(self):
        self.assertFalse(access.are_neighbors(self.a, self.b))

    def test_declined_is_false(self):
        self._set_status(Friendship.Status.DECLINED)
        self.assertFalse(access.are_neighbors(self.a, self.b))

    def test_revoked_is_false(self):
        self._set_status(Friendship.Status.REVOKED)
        self.assertFalse(access.are_neighbors(self.a, self.b))

    def test_blocked_either_direction_is_false(self):
        # Blocked supersedes accepted regardless of who blocked.
        self._set_status(Friendship.Status.BLOCKED, blocked_by=self.a)
        self.assertFalse(access.are_neighbors(self.a, self.b))
        self.assertFalse(access.are_neighbors(self.b, self.a))
        self._set_status(Friendship.Status.BLOCKED, blocked_by=self.b)
        self.assertFalse(access.are_neighbors(self.a, self.b))
        self.assertFalse(access.are_neighbors(self.b, self.a))

    def test_nonexistent_edge_is_false(self):
        stranger = _make_tenant("an_stranger")
        self.assertFalse(access.are_neighbors(self.a, stranger))

    def test_self_is_false(self):
        self.assertFalse(access.are_neighbors(self.a, self.a))

    def test_none_is_false(self):
        self.assertFalse(access.are_neighbors(self.a, None))


class AssertNeighborsTest(TestCase):
    """``access.assert_neighbors`` — returns the accepted edge for a party,
    denies everyone else. Addressing is by ``friendship_id``; the IDOR defense
    is the re-verified party check."""

    def setUp(self):
        self.a = _make_tenant("asn_a")
        self.b = _make_tenant("asn_b")
        self.edge = Friendship.objects.create(requester=self.a, addressee=self.b, status=Friendship.Status.ACCEPTED)

    def test_party_gets_edge(self):
        self.assertEqual(access.assert_neighbors(self.a, self.edge.id), self.edge)
        self.assertEqual(access.assert_neighbors(self.b, self.edge.id), self.edge)

    def test_non_party_denied_idor(self):
        # §4.5 IDOR row: a stranger swaps in someone else's friendship_id.
        stranger = _make_tenant("asn_stranger")
        with self.assertRaises(PermissionDenied):
            access.assert_neighbors(stranger, self.edge.id)

    def test_pending_edge_denied(self):
        Friendship.objects.filter(id=self.edge.id).update(status=Friendship.Status.PENDING)
        with self.assertRaises(PermissionDenied):
            access.assert_neighbors(self.a, self.edge.id)

    def test_blocked_edge_denied(self):
        Friendship.objects.filter(id=self.edge.id).update(status=Friendship.Status.BLOCKED, blocked_by=self.a)
        with self.assertRaises(PermissionDenied):
            access.assert_neighbors(self.a, self.edge.id)

    def test_nonexistent_friendship_denied(self):
        with self.assertRaises(PermissionDenied):
            access.assert_neighbors(self.a, uuid.uuid4())


class AssertCanWriteTest(TestCase):
    """``access.assert_can_write`` — cross-tenant write gate. Requires an
    accepted edge, then gates on the target's lifecycle."""

    def setUp(self):
        self.a = _make_tenant("acw_a")
        self.b = _make_tenant("acw_b")
        Friendship.objects.create(requester=self.a, addressee=self.b, status=Friendship.Status.ACCEPTED)

    def test_non_neighbor_denied(self):
        stranger = _make_tenant("acw_stranger")
        with self.assertRaises(PermissionDenied):
            access.assert_can_write(self.a, stranger)

    def test_active_target_allowed(self):
        self.assertEqual(access.assert_can_write(self.a, self.b), self.b)

    def test_suspended_target_denied(self):
        Tenant.objects.filter(id=self.b.id).update(status=Tenant.Status.SUSPENDED)
        self.b.refresh_from_db()
        with self.assertRaises(PermissionDenied):
            access.assert_can_write(self.a, self.b)

    def test_hibernated_target_allowed_by_default(self):
        Tenant.objects.filter(id=self.b.id).update(hibernated_at=timezone.now())
        self.b.refresh_from_db()
        self.assertEqual(access.assert_can_write(self.a, self.b), self.b)

    def test_hibernated_target_denied_when_wake_disallowed(self):
        Tenant.objects.filter(id=self.b.id).update(hibernated_at=timezone.now())
        self.b.refresh_from_db()
        with self.assertRaises(PermissionDenied):
            access.assert_can_write(self.a, self.b, allow_hibernated=False)


class SharedStarQsStubTest(TestCase):
    """The stub carries its full contract but must not silently return data
    before PR2 wires the real query."""

    def test_raises_not_implemented(self):
        a = _make_tenant("ssq_a")
        b = _make_tenant("ssq_b")
        with self.assertRaises(NotImplementedError):
            access.shared_star_qs(a, b)


class EnvelopeSectionTest(TestCase):
    """The ``neighborhood`` section is gated on ``friends_enabled`` and renders
    empty in PR0 — off, and on-with-no-data."""

    def _section(self):
        from apps.orchestrator.envelope_registry import all_sections

        sections = {s.key: s for s in all_sections()}
        self.assertIn("neighborhood", sections, "neighborhood section not registered")
        return sections["neighborhood"]

    def test_registered_with_expected_metadata(self):
        section = self._section()
        self.assertEqual(section.order, 63)
        self.assertIn("Neighborhood", section.heading)

    def test_disabled_when_flag_off_and_renders_empty(self):
        section = self._section()
        tenant = _make_tenant("env_off")
        self.assertFalse(tenant.friends_enabled)
        self.assertFalse(section.enabled(tenant))
        self.assertEqual(section.render(tenant), "")

    def test_enabled_with_no_data_renders_empty(self):
        section = self._section()
        tenant = _make_tenant("env_on")
        tenant.friends_enabled = True
        tenant.save(update_fields=["friends_enabled"])
        self.assertTrue(section.enabled(tenant))
        self.assertEqual(section.render(tenant), "")


class ModelBasicsTest(TestCase):
    """Light coverage that the three PR0 tables persist as designed."""

    def test_neighbor_profile_handle_unique(self):
        t1 = _make_tenant("np_1")
        t2 = _make_tenant("np_2")
        NeighborProfile.objects.create(tenant=t1, handle="kenji", display_name="Kenji")
        with self.assertRaises(IntegrityError), transaction.atomic():
            NeighborProfile.objects.create(tenant=t2, handle="kenji", display_name="Dup")

    def test_friend_invite_token_unique(self):
        inviter = _make_tenant("fi_1")
        FriendInvite.objects.create(inviter=inviter, token="tok-abc", expires_at=timezone.now())
        with self.assertRaises(IntegrityError), transaction.atomic():
            FriendInvite.objects.create(inviter=inviter, token="tok-abc", expires_at=timezone.now())
