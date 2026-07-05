"""PR4 behavioral tests — agent integration (propose → approve + backstage absorb).

Blueprint non-negotiables asserted here: agents PROPOSE only (never a grant, never
post), absorption is quiet + idempotent + honors purge, the context never returns
raw Lesson text, and the AGENTS.md backstage gate lands before the Gravity block.
"""

from __future__ import annotations

from unittest import mock

from django.test import TestCase
from django.test.utils import override_settings
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.test import APIClient

from apps.lessons.models import Lesson
from apps.orchestrator.personas import render_workspace_files
from apps.tenants.models import Tenant, User
from apps.tenants.test_utils import seed_internal_key

from . import access, envelope, services
from .models import AbsorbedItem, Friendship, LessonShareGrant, NeighborProfile, PendingShare

_RUNTIME = "/api/v1/integrations/runtime"


def _tenant(username, *, friends_enabled=True, finance_enabled=False):
    user = User.objects.create_user(username=username, password="pass", display_name=username.title())
    return Tenant.objects.create(
        user=user, status="active", friends_enabled=friends_enabled, finance_enabled=finance_enabled
    )


def _profile(tenant, handle):
    return NeighborProfile.objects.create(tenant=tenant, handle=handle, display_name=handle.title())


def _edge(a, b):
    return Friendship.objects.create(requester=a, addressee=b, status=Friendship.Status.ACCEPTED)


def _lesson(tenant, text="Batch-cook Sundays.", *, tags=None):
    return Lesson.objects.create(
        tenant=tenant, text=text, context="", source_type="experience", status="approved", tags=tags or []
    )


def _shared_ready(owner, lesson, friendship, *, redacted="someone batch-cooks on Sundays"):
    """Owner shares `lesson` to `friendship`, marked ready + granted (no scrub)."""
    sl = access.ensure_shared_lesson(lesson, owner)
    access.save_scrub_ready(sl, redacted_text=redacted, content_hash="h")
    access.create_grant(sl, friendship, granted_by=owner.user)
    return sl


# ── Agent propose (proposal only, never a grant) ─────────────────────────────


class ProposeShareServiceTest(TestCase):
    def setUp(self):
        self.a = _tenant("prop_a")
        self.b = _tenant("prop_b")
        self.edge = _edge(self.a, self.b)
        self.lesson = _lesson(self.a)

    def test_propose_creates_pending_agent_not_grant(self):
        with mock.patch("apps.friends.services._enqueue_scrub"):
            pending, created = services.propose_share(self.a, self.lesson, self.edge, "would help b")
        self.assertTrue(created)
        self.assertEqual(pending.proposed_by, "agent")
        self.assertEqual(pending.status, "pending")
        self.assertEqual(pending.source_context, "would help b")
        self.assertEqual(LessonShareGrant.objects.count(), 0)  # NEVER a grant

    def test_propose_is_idempotent(self):
        with mock.patch("apps.friends.services._enqueue_scrub"):
            p1, c1 = services.propose_share(self.a, self.lesson, self.edge, "x")
            p2, c2 = services.propose_share(self.a, self.lesson, self.edge, "y")
        self.assertTrue(c1)
        self.assertFalse(c2)
        self.assertEqual(p1.id, p2.id)
        self.assertEqual(PendingShare.objects.filter(source_lesson=self.lesson).count(), 1)

    def test_propose_foreign_lesson_404(self):
        other = _tenant("prop_other")
        foreign = _lesson(other)
        with mock.patch("apps.friends.services._enqueue_scrub"), self.assertRaises(NotFound):
            services.propose_share(self.a, foreign, self.edge, "x")

    def test_propose_pillar_blocked(self):
        finance = _lesson(self.a, text="Refi the loan.", tags=["finance"])
        with mock.patch("apps.friends.services._enqueue_scrub"), self.assertRaises(PermissionDenied):
            services.propose_share(self.a, finance, self.edge, "x")

    def test_resolve_friendship_by_handle_and_id(self):
        _profile(self.b, "bee")
        self.assertEqual(services.resolve_accepted_friendship(self.a, handle="bee").id, self.edge.id)
        self.assertEqual(services.resolve_accepted_friendship(self.a, friendship_id=str(self.edge.id)).id, self.edge.id)
        self.assertIsNone(services.resolve_accepted_friendship(self.a, handle="nobody"))


# ── Backstage absorb (quiet, idempotent, purge-honoring, no raw text) ────────


class NeighborhoodContextTest(TestCase):
    def setUp(self):
        self.owner = _tenant("ctx_owner")
        self.viewer = _tenant("ctx_viewer")
        _profile(self.owner, "owner")
        _profile(self.viewer, "viewer")
        self.edge = _edge(self.owner, self.viewer)
        self.lesson = _lesson(self.owner, text="RAW_SECRET my real name is Kenji")
        self.sl = _shared_ready(self.owner, self.lesson, self.edge, redacted="someone cooks on Sundays")

    def test_context_returns_visible_spark_no_raw_text(self):
        ctx = services.neighborhood_context(self.viewer)
        self.assertEqual(len(ctx["sparks"]), 1)
        spark = ctx["sparks"][0]
        self.assertEqual(spark["shared_lesson_id"], str(self.sl.id))
        self.assertEqual(spark["from_handle"], "owner")
        self.assertEqual(spark["text"], "someone cooks on Sundays")
        # NEVER leaks the raw lesson text.
        self.assertNotIn("RAW_SECRET", str(ctx))
        self.assertIn("owner", ctx["neighbors"])

    def test_absorb_logged_once_no_reabsorb(self):
        services.neighborhood_context(self.viewer)
        services.neighborhood_context(self.viewer)
        self.assertEqual(AbsorbedItem.objects.filter(tenant=self.viewer, source_id=self.sl.id).count(), 1)

    def test_non_neighbor_sees_nothing(self):
        stranger = _tenant("ctx_stranger")
        ctx = services.neighborhood_context(stranger)
        self.assertEqual(ctx["sparks"], [])

    def test_purged_spark_excluded_from_context(self):
        services.neighborhood_context(self.viewer)  # logs the AbsorbedItem
        item = AbsorbedItem.objects.get(tenant=self.viewer, source_id=self.sl.id)
        services.purge_absorbed(self.viewer, item.id)
        ctx = services.neighborhood_context(self.viewer)
        self.assertEqual(ctx["sparks"], [])  # human purged it — respected

    def test_revoked_grant_drops_from_context(self):
        grant = LessonShareGrant.objects.get()
        access.revoke_grant(grant)
        ctx = services.neighborhood_context(self.viewer)
        self.assertEqual(ctx["sparks"], [])


# ── Transparency ledger ──────────────────────────────────────────────────────


class AbsorbedLedgerTest(TestCase):
    def setUp(self):
        self.owner = _tenant("led_owner")
        self.viewer = _tenant("led_viewer")
        _profile(self.owner, "ledowner")
        self.edge = _edge(self.owner, self.viewer)
        self.lesson = _lesson(self.owner)
        _shared_ready(self.owner, self.lesson, self.edge)
        services.neighborhood_context(self.viewer)  # populate the ledger

    def test_list_and_purge_via_http(self):
        client = APIClient()
        client.force_authenticate(user=self.viewer.user)
        resp = client.get("/api/v1/friends/absorbed/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), 1)
        item_id = resp.json()[0]["id"]
        self.assertEqual(resp.json()[0]["from_handle"], "ledowner")

        purge = client.post(f"/api/v1/friends/absorbed/{item_id}/purge/")
        self.assertEqual(purge.status_code, 200)
        # Purged → excluded from the ledger list.
        self.assertEqual(client.get("/api/v1/friends/absorbed/").json(), [])


# ── Envelope ─────────────────────────────────────────────────────────────────


class EnvelopeRenderTest(TestCase):
    def test_renders_neighbors_and_sparks_excludes_purged(self):
        owner = _tenant("env_owner")
        viewer = _tenant("env_viewer")
        _profile(owner, "kenji")
        _profile(viewer, "me")
        edge = _edge(owner, viewer)
        lesson = _lesson(owner)
        _shared_ready(owner, lesson, edge)
        services.neighborhood_context(viewer)  # logs the AbsorbedItem

        out = envelope.render_neighborhood(viewer)
        self.assertIn("@kenji", out)
        self.assertIn("Neighbors:", out)
        self.assertLess(len(out), 1024)  # TIGHT

        item = AbsorbedItem.objects.get(tenant=viewer)
        services.purge_absorbed(viewer, item.id)
        out2 = envelope.render_neighborhood(viewer)
        # Neighbor still listed, but the purged spark line is gone.
        self.assertIn("@kenji", out2)
        self.assertNotIn("- ", out2)

    def test_empty_when_no_data(self):
        t = _tenant("env_empty")
        self.assertEqual(envelope.render_neighborhood(t), "")

    def test_never_raises(self):
        # A tenant object that will blow up any query still yields "".
        self.assertEqual(envelope.render_neighborhood(object()), "")

    def test_grant_receiver_targets_recipient(self):
        owner = _tenant("rcv_owner")
        viewer = _tenant("rcv_viewer")
        edge = _edge(owner, viewer)
        lesson = _lesson(owner)
        sl = access.ensure_shared_lesson(lesson, owner)
        access.save_scrub_ready(sl, redacted_text="x", content_hash="h")
        captured = {}
        with mock.patch(
            "apps.friends.envelope._schedule_recipient_push", side_effect=lambda tid: captured.update(id=tid)
        ):
            access.create_grant(sl, edge, granted_by=owner.user)
        # The RECIPIENT (viewer), not the owner, is refreshed.
        self.assertEqual(str(captured.get("id")), str(viewer.id))


# ── AGENTS.md backstage gate ─────────────────────────────────────────────────


class AgentsGateTest(TestCase):
    def test_gate_present_before_gravity_when_enabled(self):
        # finance_enabled=True so the Gravity block also renders (ordering check).
        tenant = _tenant("gate_on", friends_enabled=True, finance_enabled=True)

        agents_md = render_workspace_files("neighbor", tenant=tenant)["NBHD_AGENTS_MD"]
        self.assertIn("## Neighborhood — you are BACKSTAGE", agents_md)
        self.assertIn("nbhd_propose_lesson_share", agents_md)
        self.assertIn("a human must approve before anything is shared", agents_md)
        # Placed BEFORE the Gravity block (so it can't be the truncated tail).
        if "## Gravity Observation Mode" in agents_md:
            self.assertLess(
                agents_md.index("## Neighborhood — you are BACKSTAGE"),
                agents_md.index("## Gravity Observation Mode"),
            )

    def test_gate_absent_when_disabled(self):
        tenant = _tenant("gate_off", friends_enabled=False)
        agents_md = render_workspace_files("neighbor", tenant=tenant)["NBHD_AGENTS_MD"]
        self.assertNotIn("## Neighborhood — you are BACKSTAGE", agents_md)


# ── Runtime endpoints (internal-key auth; no foreign tenant_id; no approve) ───


@override_settings(NBHD_INTERNAL_API_KEY="shared-key")
class RuntimeProposeShareHttpTest(TestCase):
    def setUp(self):
        self.a = seed_internal_key(_tenant("rt_a"))
        self.b = _tenant("rt_b")
        _profile(self.b, "rtb")
        self.edge = _edge(self.a, self.b)
        self.lesson = _lesson(self.a)
        self.headers = {"HTTP_X_NBHD_INTERNAL_KEY": "shared-key", "HTTP_X_NBHD_TENANT_ID": str(self.a.id)}
        self.url = f"{_RUNTIME}/{self.a.id}/lessons/{self.lesson.id}/propose-share/"

    def test_propose_via_handle_creates_pending(self):
        with mock.patch("apps.friends.services._enqueue_scrub"):
            resp = self.client.post(
                self.url,
                {"target_handle": "rtb", "source_context": "why"},
                content_type="application/json",
                **self.headers,
            )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["status"], "pending")
        self.assertEqual(LessonShareGrant.objects.count(), 0)
        self.assertEqual(PendingShare.objects.filter(proposed_by="agent").count(), 1)

    def test_no_internal_key_401(self):
        resp = self.client.post(self.url, {"target_handle": "rtb"}, content_type="application/json")
        self.assertEqual(resp.status_code, 401)

    def test_foreign_lesson_404(self):
        other = _lesson(_tenant("rt_other"))
        url = f"{_RUNTIME}/{self.a.id}/lessons/{other.id}/propose-share/"
        with mock.patch("apps.friends.services._enqueue_scrub"):
            resp = self.client.post(url, {"target_handle": "rtb"}, content_type="application/json", **self.headers)
        self.assertEqual(resp.status_code, 404)

    def test_non_party_friendship_403(self):
        stranger = _tenant("rt_stranger")
        _profile(stranger, "rtstranger")
        with mock.patch("apps.friends.services._enqueue_scrub"):
            resp = self.client.post(
                self.url, {"target_handle": "rtstranger"}, content_type="application/json", **self.headers
            )
        self.assertEqual(resp.status_code, 403)

    def test_no_runtime_approve_endpoint(self):
        # The agent (internal key) has NO way to approve — the approve path lives
        # only on the JWT console. A runtime approve route does not exist.
        pending_id = "00000000-0000-0000-0000-000000000000"
        resp = self.client.post(
            f"{_RUNTIME}/{self.a.id}/shares/{pending_id}/approve/", {}, content_type="application/json", **self.headers
        )
        self.assertEqual(resp.status_code, 404)
        # And the console approve rejects internal-key-only (no JWT) callers.
        console = self.client.post(
            f"/api/v1/friends/shares/{pending_id}/approve/", {}, content_type="application/json", **self.headers
        )
        self.assertIn(console.status_code, (401, 403))


@override_settings(NBHD_INTERNAL_API_KEY="shared-key")
class RuntimeContextHttpTest(TestCase):
    def setUp(self):
        self.owner = _tenant("rtc_owner")
        self.viewer = seed_internal_key(_tenant("rtc_viewer"))
        _profile(self.owner, "rtcowner")
        self.edge = _edge(self.owner, self.viewer)
        self.lesson = _lesson(self.owner)
        _shared_ready(self.owner, self.lesson, self.edge, redacted="someone did a thing")
        self.headers = {"HTTP_X_NBHD_INTERNAL_KEY": "shared-key", "HTTP_X_NBHD_TENANT_ID": str(self.viewer.id)}

    def test_context_returns_sparks_and_cursor(self):
        resp = self.client.get(f"{_RUNTIME}/{self.viewer.id}/neighborhood/context/", **self.headers)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(len(body["sparks"]), 1)
        self.assertEqual(body["sparks"][0]["text"], "someone did a thing")
        self.assertIsNotNone(body["cursor"])
