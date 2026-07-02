"""Tests for the North Star (Purpose) layer.

Covers: model transitions, console (session-auth) endpoints, runtime
(internal-auth) endpoints incl. the consent gate, the USER.md envelope section
(present/absent/char-cap/proposed-excluded), the reconcile direction branch,
the journal-context payload, and the nightly extraction purpose-hypothesis card
(creation, cross-pillar requirement, sparse guard, approval → confirmed
Purpose).
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.journal.extraction import run_extraction_for_tenant
from apps.journal.models import Document, Goal, PendingExtraction, Purpose
from apps.tenants.models import Tenant, User
from apps.tenants.services import create_tenant
from apps.tenants.test_utils import seed_internal_key

# ── Model transitions ──────────────────────────────────────────────────────


class PurposeModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="pmodel", password="x")
        self.tenant = Tenant.objects.create(user=self.user, status="active")

    def test_default_status_proposed(self):
        p = Purpose.objects.create(tenant=self.tenant, statement="Live a life of service")
        self.assertEqual(p.status, Purpose.Status.PROPOSED)
        self.assertEqual(p.origin, Purpose.Origin.USER_CREATED)

    def test_confirm_stamps_timestamp(self):
        p = Purpose.objects.create(tenant=self.tenant, statement="Build something lasting")
        p.confirm()
        p.refresh_from_db()
        self.assertEqual(p.status, Purpose.Status.CONFIRMED)
        self.assertIsNotNone(p.confirmed_at)

    def test_confirm_is_idempotent_on_timestamp(self):
        p = Purpose.objects.create(tenant=self.tenant, statement="X")
        p.confirm()
        first = p.confirmed_at
        p.status = Purpose.Status.EVOLVING
        p.save(update_fields=["status"])
        p.confirm()
        p.refresh_from_db()
        self.assertEqual(p.confirmed_at, first)

    def test_retire_stamps_timestamp(self):
        p = Purpose.objects.create(tenant=self.tenant, statement="X")
        p.retire()
        p.refresh_from_db()
        self.assertEqual(p.status, Purpose.Status.RETIRED)
        self.assertIsNotNone(p.retired_at)

    def test_goal_purpose_fk_set_null_on_delete(self):
        p = Purpose.objects.create(tenant=self.tenant, statement="X", status=Purpose.Status.CONFIRMED)
        g = Goal.objects.create(tenant=self.tenant, title="A goal", purpose=p)
        p.delete()
        g.refresh_from_db()
        self.assertIsNone(g.purpose_id)


# ── Console (session-auth) endpoints ───────────────────────────────────────


class PurposeConsoleTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="pconsole", password="x")
        self.tenant = Tenant.objects.create(user=self.user, status="active")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_user_created_purpose_is_confirmed(self):
        resp = self.client.post(
            "/api/v1/journal/purposes/",
            {"statement": "Be present for my family", "pillars": ["gravity", "core"]},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        body = resp.json()
        self.assertEqual(body["status"], "confirmed")
        self.assertEqual(body["origin"], "user_created")
        self.assertIsNotNone(body["confirmed_at"])

    def test_rejects_unknown_pillar(self):
        resp = self.client.post(
            "/api/v1/journal/purposes/",
            {"statement": "X statement here", "pillars": ["not_a_pillar"]},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_list_filters_by_status(self):
        Purpose.objects.create(tenant=self.tenant, statement="one", status=Purpose.Status.CONFIRMED)
        Purpose.objects.create(tenant=self.tenant, statement="two", status=Purpose.Status.PROPOSED)
        resp = self.client.get("/api/v1/journal/purposes/?status=confirmed")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), 1)

    def test_patch_confirm_stamps_confirmed_at(self):
        p = Purpose.objects.create(tenant=self.tenant, statement="a purpose", status=Purpose.Status.PROPOSED)
        resp = self.client.patch(f"/api/v1/journal/purposes/{p.id}/", {"status": "confirmed"}, format="json")
        self.assertEqual(resp.status_code, 200)
        p.refresh_from_db()
        self.assertEqual(p.status, Purpose.Status.CONFIRMED)
        self.assertIsNotNone(p.confirmed_at)

    def test_patch_retire_stamps_retired_at(self):
        p = Purpose.objects.create(tenant=self.tenant, statement="a purpose", status=Purpose.Status.CONFIRMED)
        resp = self.client.patch(f"/api/v1/journal/purposes/{p.id}/", {"status": "retired"}, format="json")
        self.assertEqual(resp.status_code, 200)
        p.refresh_from_db()
        self.assertEqual(p.status, Purpose.Status.RETIRED)
        self.assertIsNotNone(p.retired_at)

    def test_tenant_isolation(self):
        other_user = User.objects.create_user(username="pother", password="x")
        other_tenant = Tenant.objects.create(user=other_user, status="active")
        p = Purpose.objects.create(tenant=other_tenant, statement="theirs", status=Purpose.Status.CONFIRMED)
        resp = self.client.get(f"/api/v1/journal/purposes/{p.id}/")
        self.assertEqual(resp.status_code, 404)

    def test_requires_auth(self):
        anon = APIClient()
        resp = anon.get("/api/v1/journal/purposes/")
        self.assertIn(resp.status_code, (401, 403))


# ── Runtime (internal-auth) endpoints + consent gate ───────────────────────


@override_settings(NBHD_INTERNAL_API_KEY="test-internal-key")
class PurposeRuntimeTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="PurposeRuntime", telegram_chat_id=903001)
        seed_internal_key(self.tenant)
        self.other = create_tenant(display_name="PurposeRuntimeOther", telegram_chat_id=903002)
        seed_internal_key(self.other)
        self.client = APIClient()

    def _headers(self, tid=None):
        tid = tid or str(self.tenant.id)
        return {"HTTP_X_NBHD_INTERNAL_KEY": "test-internal-key", "HTTP_X_NBHD_TENANT_ID": tid}

    def _base(self, tid=None):
        return f"/api/v1/journal/runtime/{tid or self.tenant.id}/purposes"

    def test_missing_key_401(self):
        resp = self.client.get(f"{self._base()}/")
        self.assertEqual(resp.status_code, 401)

    def test_propose_creates_proposed_assistant_origin(self):
        resp = self.client.post(
            f"{self._base()}/propose/",
            {"statement": "Build a life around meaningful work", "pillars": ["gravity", "core"]},
            format="json",
            **self._headers(),
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        purpose = resp.json()["purpose"]
        self.assertEqual(purpose["status"], "proposed")
        self.assertEqual(purpose["origin"], "assistant_proposed")

    def test_confirm_requires_user_confirmed_flag(self):
        p = Purpose.objects.create(
            tenant=self.tenant,
            statement="X",
            status=Purpose.Status.PROPOSED,
            origin=Purpose.Origin.ASSISTANT_PROPOSED,
        )
        # No flag → 403
        resp = self.client.post(f"{self._base()}/{p.id}/confirm/", {}, format="json", **self._headers())
        self.assertEqual(resp.status_code, 403)
        p.refresh_from_db()
        self.assertEqual(p.status, Purpose.Status.PROPOSED)

        # Wrong-typed flag → 403
        resp = self.client.post(
            f"{self._base()}/{p.id}/confirm/", {"user_confirmed": "yes"}, format="json", **self._headers()
        )
        self.assertEqual(resp.status_code, 403)

        # Correct flag → 200 + confirmed
        resp = self.client.post(
            f"{self._base()}/{p.id}/confirm/", {"user_confirmed": True}, format="json", **self._headers()
        )
        self.assertEqual(resp.status_code, 200)
        p.refresh_from_db()
        self.assertEqual(p.status, Purpose.Status.CONFIRMED)
        self.assertIsNotNone(p.confirmed_at)

    def test_update_cannot_promote_proposed_to_confirmed(self):
        p = Purpose.objects.create(tenant=self.tenant, statement="X", status=Purpose.Status.PROPOSED)
        resp = self.client.patch(f"{self._base()}/{p.id}/", {"status": "confirmed"}, format="json", **self._headers())
        self.assertEqual(resp.status_code, 403)
        p.refresh_from_db()
        self.assertEqual(p.status, Purpose.Status.PROPOSED)

    def test_update_can_edit_statement_and_evolve(self):
        p = Purpose.objects.create(tenant=self.tenant, statement="old", status=Purpose.Status.CONFIRMED)
        resp = self.client.patch(
            f"{self._base()}/{p.id}/",
            {"statement": "new direction", "status": "evolving"},
            format="json",
            **self._headers(),
        )
        self.assertEqual(resp.status_code, 200)
        p.refresh_from_db()
        self.assertEqual(p.statement, "new direction")
        self.assertEqual(p.status, Purpose.Status.EVOLVING)

    def test_retire(self):
        p = Purpose.objects.create(tenant=self.tenant, statement="X", status=Purpose.Status.CONFIRMED)
        resp = self.client.post(f"{self._base()}/{p.id}/retire/", {}, format="json", **self._headers())
        self.assertEqual(resp.status_code, 200)
        p.refresh_from_db()
        self.assertEqual(p.status, Purpose.Status.RETIRED)
        self.assertIsNotNone(p.retired_at)

    def test_link_goal(self):
        p = Purpose.objects.create(tenant=self.tenant, statement="X", status=Purpose.Status.CONFIRMED)
        g = Goal.objects.create(tenant=self.tenant, title="A goal")
        resp = self.client.post(
            f"{self._base()}/{p.id}/link-goal/", {"goal_id": str(g.id)}, format="json", **self._headers()
        )
        self.assertEqual(resp.status_code, 200)
        g.refresh_from_db()
        self.assertEqual(g.purpose_id, p.id)

    def test_link_goal_cross_tenant_404(self):
        p = Purpose.objects.create(tenant=self.tenant, statement="X", status=Purpose.Status.CONFIRMED)
        other_goal = Goal.objects.create(tenant=self.other, title="Theirs")
        resp = self.client.post(
            f"{self._base()}/{p.id}/link-goal/", {"goal_id": str(other_goal.id)}, format="json", **self._headers()
        )
        self.assertEqual(resp.status_code, 404)

    def test_link_goal_missing_id_400(self):
        p = Purpose.objects.create(tenant=self.tenant, statement="X", status=Purpose.Status.CONFIRMED)
        resp = self.client.post(f"{self._base()}/{p.id}/link-goal/", {}, format="json", **self._headers())
        self.assertEqual(resp.status_code, 400)

    def test_list_returns_purposes(self):
        Purpose.objects.create(tenant=self.tenant, statement="one", status=Purpose.Status.CONFIRMED)
        resp = self.client.get(f"{self._base()}/", **self._headers())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["count"], 1)


# ── Envelope section ───────────────────────────────────────────────────────


class PurposeEnvelopeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="penv", password="x")
        self.tenant = Tenant.objects.create(user=self.user, status="active")

    def _render(self):
        from apps.journal.envelope import render_north_star

        return render_north_star(self.tenant)

    def test_absent_when_no_confirmed(self):
        # A proposed purpose must NOT surface.
        Purpose.objects.create(tenant=self.tenant, statement="proposed one", status=Purpose.Status.PROPOSED)
        self.assertEqual(self._render(), "")

    def test_confirmed_renders_arrow_and_pillars(self):
        Purpose.objects.create(
            tenant=self.tenant,
            statement="Build a life around my family",
            pillars=["gravity", "core"],
            status=Purpose.Status.CONFIRMED,
        )
        out = self._render()
        self.assertIn("→ Build a life around my family", out)
        self.assertIn("[gravity, core]", out)

    def test_evolving_marked(self):
        Purpose.objects.create(tenant=self.tenant, statement="Shifting direction", status=Purpose.Status.EVOLVING)
        out = self._render()
        self.assertIn("→ Shifting direction", out)
        self.assertIn("evolving", out)

    def test_char_cap_truncates(self):
        for i in range(30):
            Purpose.objects.create(
                tenant=self.tenant,
                statement=f"A fairly long north-star statement number {i} that eats characters",
                status=Purpose.Status.CONFIRMED,
            )
        out = self._render()
        self.assertLessEqual(len(out), 700)  # ~600 cap + the "+N more" line
        self.assertIn("more — see Horizons", out)

    def test_section_appears_in_managed_region(self):
        from apps.orchestrator.workspace_envelope import render_managed_region

        Purpose.objects.create(tenant=self.tenant, statement="Confirmed direction", status=Purpose.Status.CONFIRMED)
        region = render_managed_region(self.tenant)
        self.assertIn("## North Star", region)
        self.assertIn("→ Confirmed direction", region)

    def test_no_heading_when_empty(self):
        from apps.orchestrator.workspace_envelope import render_managed_region

        region = render_managed_region(self.tenant)
        self.assertNotIn("## North Star", region)


# ── Reconcile branch ───────────────────────────────────────────────────────


@override_settings(NBHD_INTERNAL_API_KEY="test-internal-key")
class PurposeReconcileTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="PurposeReconcile", telegram_chat_id=903010)
        seed_internal_key(self.tenant)
        self.client = APIClient()

    def _scan(self, claim):
        return self.client.get(
            f"/api/v1/integrations/runtime/{self.tenant.id}/reconcile/scan/",
            {"claim": claim},
            HTTP_X_NBHD_INTERNAL_KEY="test-internal-key",
            HTTP_X_NBHD_TENANT_ID=str(self.tenant.id),
        )

    def test_direction_keyword_surfaces_confirmed_purpose(self):
        Purpose.objects.create(
            tenant=self.tenant,
            statement="Build a life around my family and health",
            status=Purpose.Status.CONFIRMED,
        )
        resp = self._scan("I'm thinking of quitting my job")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["triggered"]["direction"])
        kinds = {c["kind"] for c in body["candidates"]}
        self.assertIn("purpose", kinds)

    def test_proposed_purpose_not_surfaced(self):
        Purpose.objects.create(tenant=self.tenant, statement="A tentative direction", status=Purpose.Status.PROPOSED)
        resp = self._scan("thinking about my career path")
        self.assertEqual(resp.status_code, 200)
        kinds = {c["kind"] for c in resp.json()["candidates"]}
        self.assertNotIn("purpose", kinds)

    def test_non_direction_claim_no_purpose_without_token_overlap(self):
        Purpose.objects.create(tenant=self.tenant, statement="Serve my community", status=Purpose.Status.CONFIRMED)
        resp = self._scan("I ate a sandwich for lunch")
        self.assertEqual(resp.status_code, 200)
        kinds = {c["kind"] for c in resp.json()["candidates"]}
        self.assertNotIn("purpose", kinds)


# ── journal-context payload ────────────────────────────────────────────────


@override_settings(NBHD_INTERNAL_API_KEY="test-internal-key")
class PurposeJournalContextTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="PurposeCtx", telegram_chat_id=903020)
        seed_internal_key(self.tenant)
        self.client = APIClient()

    def test_confirmed_purpose_in_context(self):
        Purpose.objects.create(tenant=self.tenant, statement="North direction", status=Purpose.Status.CONFIRMED)
        Purpose.objects.create(tenant=self.tenant, statement="Unconfirmed", status=Purpose.Status.PROPOSED)
        resp = self.client.get(
            f"/api/v1/integrations/runtime/{self.tenant.id}/journal-context/",
            HTTP_X_NBHD_INTERNAL_KEY="test-internal-key",
            HTTP_X_NBHD_TENANT_ID=str(self.tenant.id),
        )
        self.assertEqual(resp.status_code, 200)
        north = resp.json().get("north_star", [])
        statements = {p["statement"] for p in north}
        self.assertIn("North direction", statements)
        self.assertNotIn("Unconfirmed", statements)


# ── Nightly extraction purpose-hypothesis card ─────────────────────────────


def _make_extraction_tenant(chat_id=903030) -> Tenant:
    user = User.objects.create_user(username=f"pex{chat_id}", password="x")
    user.timezone = "UTC"
    user.telegram_chat_id = chat_id
    user.save(update_fields=["timezone", "telegram_chat_id"])
    return Tenant.objects.create(user=user, status=Tenant.Status.ACTIVE)


_NOTE = (
    "# Daily Note\n\nA reflective entry long enough to clear the minimum length "
    "threshold for extraction. Worked, trained, and thought about family today, "
    "and how it all connects to where I want my life to go over the next decade."
)


class PurposeExtractionTests(TestCase):
    def setUp(self):
        self.tenant = _make_extraction_tenant()
        Document.objects.create(
            tenant=self.tenant, kind=Document.Kind.DAILY, slug=str(date.today()), title="Today", markdown=_NOTE
        )

    def _run(self, response):
        with (
            patch(
                "apps.journal.extraction._call_extraction_llm",
                return_value=(response, {"prompt_tokens": 10, "completion_tokens": 5}),
            ),
            patch("apps.journal.extraction._deliver_summary_telegram"),
            patch("django.conf.settings.TELEGRAM_BOT_TOKEN", "test-token", create=True),
        ):
            return run_extraction_for_tenant(self.tenant)

    def test_cross_pillar_hypothesis_creates_pending_card(self):
        result = self._run(
            {
                "lessons": [],
                "goals": [],
                "tasks": [],
                "purpose_hypotheses": [
                    {
                        "statement": "Build a life where work funds real time with family",
                        "pillars": ["gravity", "core"],
                        "evidence": "work + family threads recur",
                        "confidence": "medium",
                    }
                ],
            }
        )
        self.assertEqual(result["purpose_hypotheses"], 1)
        card = PendingExtraction.objects.get(tenant=self.tenant, kind=PendingExtraction.Kind.PURPOSE)
        self.assertEqual(card.status, PendingExtraction.Status.PENDING)
        self.assertEqual(card.tags, ["gravity", "core"])
        # No Purpose row yet — it awaits user approval.
        self.assertFalse(Purpose.objects.filter(tenant=self.tenant).exists())

    def test_single_pillar_hypothesis_skipped(self):
        result = self._run(
            {
                "lessons": [],
                "goals": [],
                "tasks": [],
                "purpose_hypotheses": [
                    {"statement": "Get really strong at the gym", "pillars": ["fuel"], "confidence": "high"}
                ],
            }
        )
        self.assertEqual(result["purpose_hypotheses"], 0)
        self.assertFalse(
            PendingExtraction.objects.filter(tenant=self.tenant, kind=PendingExtraction.Kind.PURPOSE).exists()
        )

    def test_sparse_guard_blocks_second_card(self):
        PendingExtraction.objects.create(
            tenant=self.tenant,
            kind=PendingExtraction.Kind.PURPOSE,
            text="Existing hypothesis pending",
            status=PendingExtraction.Status.PENDING,
            expires_at=timezone.now() + timedelta(days=7),
            source_date=date.today(),
        )
        result = self._run(
            {
                "lessons": [],
                "goals": [],
                "tasks": [],
                "purpose_hypotheses": [
                    {
                        "statement": "Another cross pillar direction entirely",
                        "pillars": ["gravity", "fuel"],
                        "confidence": "medium",
                    }
                ],
            }
        )
        self.assertEqual(result["purpose_hypotheses"], 0)
        self.assertEqual(
            PendingExtraction.objects.filter(tenant=self.tenant, kind=PendingExtraction.Kind.PURPOSE).count(), 1
        )

    def test_approval_creates_confirmed_purpose(self):
        from apps.router.extraction_callbacks import _approve_purpose

        card = PendingExtraction.objects.create(
            tenant=self.tenant,
            kind=PendingExtraction.Kind.PURPOSE,
            text="Serve others through my work",
            tags=["gravity", "lessons"],
            status=PendingExtraction.Status.PENDING,
            expires_at=timezone.now() + timedelta(days=7),
            source_date=date.today(),
        )
        _approve_purpose(card)
        p = Purpose.objects.get(tenant=self.tenant)
        self.assertEqual(p.status, Purpose.Status.CONFIRMED)
        self.assertEqual(p.origin, Purpose.Origin.ASSISTANT_PROPOSED)
        self.assertEqual(p.pillars, ["gravity", "lessons"])
        self.assertIsNotNone(p.confirmed_at)
        self.assertTrue(p.evidence)
