"""Tests for the ``fix_insight_pillars`` management command."""

from __future__ import annotations

from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from apps.insights.models import AssistantInsight, TopicRegistry
from apps.insights.pillars import Pillar
from apps.tenants.services import create_tenant


@override_settings(NBHD_DISABLE_BACKGROUND_THREADS=True)
class FixInsightPillarsCommandTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="FIP", telegram_chat_id=903000)
        self.debt, _ = TopicRegistry.objects.get_or_create(
            pillar=Pillar.GRAVITY.value,
            slug="debt",
            defaults={"display_name": "Debt", "status": TopicRegistry.Status.CANONICAL},
        )
        self.insight = AssistantInsight.objects.create(
            tenant=self.tenant,
            pillar=Pillar.GRAVITY.value,
            topic=self.debt,
            statement="misfiled under gravity but really about the journal",
        )

    def test_list_mode_runs(self):
        out = StringIO()
        call_command("fix_insight_pillars", stdout=out)
        text = out.getvalue()
        self.assertIn(str(self.insight.id), text)
        self.assertIn("[gravity/debt]", text)

    def test_reassign_changes_pillar_and_reresolves_topic(self):
        out = StringIO()
        call_command("fix_insight_pillars", f"{self.insight.id}=journal", stdout=out)
        self.insight.refresh_from_db()
        self.assertEqual(self.insight.pillar, Pillar.JOURNAL.value)
        # Topic re-resolved under the new pillar (same slug, proposed there).
        self.assertEqual(self.insight.topic.pillar, Pillar.JOURNAL.value)
        self.assertEqual(self.insight.topic.slug, "debt")
        # The original gravity/debt topic is untouched.
        self.assertTrue(TopicRegistry.objects.filter(pillar=Pillar.GRAVITY.value, slug="debt").exists())

    def test_reassign_to_same_pillar_is_noop(self):
        out = StringIO()
        call_command("fix_insight_pillars", f"{self.insight.id}=gravity", stdout=out)
        self.insight.refresh_from_db()
        self.assertEqual(self.insight.pillar, Pillar.GRAVITY.value)
        self.assertIn("already gravity", out.getvalue())

    def test_reassign_rejects_unknown_pillar(self):
        with self.assertRaises(CommandError):
            call_command("fix_insight_pillars", f"{self.insight.id}=notapillar")

    def test_reassign_rejects_bad_syntax(self):
        with self.assertRaises(CommandError):
            call_command("fix_insight_pillars", "no-equals-sign")
