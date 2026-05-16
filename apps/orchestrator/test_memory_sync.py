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

    def test_skips_docs_with_ntfs_hostile_path_components(self):
        """Defense-in-depth: even if a doc with a path-hostile kind/slug
        somehow lands in the DB (direct write, pre-validation legacy row),
        render_memory_files must skip it rather than build an NTFS-hostile
        path that grinds the SMB sync.

        Regression: a ``kind=':' slug=':'`` row on the canary tenant produced
        ``memory/journal/:/:.md`` and made ~6 failed SMB roundtrips per
        sync invocation. See migration 0017.
        """
        Document.objects.filter(tenant=self.tenant).delete()
        # Bypass field-choice validation via direct bulk_create (the model
        # has `choices=` but Django doesn't enforce it at the DB layer).
        Document.objects.bulk_create(
            [
                Document(
                    tenant=self.tenant,
                    kind=":",
                    slug=":",
                    title="garbage",
                    markdown="",
                ),
                Document(
                    tenant=self.tenant,
                    kind="cron",
                    slug="_sync:Heartbeat Check-in",
                    title="misrouted sync",
                    markdown="content",
                ),
                Document(
                    tenant=self.tenant,
                    kind="memory",
                    slug="valid-slug",
                    title="ok",
                    markdown="kept",
                ),
            ]
        )

        files = render_memory_files(self.tenant)

        # Only the valid row produces a file.
        self.assertEqual(len(files), 1)
        self.assertIn("memory/journal/memory/valid-slug.md", files)
        # Hostile paths are NOT in the output.
        self.assertNotIn("memory/journal/:/:.md", files)
        self.assertNotIn(
            "memory/journal/cron/_sync:Heartbeat Check-in.md",
            files,
        )
