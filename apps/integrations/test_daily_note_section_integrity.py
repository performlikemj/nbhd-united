"""Regression tests for scoped markdown section writes."""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.journal.models import Document
from apps.tenants.models import Tenant
from apps.tenants.test_utils import seed_internal_key

User = get_user_model()


@override_settings(NBHD_INTERNAL_API_KEY="test-key")
class MarkdownSectionIntegrityTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="section-integrity", password="pass")
        self.tenant = Tenant.objects.create(user=self.user, status=Tenant.Status.ACTIVE)
        seed_internal_key(self.tenant)
        self.client = APIClient()
        self.headers = {
            "HTTP_X_NBHD_INTERNAL_KEY": "test-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }
        self.daily_url = f"/api/v1/integrations/runtime/{self.tenant.id}/daily-note/append/"
        self.memory_url = f"/api/v1/integrations/runtime/{self.tenant.id}/long-term-memory/"

    def _daily_document(self, markdown):
        return Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.DAILY,
            slug="2026-07-27",
            title="2026-07-27",
            markdown=markdown,
        )

    def _replace_daily_report(self, document, content="new report"):
        return self.client.post(
            self.daily_url,
            {
                "content": content,
                "date": document.slug,
                "section_slug": "report",
            },
            format="json",
            **self.headers,
        )

    def test_trailing_daily_section_rewrite_preserves_user_entry_blocks(self):
        markdown = (
            "# 2026-07-27\n\n"
            "## Report\n"
            "old report\n\n"
            "### 09:05 — MJ\n"
            "First journal entry.\n\n"
            "### 18:40 — MJ\n"
            "Second journal entry.\n"
        )
        document = self._daily_document(markdown)
        preserved_tail = markdown[markdown.index("### 09:05") :]

        response = self._replace_daily_report(document)

        self.assertEqual(response.status_code, 201, response.content)
        self.assertNotIn("old report", response.data["markdown"])
        self.assertIn("## Report\nnew report", response.data["markdown"])
        self.assertTrue(response.data["markdown"].endswith(preserved_tail))

    def test_scoped_memory_rewrite_preserves_user_entry_blocks(self):
        markdown = "# Memory\n\n## People & Context\n- old fact\n\n### 09:05 — MJ\nJournal-shaped tail.\n"
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.MEMORY,
            slug="long-term",
            title="Memory",
            markdown=markdown,
        )
        preserved_tail = markdown[markdown.index("### 09:05") :]

        response = self.client.put(
            self.memory_url,
            {
                "markdown": "- new fact",
                "section": "People & Context",
            },
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertNotIn("- old fact", response.data["markdown"])
        self.assertIn("## People & Context\n- new fact", response.data["markdown"])
        self.assertTrue(response.data["markdown"].endswith(preserved_tail))

    def test_user_entry_before_next_section_is_the_replace_boundary(self):
        markdown = (
            "# 2026-07-27\n\n"
            "## Report\n"
            "old report\n\n"
            "### 09:05 — MJ\n"
            "Journal entry between sections.\n\n"
            "## Later\n"
            "later section\n"
        )
        document = self._daily_document(markdown)
        preserved_tail = markdown[markdown.index("### 09:05") :]

        response = self._replace_daily_report(document)

        self.assertEqual(response.status_code, 201, response.content)
        self.assertTrue(response.data["markdown"].endswith(preserved_tail))

    def test_middle_section_rewrite_preserves_existing_behavior(self):
        markdown = "# 2026-07-27\n\n## Report\nold report\n\n## Later\nlater section\n"
        document = self._daily_document(markdown)

        response = self._replace_daily_report(document)

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(
            response.data["markdown"],
            "# 2026-07-27\n\n## Report\nnew report\n\n## Later\nlater section\n",
        )

    def test_last_section_without_entries_preserves_existing_behavior(self):
        document = self._daily_document("# 2026-07-27\n\n## Report\nold report\n")

        response = self._replace_daily_report(document)

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(
            response.data["markdown"],
            "# 2026-07-27\n\n## Report\nnew report\n",
        )
