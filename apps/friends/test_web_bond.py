"""Web Neighborhood map: ``bond`` + ``friends_since`` on the home BFF neighbors.

``bond`` is a qualitative bucket ("light" | "steady" | "strong") for line
thickness. It must never expose a count, score or timestamp, never count
third-party activity, never read the other person's sky choice, never drop an
accepted edge below "light", and load in a fixed number of queries.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.lessons.models import Lesson
from apps.tenants.models import Tenant, User

from . import access, services
from .models import (
    Circle,
    CircleMembership,
    Friendship,
    FriendThread,
    FriendThreadMembership,
    NeighborProfile,
    SharedGoalMembership,
)

ALLOWED_NEIGHBOR_KEYS = {
    "friendship_id",
    "handle",
    "display_name",
    "avatar_hue",
    "spark_count",
    "in_my_sky",
    "has_unread_thread",
    "thread_id",
    "bond",
    "friends_since",
}


def _tenant(name: str) -> Tenant:
    user = User.objects.create_user(username=name, password="pass", display_name=name.title())
    tenant = Tenant.objects.create(user=user, status="active", friends_enabled=True)
    NeighborProfile.objects.create(tenant=tenant, handle=name, display_name=name.title(), avatar_hue=200)
    return tenant


def _accepted(a, b) -> Friendship:
    return Friendship.objects.create(
        requester=a, addressee=b, status=Friendship.Status.ACCEPTED, responded_at=timezone.now()
    )


def _thread(edge, a, b) -> FriendThread:
    thread = FriendThread.objects.create(kind=FriendThread.Kind.DIRECT, friendship=edge, created_by=a)
    FriendThreadMembership.objects.create(thread=thread, tenant=a, user=a.user)
    FriendThreadMembership.objects.create(thread=thread, tenant=b, user=b.user)
    return thread


def _messages(thread, sender, n):
    for _ in range(n):
        access.create_friend_message(thread, sender, sender.user, uuid.uuid4().hex, "hello")


def _circle(*members) -> Circle:
    circle = Circle.objects.create(name="c", created_by=members[0], invite_code=uuid.uuid4().hex)
    for m in members:
        CircleMembership.objects.create(circle=circle, tenant=m, user=m.user, status="active")
    return circle


def _mission(edge, *members):
    goal = access.create_mission(members[0], edge, title="Run together")
    for m in members:
        SharedGoalMembership.objects.create(shared_goal=goal, tenant=m, user=m.user, status="active")
    return goal


def _spark(owner, *, edge=None, circle=None):
    lesson = Lesson.objects.create(tenant=owner, text="x", source_type="experience", status="approved", tags=[])
    sl, _ = access.ensure_shared_lesson(lesson, owner)
    access.save_scrub_ready(sl, redacted_text="someone did a thing", content_hash=uuid.uuid4().hex)
    access.create_grant(sl, friendship=edge, circle=circle, granted_by=owner.user)


def _row(viewer, other):
    home = services.neighborhood_home(viewer)
    rows = [n for n in home["neighbors"] if n["handle"] == other.neighbor_profile.handle]
    return rows[0] if rows else None


class BondMathTest(TestCase):
    def test_caps_and_thresholds(self):
        self.assertEqual(access.bond_bucket(access.bond_points(messages=0, missions=0, circles=0, sparks=0)), "light")
        # Message volume alone is capped at 4 points → never "strong" by itself.
        self.assertEqual(
            access.bond_bucket(access.bond_points(messages=10_000, missions=0, circles=0, sparks=0)), "steady"
        )
        self.assertEqual(access.bond_bucket(access.bond_points(messages=40, missions=0, circles=1, sparks=0)), "strong")
        self.assertEqual(access.bond_bucket(access.bond_points(messages=0, missions=1, circles=1, sparks=0)), "steady")
        self.assertEqual(access.bond_bucket(access.bond_points(messages=0, missions=0, circles=0, sparks=99)), "steady")


class BondPayloadTest(TestCase):
    def setUp(self):
        self.me = _tenant("me")
        self.friend = _tenant("friend")
        self.edge = _accepted(self.me, self.friend)

    def test_quiet_friend_is_light_with_date_only(self):
        row = _row(self.me, self.friend)
        self.assertEqual(row["bond"], "light")
        self.assertEqual(row["friends_since"], self.edge.responded_at.date().isoformat())
        self.assertEqual(set(row), ALLOWED_NEIGHBOR_KEYS)

    def test_shared_thread_circle_and_mission_raise_the_bond(self):
        thread = _thread(self.edge, self.me, self.friend)
        _messages(thread, self.friend, 20)
        _messages(thread, self.me, 20)
        self.assertEqual(_row(self.me, self.friend)["bond"], "steady")
        _circle(self.me, self.friend)
        self.assertEqual(_row(self.me, self.friend)["bond"], "strong")
        # Symmetric: both sides see the same qualitative bucket.
        self.assertEqual(_row(self.friend, self.me)["bond"], "strong")

    def test_messages_outside_the_window_do_not_count(self):
        thread = _thread(self.edge, self.me, self.friend)
        _messages(thread, self.friend, 40)
        old = timezone.now() - timedelta(days=access.BOND_WINDOW_DAYS + 5)
        thread.messages.update(created_at=old)
        self.assertEqual(_row(self.me, self.friend)["bond"], "light")

    def test_third_party_activity_never_counts(self):
        third = _tenant("third")
        third_edge = _accepted(self.me, third)
        # Lots of activity between me and a third person…
        _messages(_thread(third_edge, self.me, third), third, 40)
        _mission(third_edge, self.me, third)
        # …and a circle spark from the friend, visible to a group, not the pair.
        circle = _circle(self.me, third)
        _spark(third, circle=circle)
        self.assertEqual(_row(self.me, self.friend)["bond"], "light")

    def test_the_other_persons_sky_choice_never_leaks(self):
        before = _row(self.me, self.friend)
        # The friend privately puts me in THEIR sky.
        access.add_to_sky(self.friend, self.edge)
        after = _row(self.me, self.friend)
        self.assertFalse(after["in_my_sky"])
        self.assertEqual(before["bond"], after["bond"])
        self.assertEqual(set(after), ALLOWED_NEIGHBOR_KEYS)

    def test_blocked_and_revoked_edges_have_no_row(self):
        for status in (Friendship.Status.BLOCKED, Friendship.Status.REVOKED):
            self.edge.status = status
            self.edge.save(update_fields=["status"])
            self.assertIsNone(_row(self.me, self.friend))

    def test_pair_sparks_count_only_on_their_edge(self):
        for _ in range(4):
            _spark(self.friend, edge=self.edge)
        self.assertEqual(_row(self.me, self.friend)["bond"], "steady")


class BondQueryBoundTest(TestCase):
    """No per-neighbor N+1: one busy neighbor or five cost the same queries."""

    def _home_queries(self, viewer) -> int:
        with CaptureQueriesContext(connection) as ctx:
            services.neighborhood_home(viewer)
        return len(ctx.captured_queries)

    def _busy_neighbor(self, viewer, name):
        other = _tenant(name)
        edge = _accepted(viewer, other)
        _messages(_thread(edge, viewer, other), other, 3)
        _mission(edge, viewer, other)
        _circle(viewer, other)
        _spark(other, edge=edge)

    def test_query_count_is_flat_in_the_number_of_neighbors(self):
        viewer = _tenant("viewer")
        self._busy_neighbor(viewer, "n0")
        one = self._home_queries(viewer)
        for i in range(1, 5):
            self._busy_neighbor(viewer, f"n{i}")
        five = self._home_queries(viewer)
        self.assertEqual(one, five)
