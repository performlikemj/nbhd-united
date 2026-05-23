"""Tests for Phase 4 weekly reflection synthesis.

Covers:
- Happy path: LLM returns prose + insight marker → AssistantInsight + Document(kind=WEEKLY) written
- Idempotency: re-run in the same ISO week is a no-op (skipped=already_ran)
- Volume gate: UserVoicePref.volume=SILENT skips
- Finance disabled: skips
- LLM returns NO_REFLECTION sentinel: no rows written
- LLM error: skipped=llm_error, nothing written
- record_usage uses is_system=True (no tenant counter update)
- Dispatcher: fires for tenants in Sunday 09:00 local time, skips others
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.billing.models import UsageRecord
from apps.insights.models import AssistantInsight, TopicRegistry, UserVoicePref
from apps.insights.pillars import Pillar
from apps.insights.synthesis import generate_weekly_reflection
from apps.insights.tasks import _is_sunday_morning_local, weekly_gravity_reflection_task
from apps.journal.models import Document
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


def _enable_finance(tenant: Tenant) -> Tenant:
    """Flip finance_enabled True (default tenant ctor doesn't)."""
    Tenant.objects.filter(id=tenant.id).update(finance_enabled=True, status=Tenant.Status.ACTIVE)
    tenant.refresh_from_db()
    return tenant


@override_settings(NBHD_DISABLE_BACKGROUND_THREADS=True, OPENROUTER_API_KEY="test-key")
class GenerateWeeklyReflectionTests(TestCase):
    def setUp(self):
        # 2026-05-21 is a Thursday — ISO week 21 of 2026.
        self.now = datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC)
        self.tenant = _enable_finance(create_tenant(display_name="P4", telegram_chat_id=901000))
        self.debt, _ = TopicRegistry.objects.get_or_create(
            pillar=Pillar.GRAVITY.value,
            slug="debt",
            defaults={
                "display_name": "Debt",
                "status": TopicRegistry.Status.CANONICAL,
                "source": TopicRegistry.Source.SEED,
            },
        )

    def _seed_some_signal(self):
        AssistantInsight.objects.create(
            tenant=self.tenant,
            pillar=Pillar.GRAVITY.value,
            topic=self.debt,
            statement="prior observation about debt",
        )

    @patch("apps.insights.synthesis._call_synthesis_llm")
    def test_happy_path_writes_insight_and_document(self, mock_llm):
        self._seed_some_signal()
        mock_llm.return_value = (
            "This week your debt trajectory held steady. "
            "[[insight:debt]]you're treading water on principal while interest accrues[[/insight]]",
            {"prompt_tokens": 100, "completion_tokens": 50},
        )

        result = generate_weekly_reflection(self.tenant, now=self.now)
        self.assertEqual(result.skipped, "")
        self.assertIsNotNone(result.document_id)
        self.assertIsNotNone(result.insight_id)
        self.assertEqual(result.iso_week, "2026-W21")

        doc = Document.objects.get(id=result.document_id)
        self.assertEqual(doc.kind, Document.Kind.WEEKLY)
        self.assertNotIn("[[insight:", doc.markdown)
        self.assertIn("treading water", doc.markdown)

        ins = AssistantInsight.objects.get(id=result.insight_id)
        self.assertEqual(ins.statement, "you're treading water on principal while interest accrues")
        self.assertEqual(ins.topic_id, self.debt.id)
        self.assertEqual(ins.status, "open")

    @patch("apps.insights.synthesis._call_synthesis_llm")
    def test_records_usage_with_is_system_true(self, mock_llm):
        self._seed_some_signal()
        mock_llm.return_value = (
            "[[insight:debt]]a fresh debt observation[[/insight]]",
            {"prompt_tokens": 80, "completion_tokens": 40},
        )

        before = Tenant.objects.filter(id=self.tenant.id).values_list("estimated_cost_this_month", flat=True).first()
        generate_weekly_reflection(self.tenant, now=self.now)
        after = Tenant.objects.filter(id=self.tenant.id).values_list("estimated_cost_this_month", flat=True).first()
        self.assertEqual(before, after)

        usage = UsageRecord.objects.filter(tenant=self.tenant, event_type="weekly_reflection").first()
        self.assertIsNotNone(usage)
        self.assertTrue(usage.is_system_event)
        self.assertEqual(usage.input_tokens, 80)
        self.assertEqual(usage.output_tokens, 40)

    @patch("apps.insights.synthesis._call_synthesis_llm")
    def test_idempotent_per_iso_week(self, mock_llm):
        self._seed_some_signal()
        mock_llm.return_value = (
            "[[insight:debt]]first run observation[[/insight]]",
            {"prompt_tokens": 50, "completion_tokens": 25},
        )

        first = generate_weekly_reflection(self.tenant, now=self.now)
        self.assertEqual(first.skipped, "")

        second = generate_weekly_reflection(self.tenant, now=self.now)
        self.assertEqual(second.skipped, "already_ran")
        self.assertEqual(mock_llm.call_count, 1)

    def test_finance_disabled_skips(self):
        Tenant.objects.filter(id=self.tenant.id).update(finance_enabled=False)
        self.tenant.refresh_from_db()
        result = generate_weekly_reflection(self.tenant, now=self.now)
        self.assertEqual(result.skipped, "finance_disabled")

    def test_voice_pref_silent_skips(self):
        UserVoicePref.objects.create(
            tenant=self.tenant,
            pillar=Pillar.GRAVITY.value,
            topic=None,
            volume=UserVoicePref.Volume.SILENT,
        )
        self._seed_some_signal()
        result = generate_weekly_reflection(self.tenant, now=self.now)
        self.assertEqual(result.skipped, "volume_silent")

    def test_no_data_skips(self):
        result = generate_weekly_reflection(self.tenant, now=self.now)
        self.assertEqual(result.skipped, "no_data")

    @patch("apps.insights.synthesis._call_synthesis_llm")
    def test_llm_returns_no_reflection_sentinel(self, mock_llm):
        self._seed_some_signal()
        mock_llm.return_value = ("NO_REFLECTION", {"prompt_tokens": 30, "completion_tokens": 5})

        result = generate_weekly_reflection(self.tenant, now=self.now)
        self.assertEqual(result.skipped, "no_reflection")
        self.assertFalse(Document.objects.filter(tenant=self.tenant, kind=Document.Kind.WEEKLY).exists())

    @patch("apps.insights.synthesis._call_synthesis_llm")
    def test_llm_error_is_swallowed(self, mock_llm):
        self._seed_some_signal()
        mock_llm.side_effect = RuntimeError("openrouter down")

        result = generate_weekly_reflection(self.tenant, now=self.now)
        self.assertEqual(result.skipped, "llm_error")
        self.assertFalse(Document.objects.filter(tenant=self.tenant, kind=Document.Kind.WEEKLY).exists())


class IsSundayMorningLocalTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="TZ", telegram_chat_id=901100)

    def _set_tz(self, tz_name: str) -> None:
        user = self.tenant.user
        user.timezone = tz_name
        user.save(update_fields=["timezone"])

    def test_utc_sunday_9am(self):
        self._set_tz("UTC")
        # 2026-05-24 is a Sunday.
        now = datetime(2026, 5, 24, 9, 0, 0, tzinfo=UTC)
        self.assertTrue(_is_sunday_morning_local(self.tenant, now=now))

    def test_utc_sunday_10am_misses(self):
        self._set_tz("UTC")
        now = datetime(2026, 5, 24, 10, 0, 0, tzinfo=UTC)
        self.assertFalse(_is_sunday_morning_local(self.tenant, now=now))

    def test_utc_saturday_9am_misses(self):
        self._set_tz("UTC")
        now = datetime(2026, 5, 23, 9, 0, 0, tzinfo=UTC)
        self.assertFalse(_is_sunday_morning_local(self.tenant, now=now))

    def test_tokyo_user_fires_at_sunday_00z(self):
        # Sunday 09:00 JST = Sunday 00:00 UTC (JST is UTC+9, no DST).
        self._set_tz("Asia/Tokyo")
        now = datetime(2026, 5, 24, 0, 0, 0, tzinfo=UTC)
        self.assertTrue(_is_sunday_morning_local(self.tenant, now=now))

    def test_invalid_tz_falls_back_to_utc(self):
        self._set_tz("Not/A/Zone")
        now = datetime(2026, 5, 24, 9, 0, 0, tzinfo=UTC)
        self.assertTrue(_is_sunday_morning_local(self.tenant, now=now))


@override_settings(NBHD_DISABLE_BACKGROUND_THREADS=True, OPENROUTER_API_KEY="test-key")
class WeeklyGravityReflectionTaskTests(TestCase):
    def setUp(self):
        self.tenant_in = _enable_finance(create_tenant(display_name="In", telegram_chat_id=901200))
        self.tenant_in.user.timezone = "UTC"
        self.tenant_in.user.save(update_fields=["timezone"])

        self.tenant_out = _enable_finance(create_tenant(display_name="Out", telegram_chat_id=901201))
        self.tenant_out.user.timezone = "Asia/Tokyo"
        self.tenant_out.user.save(update_fields=["timezone"])

        TopicRegistry.objects.get_or_create(
            pillar=Pillar.GRAVITY.value,
            slug="debt",
            defaults={
                "display_name": "Debt",
                "status": TopicRegistry.Status.CANONICAL,
                "source": TopicRegistry.Source.SEED,
            },
        )

    @patch("apps.insights.tasks.datetime")
    @patch("apps.insights.synthesis._call_synthesis_llm")
    def test_dispatcher_fires_only_in_local_sunday_9am(self, mock_llm, mock_dt):
        # Sunday 09:00 UTC: in-window tenant fires; Tokyo tenant local is 18:00, misses.
        frozen = datetime(2026, 5, 24, 9, 0, 0, tzinfo=UTC)
        mock_dt.now.return_value = frozen

        debt = TopicRegistry.objects.get(pillar=Pillar.GRAVITY.value, slug="debt")
        AssistantInsight.objects.create(
            tenant=self.tenant_in,
            pillar=Pillar.GRAVITY.value,
            topic=debt,
            statement="prior",
        )
        mock_llm.return_value = (
            "Reflection. [[insight:debt]]new observation[[/insight]]",
            {"prompt_tokens": 80, "completion_tokens": 40},
        )

        counts = weekly_gravity_reflection_task()
        self.assertEqual(counts["fired"], 1)
        self.assertGreaterEqual(counts["skipped_window"], 1)
        self.assertTrue(Document.objects.filter(tenant=self.tenant_in, kind=Document.Kind.WEEKLY).exists())
        self.assertFalse(Document.objects.filter(tenant=self.tenant_out, kind=Document.Kind.WEEKLY).exists())


class RecordUsageIsSystemTests(TestCase):
    """Direct unit coverage for the new is_system kwarg on record_usage."""

    def setUp(self):
        self.tenant = create_tenant(display_name="Bills", telegram_chat_id=901300)

    def test_is_system_writes_row_skips_counter(self):
        from apps.billing.services import record_usage

        before = (
            Tenant.objects.filter(id=self.tenant.id)
            .values_list("estimated_cost_this_month", "tokens_this_month")
            .first()
        )
        record_usage(
            self.tenant,
            event_type="weekly_reflection",
            input_tokens=100,
            output_tokens=50,
            model_used="openrouter/deepseek/deepseek-v4-pro",
            is_system=True,
        )
        after = (
            Tenant.objects.filter(id=self.tenant.id)
            .values_list("estimated_cost_this_month", "tokens_this_month")
            .first()
        )
        self.assertEqual(before, after)

        row = UsageRecord.objects.get(tenant=self.tenant, event_type="weekly_reflection")
        self.assertTrue(row.is_system_event)
        self.assertGreater(row.cost_estimate, Decimal("0"))

    def test_default_is_system_false_updates_counter(self):
        from apps.billing.services import record_usage

        before = Tenant.objects.filter(id=self.tenant.id).values_list("tokens_this_month", flat=True).first()
        record_usage(
            self.tenant,
            event_type="message",
            input_tokens=100,
            output_tokens=50,
            model_used="openrouter/deepseek/deepseek-v4-pro",
        )
        after = Tenant.objects.filter(id=self.tenant.id).values_list("tokens_this_month", flat=True).first()
        self.assertEqual(after, before + 150)
