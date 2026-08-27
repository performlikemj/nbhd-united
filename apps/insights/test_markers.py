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
        # These tests exercise the Gravity write path; gravity insights only
        # persist for a finance-active tenant (test settings keep
        # GRAVITY_ENABLED=True, so flipping the flag is enough). The refusal
        # path for non-finance tenants is covered by GravityFinanceGateTests.
        self.tenant.finance_enabled = True
        self.tenant.save(update_fields=["finance_enabled"])
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
        out = extract_and_record_insights(text, tenant=self.tenant, pillar=Pillar.GRAVITY.value)
        self.assertEqual(out, "Looking at your trajectory, you stay in debt for decades — fixable.")
        rows = self._insights()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["topic_id"], self.debt.id)
        self.assertEqual(rows[0]["statement"], "you stay in debt for decades")
        self.assertEqual(rows[0]["status"], "open")
        self.assertEqual(rows[0]["pillar"], "gravity")

    def test_rehydrated_delivery_text_is_redacted_again_for_storage_only(self):
        self.tenant.pii_entity_map = {"[PERSON_1]": {"name": "Theo Smith"}}
        self.tenant.save(update_fields=["pii_entity_map"])
        text = "[[insight:journal/relationships]]Theo Smith checks in weekly[[/insight]]"

        out = extract_and_record_insights(text, tenant=self.tenant)

        self.assertEqual(out, "Theo Smith checks in weekly")
        insight = AssistantInsight.objects.get(tenant=self.tenant)
        self.assertEqual(insight.statement, "[PERSON_1] checks in weekly")

    def test_multiple_markers_in_one_reply(self):
        text = (
            "[[insight:debt]]carrying 8 lines, 20+ year payoff[[/insight]] and "
            "[[insight:dining]]dining ran 1.8x baseline[[/insight]]."
        )
        out = extract_and_record_insights(text, tenant=self.tenant, pillar=Pillar.GRAVITY.value)
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
        extract_and_record_insights(text, tenant=self.tenant, pillar=Pillar.GRAVITY.value)
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


@override_settings(NBHD_DISABLE_BACKGROUND_THREADS=True)
class MarkerPillarParsingTests(TestCase):
    """The optional ``<pillar>/`` prefix in the marker's topic spec."""

    def setUp(self):
        self.tenant = create_tenant(display_name="PillarMarkers", telegram_chat_id=900910)
        # One test here files a gravity insight, which requires finance_active.
        self.tenant.finance_enabled = True
        self.tenant.save(update_fields=["finance_enabled"])

    def _rows(self):
        return list(AssistantInsight.objects.filter(tenant=self.tenant).select_related("topic").order_by("created_at"))

    def test_no_prefix_defaults_to_journal(self):
        # No marker prefix and no explicit pillar arg → the neutral journal
        # default (NOT gravity — that was the misfiling bug).
        extract_and_record_insights(
            "[[insight:mood]]you write more on rough days[[/insight]]",
            tenant=self.tenant,
        )
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].pillar, Pillar.JOURNAL.value)
        self.assertEqual(rows[0].topic.pillar, Pillar.JOURNAL.value)
        self.assertEqual(rows[0].topic.slug, "mood")

    def test_pillar_prefix_files_under_named_pillar(self):
        extract_and_record_insights(
            "[[insight:fuel/exercise]]you train hardest on Mondays[[/insight]]",
            tenant=self.tenant,
        )
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].pillar, Pillar.FUEL.value)
        self.assertEqual(rows[0].topic.pillar, Pillar.FUEL.value)
        self.assertEqual(rows[0].topic.slug, "exercise")

    def test_prefix_overrides_caller_default(self):
        # A marker's own prefix wins over the caller-supplied pillar default.
        extract_and_record_insights(
            "[[insight:fuel/sleep_quality]]your sleep dips before deadlines[[/insight]]",
            tenant=self.tenant,
            pillar=Pillar.GRAVITY.value,
        )
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].pillar, Pillar.FUEL.value)

    def test_junk_pillar_falls_back_to_default_and_keeps_whole_slug(self):
        # "notapillar" is not canonical → the whole "notapillar/x" becomes the
        # slug under the default pillar (journal). Nothing is misfiled; the
        # topic is auto-proposed for ops.
        extract_and_record_insights(
            "[[insight:notapillar/whatever]]some observation[[/insight]]",
            tenant=self.tenant,
        )
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].pillar, Pillar.JOURNAL.value)
        # resolve_topic slugifies "notapillar/whatever" → "notapillar_whatever"
        self.assertEqual(rows[0].topic.slug, "notapillar_whatever")
        self.assertEqual(rows[0].topic.status, TopicRegistry.Status.PROPOSED)

    def test_explicit_pillar_arg_used_when_no_prefix(self):
        extract_and_record_insights(
            "[[insight:debt]]you carry a balance every month[[/insight]]",
            tenant=self.tenant,
            pillar=Pillar.GRAVITY.value,
        )
        rows = self._rows()
        self.assertEqual(rows[0].pillar, Pillar.GRAVITY.value)


@override_settings(NBHD_DISABLE_BACKGROUND_THREADS=True)
class GravityFinanceGateTests(TestCase):
    """Gravity insight markers are gated on ``Tenant.finance_active``.

    The always-loaded ``AGENTS.md`` teaches the gravity taxonomy to every assistant
    fleet-wide, so a gravity-prefixed marker can surface for a tenant who never
    enabled the finance module. ``finance_active`` is the authoritative kill
    switch for Gravity data — such a marker must be refused (no row written),
    while the statement still stays in the user-facing reply.
    """

    def setUp(self):
        # Default tenant: finance_enabled=False → finance_active=False even with
        # GRAVITY_ENABLED=True in test settings.
        self.tenant = create_tenant(display_name="NoFinance", telegram_chat_id=900920)

    def _rows(self):
        return list(AssistantInsight.objects.filter(tenant=self.tenant).order_by("created_at"))

    def _enable_finance(self):
        self.tenant.finance_enabled = True
        self.tenant.save(update_fields=["finance_enabled"])

    def test_gravity_prefix_refused_for_non_finance_tenant(self):
        text = "You mentioned money — [[insight:gravity/dining]]you eat out when stressed[[/insight]]."
        out = extract_and_record_insights(text, tenant=self.tenant)
        # Statement stays visible; marker tokens stripped.
        self.assertEqual(out, "You mentioned money — you eat out when stressed.")
        # Nothing persisted — the Gravity kill switch held.
        self.assertEqual(self._rows(), [])

    def test_gravity_default_pillar_refused_for_non_finance_tenant(self):
        # Even when the caller default is gravity, a bare marker under a
        # non-finance tenant writes nothing.
        text = "[[insight:debt]]you carry balances for years[[/insight]]"
        out = extract_and_record_insights(text, tenant=self.tenant, pillar=Pillar.GRAVITY.value)
        self.assertEqual(out, "you carry balances for years")
        self.assertEqual(self._rows(), [])

    def test_with_ids_returns_empty_when_gravity_refused(self):
        from apps.insights.markers import extract_and_record_insights_with_ids

        text = "[[insight:gravity/dining]]you eat out when stressed[[/insight]]"
        cleaned, ids = extract_and_record_insights_with_ids(text, tenant=self.tenant)
        self.assertEqual(cleaned, "you eat out when stressed")
        self.assertEqual(ids, [])
        self.assertEqual(self._rows(), [])

    def test_non_gravity_marker_still_records_for_non_finance_tenant(self):
        # The gate is gravity-specific: journal / fuel / etc. still record.
        text = "[[insight:fuel/exercise]]you train hardest on Mondays[[/insight]]"
        extract_and_record_insights(text, tenant=self.tenant)
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].pillar, Pillar.FUEL.value)

    def test_gravity_marker_recorded_for_finance_active_tenant(self):
        self._enable_finance()
        text = "[[insight:gravity/dining]]you eat out when stressed[[/insight]]"
        extract_and_record_insights(text, tenant=self.tenant)
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].pillar, Pillar.GRAVITY.value)

    @override_settings(GRAVITY_ENABLED=False)
    def test_gravity_refused_when_platform_kill_switch_off(self):
        # finance_enabled=True but the platform-wide GRAVITY_ENABLED kill switch
        # is off → finance_active=False → gravity marker still refused.
        self._enable_finance()
        text = "[[insight:gravity/dining]]you eat out when stressed[[/insight]]"
        out = extract_and_record_insights(text, tenant=self.tenant)
        self.assertEqual(out, "you eat out when stressed")
        self.assertEqual(self._rows(), [])
