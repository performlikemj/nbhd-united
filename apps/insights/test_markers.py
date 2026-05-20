"""Tests for the insight-marker extractor.

Covers extraction semantics (single, multi, multi-line statements),
topic resolution (canonical, alias, novel → proposed), edge cases
(malformed markers, empty statements, oversized statements), and the
"strip marker tokens, keep statement" contract.
"""

from __future__ import annotations

from django.test import TestCase, override_settings

from apps.insights.markers import extract_and_record_insights
from apps.insights.models import AssistantInsight, TopicAlias, TopicRegistry
from apps.insights.pillars import Pillar
from apps.tenants.services import create_tenant


@override_settings(NBHD_DISABLE_BACKGROUND_THREADS=True)
class ExtractAndRecordInsightsTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Markers", telegram_chat_id=900900)
        # Seed migration 0002 already creates the canonical Gravity topics;
        # use get_or_create as a defensive primer for any topic the tests
        # touch directly so the assertions don't depend on seed ordering.
        self.debt, _ = TopicRegistry.objects.get_or_create(
            pillar=Pillar.GRAVITY.value,
            slug="debt",
            defaults={
                "display_name": "Debt",
                "status": TopicRegistry.Status.CANONICAL,
                "source": TopicRegistry.Source.SEED,
            },
        )
        self.dining, _ = TopicRegistry.objects.get_or_create(
            pillar=Pillar.GRAVITY.value,
            slug="dining",
            defaults={
                "display_name": "Dining",
                "status": TopicRegistry.Status.CANONICAL,
                "source": TopicRegistry.Source.SEED,
            },
        )

    def _insights(self):
        return list(
            AssistantInsight.objects.filter(tenant=self.tenant)
            .order_by("created_at")
            .values("pillar", "topic_id", "statement", "status")
        )

    # --- happy path -----------------------------------------------------

    def test_single_marker_writes_row_and_strips_tokens(self):
        text = "Looking at your trajectory, [[insight:debt]]you stay in debt for decades[[/insight]] — fixable."
        out = extract_and_record_insights(text, tenant=self.tenant)
        self.assertEqual(out, "Looking at your trajectory, you stay in debt for decades — fixable.")
        rows = self._insights()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["topic_id"], self.debt.id)
        self.assertEqual(rows[0]["statement"], "you stay in debt for decades")
        self.assertEqual(rows[0]["status"], "open")
        self.assertEqual(rows[0]["pillar"], "gravity")

    def test_multiple_markers_in_one_reply(self):
        text = (
            "[[insight:debt]]carrying 8 lines, 20+ year payoff[[/insight]] and "
            "[[insight:dining]]dining ran 1.8x baseline[[/insight]]."
        )
        out = extract_and_record_insights(text, tenant=self.tenant)
        self.assertEqual(out, "carrying 8 lines, 20+ year payoff and dining ran 1.8x baseline.")
        rows = self._insights()
        self.assertEqual(len(rows), 2)
        topic_ids = {r["topic_id"] for r in rows}
        self.assertEqual(topic_ids, {self.debt.id, self.dining.id})

    def test_multi_line_statement_extracted(self):
        text = (
            "Pattern: [[insight:debt]]you've been adding to balances\n"
            "across three months while telling yourself otherwise[[/insight]]"
        )
        out = extract_and_record_insights(text, tenant=self.tenant)
        self.assertIn("adding to balances\nacross three months", out)
        self.assertNotIn("[[insight:", out)
        self.assertEqual(len(self._insights()), 1)

    # --- topic resolution ----------------------------------------------

    def test_alias_resolves_to_canonical_topic(self):
        TopicAlias.objects.get_or_create(
            topic=self.dining,
            alias="eating out",
            defaults={"source": TopicAlias.Source.SEED},
        )
        text = "[[insight:eating out]]you order Friday night every week[[/insight]]"
        extract_and_record_insights(text, tenant=self.tenant)
        rows = self._insights()
        self.assertEqual(rows[0]["topic_id"], self.dining.id)

    def test_unknown_slug_creates_proposed_topic(self):
        text = "[[insight:vintage_wine_hunting]]you've been bidding on Friday auctions[[/insight]]"
        extract_and_record_insights(text, tenant=self.tenant)
        rows = self._insights()
        self.assertEqual(len(rows), 1)
        new_topic = TopicRegistry.objects.get(id=rows[0]["topic_id"])
        self.assertEqual(new_topic.status, TopicRegistry.Status.PROPOSED)
        self.assertEqual(new_topic.slug, "vintage_wine_hunting")

    # --- edge cases ----------------------------------------------------

    def test_no_marker_returns_unchanged_text(self):
        text = "Just a regular reply with no markup."
        out = extract_and_record_insights(text, tenant=self.tenant)
        self.assertEqual(out, text)
        self.assertEqual(self._insights(), [])

    def test_empty_text_short_circuits(self):
        self.assertEqual(extract_and_record_insights("", tenant=self.tenant), "")
        self.assertEqual(self._insights(), [])

    def test_malformed_marker_unclosed_does_not_record(self):
        text = "[[insight:debt]]but where's the closing tag — just text"
        out = extract_and_record_insights(text, tenant=self.tenant)
        # Unclosed marker is left literal (no regex match → no substitution).
        self.assertEqual(out, text)
        self.assertEqual(self._insights(), [])

    def test_empty_statement_is_silently_dropped(self):
        text = "leading [[insight:debt]][[/insight]] trailing"
        out = extract_and_record_insights(text, tenant=self.tenant)
        self.assertEqual(out, "leading  trailing")
        self.assertEqual(self._insights(), [])

    def test_oversized_statement_is_truncated(self):
        big = "x" * 2000
        text = f"[[insight:debt]]{big}[[/insight]]"
        extract_and_record_insights(text, tenant=self.tenant)
        rows = self._insights()
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]["statement"]), 1000)

    # --- tenant isolation ---------------------------------------------

    def test_writes_scoped_to_passed_tenant(self):
        other = create_tenant(display_name="OtherMarkers", telegram_chat_id=900901)
        text = "[[insight:debt]]their pattern[[/insight]]"
        extract_and_record_insights(text, tenant=other)
        # Original tenant has zero insights; other tenant has one.
        self.assertEqual(AssistantInsight.objects.filter(tenant=self.tenant).count(), 0)
        self.assertEqual(AssistantInsight.objects.filter(tenant=other).count(), 1)
