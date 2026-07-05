"""PR3 behavioral tests — wormholes, warp (read-only friend galaxy), the
souvenir (adopt), the watermark, and coords-only position refresh.

The security spine here is the same as the whole feature: every cross-tenant
read routes through ``apps.friends.access`` and returns ONLY frozen, scrubbed,
active+ready ``SharedLesson`` snapshots — never the raw ``Lesson`` corpus (which
stores real names), never an unshared or revoked snapshot, never a foreign
galaxy for a non-neighbor.
"""

from __future__ import annotations

from django.test import TestCase
from rest_framework.test import APIClient

from apps.lessons.models import Lesson, StarJournalEntry
from apps.tenants.models import Tenant, User

from . import access, services
from .models import Friendship, NeighborProfile, WormholeVisit


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


def _share(
    owner,
    edge,
    *,
    redacted="someone batch-cooks on sundays",
    raw="Alice batch-cooks on sundays with Bob",
    x=0.2,
    y=-0.3,
    stage="ignited",
    cluster_label="Kitchen wins",
    tags=None,
):
    """Fully publish a scrubbed, ready, active-granted snapshot from `owner` on
    `edge`. Returns (lesson, shared_lesson, grant)."""
    tags = tags or ["cooking", "habits"]
    lesson = Lesson.objects.create(
        tenant=owner,
        text=raw,
        source_type="experience",
        status="approved",
        position_x=x,
        position_y=y,
        star_stage=stage,
        cluster_label=cluster_label,
        tags=tags,
    )
    sl = access.ensure_shared_lesson(lesson, owner)
    access.save_scrub_ready(
        sl,
        redacted_text=redacted,
        content_hash="h",
        position_x=x,
        position_y=y,
        star_stage=stage,
        cluster_label=cluster_label,
        tags=tags,
    )
    grant = access.create_grant(sl, edge, granted_by=owner.user)
    return lesson, sl, grant


# ── Friend galaxy endpoint (read-only) ────────────────────────────────────────


class FriendGalaxyTest(TestCase):
    def setUp(self):
        self.owner = _tenant("kenji")
        self.viewer = _tenant("mika")
        _profile(self.owner, "kenji", hue=140)
        _profile(self.viewer, "mika")
        self.edge = _accepted_edge(self.owner, self.viewer)
        self.lesson, self.sl, self.grant = _share(self.owner, self.edge)

    def test_returns_only_ready_active_shared_snapshots_namespaced(self):
        payload = services.friend_galaxy(self.viewer, self.edge.id)
        self.assertEqual(len(payload["stars"]), 1)
        star = payload["stars"][0]
        # Namespaced id: f:<friendship_id>:<shared_lesson_id> — can't collide with
        # a home Lesson PK or be replayed against owner-scoped /lessons/ endpoints.
        self.assertEqual(star["id"], f"f:{self.edge.id}:{self.sl.id}")
        self.assertEqual(star["shared_lesson_id"], str(self.sl.id))
        # The FROZEN scrubbed text only — never the raw lesson corpus.
        self.assertEqual(star["text"], "someone batch-cooks on sundays")
        self.assertNotIn("Alice", star["text"])
        self.assertNotIn("Bob", star["text"])
        self.assertEqual(star["x"], 0.2)
        self.assertEqual(star["y"], -0.3)
        self.assertEqual(payload["edges"], [])  # edges omitted for MVP (one less leak surface)
        self.assertEqual(len(payload["clusters"]), 1)
        self.assertEqual(payload["clusters"][0]["label"], "Kitchen wins")

    def test_never_leaks_raw_lesson_or_star_journal(self):
        # A star journal entry on the owner's lesson must never surface anywhere
        # in the friend payload (the friend path touches only SharedLesson).
        StarJournalEntry.objects.create(tenant=self.owner, star=self.lesson, text="secret private reflection about Bob")
        payload = services.friend_galaxy(self.viewer, self.edge.id)
        blob = str(payload)
        self.assertNotIn("secret private reflection", blob)
        self.assertNotIn("Alice", blob)

    def test_unshared_snapshot_absent(self):
        # A second lesson the owner has NOT shared must not appear.
        Lesson.objects.create(
            tenant=self.owner, text="unshared private lesson", source_type="experience", status="approved"
        )
        payload = services.friend_galaxy(self.viewer, self.edge.id)
        self.assertEqual(len(payload["stars"]), 1)

    def test_revoked_grant_disappears_immediately(self):
        access.revoke_grant(self.grant)
        payload = services.friend_galaxy(self.viewer, self.edge.id)
        self.assertEqual(payload["stars"], [])

    def test_failed_scrub_never_visible(self):
        _lesson2 = Lesson.objects.create(tenant=self.owner, text="raw", source_type="experience", status="approved")
        sl2 = access.ensure_shared_lesson(_lesson2, self.owner)
        access.save_scrub_failed(sl2, "ner unavailable")
        access.create_grant(sl2, self.edge, granted_by=self.owner.user)
        payload = services.friend_galaxy(self.viewer, self.edge.id)
        self.assertEqual(len(payload["stars"]), 1)  # only the ready one

    def test_non_party_denied(self):
        stranger = _tenant("stranger")
        with self.assertRaises(Exception) as ctx:
            services.friend_galaxy(stranger, self.edge.id)
        self.assertIn("party", str(ctx.exception).lower())

    def test_endpoint_returns_stars_and_denies_stranger(self):
        resp = _client(self.viewer.user).get(f"/api/v1/friends/{self.edge.id}/galaxy/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data["stars"]), 1)

        stranger = _tenant("stranger2")
        resp2 = _client(stranger.user).get(f"/api/v1/friends/{self.edge.id}/galaxy/")
        self.assertEqual(resp2.status_code, 403)


# ── Wormholes list ────────────────────────────────────────────────────────────


class WormholesListTest(TestCase):
    def setUp(self):
        self.viewer = _tenant("viewer")
        _profile(self.viewer, "viewer")
        self.kenji = _tenant("kenji")
        _profile(self.kenji, "kenji", hue=140)
        self.edge = _accepted_edge(self.kenji, self.viewer)

    def test_neighbor_with_zero_ready_grants_absent(self):
        # Accepted edge but nothing shared yet → no gate.
        self.assertEqual(services.list_wormholes(self.viewer), [])

    def test_spark_count_and_new_since_last_visit(self):
        _share(self.kenji, self.edge, redacted="spark one")
        _share(self.kenji, self.edge, redacted="spark two")
        wormholes = services.list_wormholes(self.viewer)
        self.assertEqual(len(wormholes), 1)
        w = wormholes[0]
        self.assertEqual(w["friendship_id"], str(self.edge.id))
        self.assertEqual(w["display_name"], "Kenji")
        self.assertEqual(w["avatar_hue"], 140)
        self.assertEqual(w["spark_count"], 2)
        # No watermark yet → everything is "new".
        self.assertEqual(w["new_since_last_visit"], 2)

    def test_new_since_last_visit_respects_watermark(self):
        _share(self.kenji, self.edge, redacted="old spark")
        # Visit now → watermark advances past the first spark.
        access.upsert_wormhole_visit(self.viewer, self.edge)
        _share(self.kenji, self.edge, redacted="fresh spark")
        w = services.list_wormholes(self.viewer)[0]
        self.assertEqual(w["spark_count"], 2)
        self.assertEqual(w["new_since_last_visit"], 1)  # only the post-visit share is new

    def test_only_counts_the_neighbors_shares_not_my_own(self):
        # Both directions can share on the same edge; the viewer's own shares to
        # Kenji must NOT inflate Kenji's gate.
        _share(self.kenji, self.edge, redacted="kenji's spark")
        _share(self.viewer, self.edge, redacted="my spark to kenji")
        w = services.list_wormholes(self.viewer)[0]
        self.assertEqual(w["spark_count"], 1)

    def test_endpoint(self):
        _share(self.kenji, self.edge)
        resp = _client(self.viewer.user).get("/api/v1/friends/wormholes/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 1)


# ── Watermark (visited) ───────────────────────────────────────────────────────


class WormholeVisitedTest(TestCase):
    def setUp(self):
        self.viewer = _tenant("viewer")
        self.kenji = _tenant("kenji")
        self.edge = _accepted_edge(self.kenji, self.viewer)

    def test_visit_upsert_is_idempotent(self):
        services.mark_wormhole_visited(self.viewer, self.edge.id)
        first = WormholeVisit.objects.get(viewer_tenant=self.viewer, friendship=self.edge)
        services.mark_wormhole_visited(self.viewer, self.edge.id)
        # Still exactly one row (unique constraint holds; upsert, not insert).
        self.assertEqual(WormholeVisit.objects.filter(viewer_tenant=self.viewer, friendship=self.edge).count(), 1)
        first.refresh_from_db()
        self.assertIsNotNone(first.last_visited_at)

    def test_non_party_cannot_advance_watermark(self):
        stranger = _tenant("stranger")
        with self.assertRaises(Exception):
            services.mark_wormhole_visited(stranger, self.edge.id)
        self.assertEqual(WormholeVisit.objects.count(), 0)

    def test_endpoint(self):
        resp = _client(self.viewer.user).post(f"/api/v1/friends/{self.edge.id}/visited/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["friendship_id"], str(self.edge.id))


# ── Souvenir: adopt (bring a spark home) ──────────────────────────────────────


class AdoptSparkTest(TestCase):
    def setUp(self):
        self.owner = _tenant("kenji")
        self.viewer = _tenant("mika")
        _profile(self.owner, "kenji")
        _profile(self.viewer, "mika")
        self.edge = _accepted_edge(self.owner, self.viewer)
        self.lesson, self.sl, self.grant = _share(self.owner, self.edge, redacted="batch-cook on sundays")

    def test_creates_pending_lesson_in_viewer_tenant_with_attribution(self):
        payload, code = services.adopt_spark(self.viewer, self.viewer.user, self.sl.id)
        self.assertEqual(code, 201)
        self.assertTrue(payload["created"])
        adopted = Lesson.objects.get(id=payload["lesson_id"])
        # Written into the VIEWER's tenant, never the owner's.
        self.assertEqual(adopted.tenant_id, self.viewer.id)
        self.assertEqual(adopted.status, "pending")  # enters the normal approve gate
        self.assertEqual(adopted.source_type, "shared")
        self.assertEqual(adopted.source_ref, f"shared_lesson:{self.sl.id}")
        self.assertEqual(adopted.text, "batch-cook on sundays")
        self.assertIn("@kenji", adopted.context)  # attribution
        # The owner's galaxy is untouched (no new lesson in the owner's tenant).
        self.assertEqual(Lesson.objects.filter(tenant=self.owner).count(), 1)

    def test_idempotent_no_duplicate(self):
        payload1, _ = services.adopt_spark(self.viewer, self.viewer.user, self.sl.id)
        payload2, code2 = services.adopt_spark(self.viewer, self.viewer.user, self.sl.id)
        self.assertEqual(payload1["lesson_id"], payload2["lesson_id"])
        self.assertEqual(code2, 200)
        self.assertFalse(payload2["created"])
        self.assertEqual(Lesson.objects.filter(tenant=self.viewer, source_ref=f"shared_lesson:{self.sl.id}").count(), 1)

    def test_re_adopt_after_dismiss_mints_fresh_pending(self):
        """Dismissing a souvenir means "not now" — the spark is still in the
        friend's galaxy inviting adoption, so a later adopt must mint a FRESH
        pending lesson (a dismissed copy blocking would make the success toast
        lie). Live copies (pending/approved) still dedup."""
        payload1, _ = services.adopt_spark(self.viewer, self.viewer.user, self.sl.id)
        first = Lesson.objects.get(id=payload1["lesson_id"])
        first.status = "dismissed"
        first.save(update_fields=["status"])

        payload2, code2 = services.adopt_spark(self.viewer, self.viewer.user, self.sl.id)
        self.assertEqual(code2, 201)
        self.assertTrue(payload2["created"])
        self.assertNotEqual(payload1["lesson_id"], payload2["lesson_id"])
        fresh = Lesson.objects.get(id=payload2["lesson_id"])
        self.assertEqual(fresh.status, "pending")
        # And the fresh live copy now dedups again.
        payload3, code3 = services.adopt_spark(self.viewer, self.viewer.user, self.sl.id)
        self.assertEqual(payload3["lesson_id"], payload2["lesson_id"])
        self.assertEqual(code3, 200)

    def test_adopting_own_snapshot_is_400(self):
        # The owner tries to adopt their own snapshot.
        resp = _client(self.owner.user).post(f"/api/v1/friends/shares/{self.sl.id}/adopt/")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(Lesson.objects.filter(tenant=self.owner, source_type="shared").count(), 0)

    def test_non_neighbor_denied(self):
        stranger = _tenant("stranger")
        resp = _client(stranger.user).post(f"/api/v1/friends/shares/{self.sl.id}/adopt/")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(Lesson.objects.filter(tenant=stranger).count(), 0)

    def test_revoked_grant_cannot_be_adopted(self):
        access.revoke_grant(self.grant)  # this also deletes the orphaned snapshot
        resp = _client(self.viewer.user).post(f"/api/v1/friends/shares/{self.sl.id}/adopt/")
        self.assertEqual(resp.status_code, 403)

    def test_endpoint_creates_pending(self):
        resp = _client(self.viewer.user).post(f"/api/v1/friends/shares/{self.sl.id}/adopt/")
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(Lesson.objects.filter(tenant=self.viewer, status="pending", source_type="shared").exists())


# ── Coords-only position refresh ──────────────────────────────────────────────


class RefreshSharedPositionsTest(TestCase):
    def setUp(self):
        self.owner = _tenant("kenji")
        self.viewer = _tenant("mika")
        self.edge = _accepted_edge(self.owner, self.viewer)
        self.lesson, self.sl, self.grant = _share(
            self.owner, self.edge, x=0.1, y=0.1, stage="ignited", redacted="frozen scrubbed text"
        )

    def test_copies_coords_only_not_text_or_stage(self):
        # The owner re-clusters: the source lesson's coords move and its stage grows.
        self.lesson.position_x = 0.8
        self.lesson.position_y = -0.6
        self.lesson.star_stage = "radiant"
        self.lesson.text = "a rewritten raw lesson with a new Name"
        self.lesson.save()

        result = services.refresh_shared_positions(str(self.owner.id))
        self.assertEqual(result["updated"], 1)

        self.sl.refresh_from_db()
        self.assertEqual(self.sl.position_x, 0.8)  # coords copied forward
        self.assertEqual(self.sl.position_y, -0.6)
        # Everything else stays FROZEN at its scrubbed value — no new PII crosses.
        self.assertEqual(self.sl.star_stage, "ignited")
        self.assertEqual(self.sl.redacted_text, "frozen scrubbed text")

    def test_noop_when_unchanged(self):
        result = services.refresh_shared_positions(str(self.owner.id))
        self.assertEqual(result["updated"], 0)

    def test_ignores_failed_snapshots(self):
        _l2 = Lesson.objects.create(
            tenant=self.owner, text="raw", source_type="experience", status="approved", position_x=0.0, position_y=0.0
        )
        sl2 = access.ensure_shared_lesson(_l2, self.owner)
        access.save_scrub_failed(sl2, "blocked")
        _l2.position_x = 0.5
        _l2.save()
        services.refresh_shared_positions(str(self.owner.id))
        sl2.refresh_from_db()
        self.assertIsNone(sl2.position_x)  # failed snapshot never touched

    def test_recluster_enqueues_refresh_when_friends_enabled(self):
        # The debounce seam: a recluster copies coords forward onto ready snapshots
        # (QStash runs inline in tests). Move the source coords, recluster, assert
        # the snapshot followed.
        from apps.lessons.clustering import _enqueue_shared_position_refresh

        self.lesson.position_x = 0.42
        self.lesson.save()
        _enqueue_shared_position_refresh(self.owner)
        self.sl.refresh_from_db()
        self.assertEqual(self.sl.position_x, 0.42)

    def test_recluster_hook_noop_when_friends_disabled(self):
        from apps.lessons.clustering import _enqueue_shared_position_refresh

        plain = _tenant("plain", friends_enabled=False)
        # Should return without enqueuing anything (no crash, no work).
        self.assertIsNone(_enqueue_shared_position_refresh(plain))
