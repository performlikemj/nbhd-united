"""Dashboard view tests — Horizons Weekly Pulse helpers + HorizonsView Phase 2 extension."""

from __future__ import annotations

from datetime import date

from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.dashboard.views import _clean_markdown_preview, _derive_week_bounds
from apps.insights.models import AssistantInsight, TopicRegistry
from apps.insights.pillars import Pillar
from apps.tenants.services import create_tenant


class CleanMarkdownPreviewTests(SimpleTestCase):
    def test_strips_headings_bold_and_lists(self):
        md = (
            "# Weekly Review — 2026-W14\n"
            "*Week of April 6–12, 2026*\n\n"
            "## 🏆 Wins\n"
            "- Shipped **billing** fix\n"
            "- Landed *reflection* gate\n"
        )
        out = _clean_markdown_preview(md)
        self.assertNotIn("#", out)
        self.assertNotIn("**", out)
        self.assertNotIn("- ", out)
        self.assertIn("Shipped billing fix", out)
        self.assertIn("Landed reflection gate", out)

    def test_drops_links_keeps_visible_text(self):
        md = "See [the doc](https://example.com/thing) for details."
        self.assertEqual(
            _clean_markdown_preview(md),
            "See the doc for details.",
        )

    def test_strips_inline_code_and_blockquotes(self):
        md = "> quoted line\n`inline` snippet here"
        out = _clean_markdown_preview(md)
        self.assertEqual(out, "quoted line inline snippet here")

    def test_empty_input_returns_empty(self):
        self.assertEqual(_clean_markdown_preview(""), "")
        self.assertEqual(_clean_markdown_preview(None), "")  # type: ignore[arg-type]

    def test_truncates_with_ellipsis(self):
        md = "a " * 200
        out = _clean_markdown_preview(md, max_chars=50)
        self.assertLessEqual(len(out), 50)
        self.assertTrue(out.endswith("\u2026"))

    def test_preserves_underscore_in_identifiers(self):
        # A bare `some_var` should not be treated as italic
        md = "Look at some_var and another_name in the logs."
        out = _clean_markdown_preview(md)
        self.assertIn("some_var", out)
        self.assertIn("another_name", out)


class DeriveWeekBoundsTests(SimpleTestCase):
    def test_parses_iso_monday_slug(self):
        start, end = _derive_week_bounds("2026-04-06", date(2026, 4, 13))
        self.assertEqual(start, date(2026, 4, 6))
        self.assertEqual(end, date(2026, 4, 12))

    def test_falls_back_to_week_of_fallback(self):
        # Wednesday 2026-04-15 → Monday is 2026-04-13
        start, end = _derive_week_bounds("not-a-date", date(2026, 4, 15))
        self.assertEqual(start, date(2026, 4, 13))
        self.assertEqual(end, date(2026, 4, 19))

    def test_invalid_month_falls_back(self):
        start, _ = _derive_week_bounds("2026-13-01", date(2026, 4, 20))
        self.assertEqual(start, date(2026, 4, 20))  # Monday


class HorizonsViewAssistantInsightsTests(TestCase):
    """Phase 2 extension: assistant_insights surfaced in /api/v1/dashboard/horizons/."""

    def setUp(self):
        self.tenant = create_tenant(display_name="Horizons-P2", telegram_chat_id=900700)
        self.other_tenant = create_tenant(display_name="Horizons-Other", telegram_chat_id=900701)
        self.client = APIClient()
        token = RefreshToken.for_user(self.tenant.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")
        self.topic = TopicRegistry.objects.create(
            pillar=Pillar.GRAVITY.value,
            slug="debt",
            display_name="Debt",
            status=TopicRegistry.Status.CANONICAL,
            source=TopicRegistry.Source.SEED,
        )

    def _make(self, *, tenant=None, statement="An observation.", status=AssistantInsight.Status.OPEN):
        return AssistantInsight.objects.create(
            tenant=tenant or self.tenant,
            pillar=Pillar.GRAVITY.value,
            topic=self.topic,
            statement=statement,
            status=status,
        )

    def test_horizons_includes_assistant_insights_field(self):
        resp = self.client.get("/api/v1/dashboard/horizons/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("assistant_insights", resp.json())
        self.assertEqual(resp.json()["assistant_insights"], [])

    def test_open_and_confirmed_surface_refuted_hidden(self):
        self._make(statement="Open one")
        self._make(statement="Confirmed one", status=AssistantInsight.Status.CONFIRMED)
        self._make(statement="Refuted one", status=AssistantInsight.Status.REFUTED)
        resp = self.client.get("/api/v1/dashboard/horizons/")
        statements = {i["statement"] for i in resp.json()["assistant_insights"]}
        self.assertIn("Open one", statements)
        self.assertIn("Confirmed one", statements)
        self.assertNotIn("Refuted one", statements)

    def test_ordered_newest_first(self):
        first = self._make(statement="Older")
        second = self._make(statement="Newer")
        # Force-stamp first one to be older
        AssistantInsight.objects.filter(id=first.id).update(created_at=second.created_at.replace(year=2025))
        resp = self.client.get("/api/v1/dashboard/horizons/")
        statements = [i["statement"] for i in resp.json()["assistant_insights"]]
        self.assertEqual(statements[0], "Newer")
        self.assertEqual(statements[1], "Older")

    def test_capped_at_twenty(self):
        for i in range(25):
            self._make(statement=f"Insight {i}")
        resp = self.client.get("/api/v1/dashboard/horizons/")
        self.assertLessEqual(len(resp.json()["assistant_insights"]), 20)

    def test_isolates_other_tenant_insights(self):
        self._make(tenant=self.other_tenant, statement="Other tenant private")
        resp = self.client.get("/api/v1/dashboard/horizons/")
        statements = {i["statement"] for i in resp.json()["assistant_insights"]}
        self.assertNotIn("Other tenant private", statements)

    def test_payload_shape(self):
        ins = self._make(statement="Shape check", status=AssistantInsight.Status.CONFIRMED)
        resp = self.client.get("/api/v1/dashboard/horizons/")
        row = next(i for i in resp.json()["assistant_insights"] if i["id"] == str(ins.id))
        self.assertEqual(row["pillar"], Pillar.GRAVITY.value)
        self.assertEqual(row["topic_slug"], "debt")
        self.assertEqual(row["topic_display_name"], "Debt")
        self.assertEqual(row["status"], "confirmed")
        self.assertIn("confidence", row)
        self.assertIn("created_at", row)
