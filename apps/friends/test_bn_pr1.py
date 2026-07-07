"""BN-PR1 behavioral tests — "My sky", the chosen inner circle (Bounded
Neighborhood brief, decisions accepted 2026-07-07).

The spine under test:
  * A PRIVATE, ONE-WAY, per-(viewer, friendship) curation — a neighbor can NEVER
    see they were chosen (no field on the other side's home feed, no roster read,
    no mutate). SkyMembership.objects is confined to apps/friends/access.py.
  * A HARD cap of 12 enforced server-side — a 13th add returns a structured
    ``409 {"error": "sky_full", "cap": 12, "sky": [...]}`` and creates nothing.
  * Additive ``in_my_sky`` on the home BFF neighbor rows (THE iOS flight contract)
    and on the /wormholes/ payload (web parity), plus a ``warpable=sky`` filter.
  * The new table lands RLS-enabled with no policy (the relock migration).
"""

from __future__ import annotations

from django.db import connection
from django.test import TestCase
from rest_framework.test import APIClient

from apps.lessons.models import Lesson
from apps.tenants.models import Tenant, User

from . import access, services
from .models import Friendship, NeighborProfile, SkyMembership


def _tenant(username: str, *, friends_enabled: bool = True) -> Tenant:
    user = User.objects.create_user(username=username, password="pass", display_name=username.title())
    return Tenant.objects.create(user=user, status="active", friends_enabled=friends_enabled)


def _profile(tenant, handle: str, *, hue: int = 210) -> NeighborProfile:
    return NeighborProfile.objects.create(tenant=tenant, handle=handle, display_name=handle.title(), avatar_hue=hue)


def _client(user) -> APIClient:
    c = APIClient()
    c.force_authenticate(user=user)
    return c


def _accepted_edge(a, b) -> Friendship:
    return Friendship.objects.create(requester=a, addressee=b, status=Friendship.Status.ACCEPTED)


def _neighbor(viewer, name: str) -> tuple[Tenant, Friendship]:
    """A fresh accepted neighbor of ``viewer`` with a profile. Returns (tenant, edge)."""
    other = _tenant(name)
    _profile(other, name)
    return other, _accepted_edge(viewer, other)


def _share(owner, edge, *, redacted="someone batch-cooks on sundays"):
    """Publish one scrubbed, ready, active-granted snapshot from ``owner`` on
    ``edge`` (so the neighbor's gate has spark_count > 0)."""
    lesson = Lesson.objects.create(tenant=owner, text="raw " + redacted, source_type="experience", status="approved")
    sl = access.ensure_shared_lesson(lesson, owner)
    access.save_scrub_ready(sl, redacted_text=redacted, content_hash="h")
    access.create_grant(sl, edge, granted_by=owner.user)
    return sl


# ── The hard cap of 12 ────────────────────────────────────────────────────────


class SkyCapTest(TestCase):
    def setUp(self):
        self.viewer = _tenant("mj")
        _profile(self.viewer, "mj")
        # 13 accepted neighbors — one more than the cap.
        self.edges = [_neighbor(self.viewer, f"n{i}")[1] for i in range(13)]

    def test_twelve_add_then_thirteenth_rejected_structured(self):
        for edge in self.edges[:12]:
            payload, code = services.add_neighbor_to_sky(self.viewer, edge.id)
            self.assertEqual(code, 201)
            self.assertTrue(payload["created"])
            self.assertTrue(payload["in_my_sky"])
        self.assertEqual(access.sky_count(self.viewer), 12)

        # The 13th is a genuinely new add at capacity → structured 409, nothing created.
        payload, code = services.add_neighbor_to_sky(self.viewer, self.edges[12].id)
        self.assertEqual(code, 409)
        self.assertEqual(payload["error"], "sky_full")
        self.assertEqual(payload["cap"], 12)
        # The current members ride along so the client can render the swap.
        self.assertEqual(len(payload["sky"]), 12)
        self.assertEqual(access.sky_count(self.viewer), 12)  # unchanged — no 13th row
        self.assertFalse(SkyMembership.objects.filter(viewer_tenant=self.viewer, friendship=self.edges[12]).exists())

    def test_cap_never_blocks_an_already_chosen_edge(self):
        # Fill to 12, then re-add one already in the sky → 200 (idempotent), never 409.
        for edge in self.edges[:12]:
            services.add_neighbor_to_sky(self.viewer, edge.id)
        payload, code = services.add_neighbor_to_sky(self.viewer, self.edges[0].id)
        self.assertEqual(code, 200)
        self.assertFalse(payload["created"])
        self.assertEqual(access.sky_count(self.viewer), 12)

    def test_removing_makes_room_for_the_thirteenth(self):
        for edge in self.edges[:12]:
            services.add_neighbor_to_sky(self.viewer, edge.id)
        services.remove_neighbor_from_sky(self.viewer, self.edges[0].id)  # free a slot
        payload, code = services.add_neighbor_to_sky(self.viewer, self.edges[12].id)
        self.assertEqual(code, 201)
        self.assertTrue(payload["created"])
        self.assertEqual(access.sky_count(self.viewer), 12)

    def test_endpoint_full_returns_409_body(self):
        for edge in self.edges[:12]:
            services.add_neighbor_to_sky(self.viewer, edge.id)
        resp = _client(self.viewer.user).post(f"/api/v1/friends/{self.edges[12].id}/sky/")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data["error"], "sky_full")
        self.assertEqual(resp.data["cap"], 12)


# ── Idempotency (both directions) ─────────────────────────────────────────────


class SkyIdempotencyTest(TestCase):
    def setUp(self):
        self.viewer = _tenant("mj")
        _profile(self.viewer, "mj")
        _, self.edge = _neighbor(self.viewer, "kiho")

    def test_add_twice_is_idempotent(self):
        p1, c1 = services.add_neighbor_to_sky(self.viewer, self.edge.id)
        self.assertEqual((c1, p1["created"]), (201, True))
        p2, c2 = services.add_neighbor_to_sky(self.viewer, self.edge.id)
        self.assertEqual((c2, p2["created"]), (200, False))
        self.assertEqual(SkyMembership.objects.filter(viewer_tenant=self.viewer).count(), 1)

    def test_remove_twice_is_idempotent(self):
        services.add_neighbor_to_sky(self.viewer, self.edge.id)
        r1 = services.remove_neighbor_from_sky(self.viewer, self.edge.id)
        self.assertTrue(r1["removed"])
        self.assertFalse(r1["in_my_sky"])
        r2 = services.remove_neighbor_from_sky(self.viewer, self.edge.id)
        self.assertFalse(r2["removed"])  # already gone — still a clean 200
        self.assertEqual(SkyMembership.objects.filter(viewer_tenant=self.viewer).count(), 0)

    def test_remove_is_not_an_unfriend(self):
        services.add_neighbor_to_sky(self.viewer, self.edge.id)
        services.remove_neighbor_from_sky(self.viewer, self.edge.id)
        self.edge.refresh_from_db()
        self.assertEqual(self.edge.status, Friendship.Status.ACCEPTED)  # the edge is untouched

    def test_endpoint_add_delete_status_codes(self):
        client = _client(self.viewer.user)
        r_add = client.post(f"/api/v1/friends/{self.edge.id}/sky/")
        self.assertEqual(r_add.status_code, 201)
        self.assertTrue(r_add.data["in_my_sky"])
        r_add2 = client.post(f"/api/v1/friends/{self.edge.id}/sky/")
        self.assertEqual(r_add2.status_code, 200)
        r_del = client.delete(f"/api/v1/friends/{self.edge.id}/sky/")
        self.assertEqual(r_del.status_code, 200)
        self.assertFalse(r_del.data["in_my_sky"])


# ── Privacy: one-way + invisible ──────────────────────────────────────────────


class SkyPrivacyTest(TestCase):
    def setUp(self):
        self.a = _tenant("aya")
        self.b = _tenant("ben")
        _profile(self.a, "aya")
        _profile(self.b, "ben")
        self.edge = _accepted_edge(self.a, self.b)  # a↔b, one shared edge
        # A chooses B for A's sky. B chooses nobody.
        services.add_neighbor_to_sky(self.a, self.edge.id)

    def test_choice_is_invisible_in_the_other_sides_home_feed(self):
        # A's home shows B in A's sky; B's home shows A NOT in sky (B never chose A).
        a_home = services.neighborhood_home(self.a)
        b_home = services.neighborhood_home(self.b)
        self.assertTrue(a_home["neighbors"][0]["in_my_sky"])
        self.assertFalse(b_home["neighbors"][0]["in_my_sky"])

    def test_b_cannot_read_a_sky_roster(self):
        # B's own roster is empty even though A put B in A's sky.
        self.assertEqual(services.list_sky(self.b)["sky"], [])
        self.assertEqual(services.list_sky(self.a)["count"], 1)
        # And the endpoint only ever reflects the caller's own sky.
        resp = _client(self.b.user).get("/api/v1/friends/sky/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["sky"], [])

    def test_b_cannot_mutate_a_sky(self):
        # B removing "the edge" only ever touches B's own (empty) sky — A's pick survives.
        services.remove_neighbor_from_sky(self.b, self.edge.id)
        self.assertEqual(access.sky_count(self.a), 1)
        self.assertTrue(SkyMembership.objects.filter(viewer_tenant=self.a, friendship=self.edge).exists())

    def test_both_can_choose_each_other_independently(self):
        services.add_neighbor_to_sky(self.b, self.edge.id)  # B now also chooses A
        self.assertTrue(services.neighborhood_home(self.a)["neighbors"][0]["in_my_sky"])
        self.assertTrue(services.neighborhood_home(self.b)["neighbors"][0]["in_my_sky"])
        # Two rows on one edge — one per viewer, never a shared/mutual flag.
        self.assertEqual(SkyMembership.objects.filter(friendship=self.edge).count(), 2)


# ── in_my_sky on the home BFF (the iOS flight contract) ───────────────────────


class SkyHomeBFFTest(TestCase):
    def setUp(self):
        self.viewer = _tenant("mj")
        _profile(self.viewer, "mj")
        self.k_tenant, self.k_edge = _neighbor(self.viewer, "kiho")
        self.h_tenant, self.h_edge = _neighbor(self.viewer, "hana")

    def _row(self, home, friendship_id):
        return next(n for n in home["neighbors"] if n["friendship_id"] == str(friendship_id))

    def test_in_my_sky_flag_tracks_membership_per_row(self):
        home = services.neighborhood_home(self.viewer)
        # Default: nobody chosen → every row False (backward-compatible for old clients
        # that ignore the field entirely).
        self.assertFalse(self._row(home, self.k_edge.id)["in_my_sky"])
        self.assertFalse(self._row(home, self.h_edge.id)["in_my_sky"])

        services.add_neighbor_to_sky(self.viewer, self.k_edge.id)
        home = services.neighborhood_home(self.viewer)
        self.assertTrue(self._row(home, self.k_edge.id)["in_my_sky"])
        self.assertFalse(self._row(home, self.h_edge.id)["in_my_sky"])  # only the chosen one

    def test_endpoint_home_carries_in_my_sky(self):
        services.add_neighbor_to_sky(self.viewer, self.k_edge.id)
        resp = _client(self.viewer.user).get("/api/v1/friends/home/")
        self.assertEqual(resp.status_code, 200)
        row = self._row(resp.data, self.k_edge.id)
        self.assertTrue(row["in_my_sky"])


# ── in_my_sky + warpable=sky on the wormholes payload (web parity) ────────────


class SkyWormholesTest(TestCase):
    def setUp(self):
        self.viewer = _tenant("mj")
        _profile(self.viewer, "mj")
        self.k_tenant, self.k_edge = _neighbor(self.viewer, "kiho")
        self.h_tenant, self.h_edge = _neighbor(self.viewer, "hana")
        _share(self.k_tenant, self.k_edge, redacted="kiho spark")  # both have gates (spark_count>0)
        _share(self.h_tenant, self.h_edge, redacted="hana spark")
        services.add_neighbor_to_sky(self.viewer, self.k_edge.id)  # only Kiho is in the sky

    def test_in_my_sky_on_each_gate(self):
        wormholes = {w["friendship_id"]: w for w in services.list_wormholes(self.viewer)}
        self.assertTrue(wormholes[str(self.k_edge.id)]["in_my_sky"])
        self.assertFalse(wormholes[str(self.h_edge.id)]["in_my_sky"])

    def test_warpable_sky_returns_only_the_inner_circle(self):
        sky_only = services.list_wormholes(self.viewer, warpable="sky")
        self.assertEqual([w["friendship_id"] for w in sky_only], [str(self.k_edge.id)])
        # Unfiltered still returns both gates (the filter only ever narrows).
        self.assertEqual(len(services.list_wormholes(self.viewer)), 2)

    def test_endpoint_warpable_sky_filter(self):
        resp = _client(self.viewer.user).get("/api/v1/friends/wormholes/?warpable=sky")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 1)
        self.assertEqual(resp.data[0]["friendship_id"], str(self.k_edge.id))


# ── The sky roster (GET /sky/) incl. quiet no-spark slots ─────────────────────


class SkyRosterTest(TestCase):
    def setUp(self):
        self.viewer = _tenant("mj")
        _profile(self.viewer, "mj")
        self.k_tenant, self.k_edge = _neighbor(self.viewer, "kiho")  # will share (live gate)
        self.q_tenant, self.q_edge = _neighbor(self.viewer, "quinn")  # quiet: chosen, no spark
        _share(self.k_tenant, self.k_edge, redacted="kiho spark")
        services.add_neighbor_to_sky(self.viewer, self.k_edge.id)
        services.add_neighbor_to_sky(self.viewer, self.q_edge.id)

    def test_roster_includes_quiet_no_spark_slots(self):
        payload = services.list_sky(self.viewer)
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["cap"], 12)
        by_edge = {r["friendship_id"]: r for r in payload["sky"]}
        self.assertFalse(by_edge[str(self.k_edge.id)]["quiet_slot"])  # has a spark
        self.assertTrue(by_edge[str(self.q_edge.id)]["quiet_slot"])  # chosen, waiting to share
        self.assertTrue(all(r["in_my_sky"] for r in payload["sky"]))

    def test_roster_drops_a_row_whose_edge_is_no_longer_accepted(self):
        services.unfriend(self.viewer, self.q_edge.id)  # revokes the edge (row lingers, like WormholeVisit)
        payload = services.list_sky(self.viewer)
        self.assertEqual([r["friendship_id"] for r in payload["sky"]], [str(self.k_edge.id)])


# ── Add gating: accepted party only (IDOR dead by construction) ───────────────


class SkyAddGateTest(TestCase):
    def setUp(self):
        self.viewer = _tenant("mj")
        _profile(self.viewer, "mj")
        _, self.edge = _neighbor(self.viewer, "kiho")

    def test_stranger_friendship_id_is_403(self):
        stranger_a = _tenant("sa")
        stranger_b = _tenant("sb")
        foreign_edge = _accepted_edge(stranger_a, stranger_b)
        resp = _client(self.viewer.user).post(f"/api/v1/friends/{foreign_edge.id}/sky/")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(access.sky_count(self.viewer), 0)

    def test_pending_edge_cannot_be_added(self):
        pending_other = _tenant("pending")
        pending_edge = Friendship.objects.create(
            requester=self.viewer, addressee=pending_other, status=Friendship.Status.PENDING
        )
        resp = _client(self.viewer.user).post(f"/api/v1/friends/{pending_edge.id}/sky/")
        self.assertEqual(resp.status_code, 403)

    def test_friends_disabled_tenant_is_gated(self):
        off = _tenant("off", friends_enabled=False)
        resp = _client(off.user).get("/api/v1/friends/sky/")
        self.assertEqual(resp.status_code, 403)


# ── RLS: the relock lands the new table enabled, with no policy (BN-PR1) ──────


class SkyRlsRelockTest(TestCase):
    """BN-PR1's relock (tenants.0106_relock_after_sky) must land
    ``friend_sky_memberships`` RLS-ENABLED (the anon Supabase Data API sees
    nothing) with NO policy — the FORCE-RLS + GUC SELECT policy is the BN-PR6
    defense-in-depth follow-up, not this PR."""

    def test_sky_table_rls_enabled_with_no_policy(self):
        with connection.cursor() as cur:
            cur.execute(
                "SELECT rowsecurity FROM pg_tables WHERE schemaname='public' AND tablename=%s",
                ["friend_sky_memberships"],
            )
            row = cur.fetchone()
            self.assertIsNotNone(row, "friend_sky_memberships table is missing")
            self.assertTrue(row[0], "friend_sky_memberships must have RLS enabled after the relock migration")
            cur.execute(
                "SELECT policyname FROM pg_policies WHERE schemaname='public' AND tablename=%s",
                ["friend_sky_memberships"],
            )
            self.assertEqual(cur.fetchall(), [], "BN-PR1 adds NO policy on the sky table (FORCE-RLS is BN-PR6)")
