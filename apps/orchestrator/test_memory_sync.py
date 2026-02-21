"""Tests for memory_sync module."""
from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.journal.models import Document
from apps.orchestrator.memory_sync import render_memory_files
from apps.tenants.services import create_tenant


class RenderMemoryFilesTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Sync", telegram_chat_id=808080)

    def test_empty_when_no_documents(self):
        # Tenant creation seeds starter docs; clear them to test empty state.
        Document.objects.filter(tenant=self.tenant).delete()
        files = render_memory_files(self.tenant)
        self.assertEqual(files, {})

    def test_renders_non_daily_documents(self):
        # Clear seeded docs to test with controlled data only.
        Document.objects.filter(tenant=self.tenant).delete()
        Document.objects.create(
            tenant=self.tenant,
            kind="memory",
            slug="long-term",
            title="Long-Term Memory",
            markdown="Important stuff",
        )
        Document.objects.create(
            tenant=self.tenant,
            kind="goal",
            slug="fitness",
            title="Fitness Goal",
            markdown="Run a marathon",
        )

        files = render_memory_files(self.tenant)

        self.assertEqual(len(files), 2)
        self.assertIn("memory/journal/memory/long-term.md", files)
        self.assertIn("memory/journal/goal/fitness.md", files)
        self.assertIn("# Long-Term Memory", files["memory/journal/memory/long-term.md"])
        self.assertIn("Important stuff", files["memory/journal/memory/long-term.md"])

    def test_includes_recent_dailies_excludes_old(self):
        today = timezone.now().date()
        old_date = today - timedelta(days=60)

        Document.objects.create(
            tenant=self.tenant,
            kind="daily",
            slug=str(today),
            title=f"Daily {today}",
            markdown="Today's note",
        )
        Document.objects.create(
            tenant=self.tenant,
            kind="daily",
            slug=str(old_date),
            title=f"Daily {old_date}",
            markdown="Old note",
        )

        files = render_memory_files(self.tenant)

        self.assertIn(f"memory/journal/daily/{today}.md", files)
        self.assertNotIn(f"memory/journal/daily/{old_date}.md", files)

    def test_excludes_other_tenants(self):
        # Clear seeded docs to test isolation with controlled data only.
        Document.objects.filter(tenant=self.tenant).delete()
        other = create_tenant(display_name="Other", telegram_chat_id=909090)
        Document.objects.filter(tenant=other).delete()
        Document.objects.create(
            tenant=other,
            kind="memory",
            slug="secret",
            title="Secret",
            markdown="Not yours",
        )
        Document.objects.create(
            tenant=self.tenant,
            kind="memory",
            slug="mine",
            title="Mine",
            markdown="My stuff",
        )

        files = render_memory_files(self.tenant)

        self.assertEqual(len(files), 1)
        self.assertIn("memory/journal/memory/mine.md", files)
