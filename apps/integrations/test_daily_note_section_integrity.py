"""Regression tests for scoped markdown section writes."""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.journal.models import Document, NoteTemplate
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

    def test_legacy_short_heading_routes_to_existing_short_section(self):
        document = self._daily_document("# 2026-07-27\n\n## News\nold legacy news\n\n## Later\nlater section\n")

        response = self.client.post(
            self.daily_url,
            {
                "content": "new legacy news",
                "date": document.slug,
                "section_slug": "news",
            },
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 201, response.content)
        self.assertIn("## News\nnew legacy news", response.data["markdown"])
        self.assertNotIn("old legacy news", response.data["markdown"])
        self.assertNotIn("## News & Interests", response.data["markdown"])
        self.assertEqual(response.data["markdown"].count("## News\n"), 1)

    def test_canonical_heading_routes_to_existing_canonical_section(self):
        document = self._daily_document(
            "# 2026-07-27\n\n## News & Interests\nold canonical news\n\n## Later\nlater section\n"
        )

        response = self.client.post(
            self.daily_url,
            {
                "content": "new canonical news",
                "date": document.slug,
                "section_slug": "news",
            },
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 201, response.content)
        self.assertIn(
            "## News & Interests\nnew canonical news",
            response.data["markdown"],
        )
        self.assertNotIn("old canonical news", response.data["markdown"])
        self.assertNotIn("## News\n", response.data["markdown"])
        self.assertEqual(response.data["markdown"].count("## News & Interests"), 1)

    def test_canonical_heading_wins_when_canonical_and_legacy_both_exist(self):
        document = self._daily_document(
            "# 2026-07-27\n\n## News & Interests\nold canonical news\n\n## News\nlegacy news stays\n"
        )

        response = self.client.post(
            self.daily_url,
            {
                "content": "new canonical news",
                "date": document.slug,
                "section_slug": "news",
            },
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 201, response.content)
        self.assertIn(
            "## News & Interests\nnew canonical news",
            response.data["markdown"],
        )
        self.assertIn("## News\nlegacy news stays", response.data["markdown"])
        self.assertNotIn("old canonical news", response.data["markdown"])

    def test_absent_section_is_created_with_canonical_default_title(self):
        document = self._daily_document("# 2026-07-27\n")

        response = self.client.post(
            self.daily_url,
            {
                "content": "Three priorities",
                "date": document.slug,
                "section_slug": "focus",
            },
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 201, response.content)
        self.assertIn("## Today's Focus\nThree priorities", response.data["markdown"])
        self.assertNotIn("## Focus\n", response.data["markdown"])

    def test_tenant_template_title_wins_when_section_is_absent(self):
        NoteTemplate.objects.create(
            tenant=self.tenant,
            slug="custom",
            name="Custom",
            is_default=True,
            sections=[
                {
                    "slug": "news",
                    "title": "Neighborhood Pulse",
                    "content": "",
                    "source": "agent",
                }
            ],
        )
        document = self._daily_document("# 2026-07-27\n")

        response = self.client.post(
            self.daily_url,
            {
                "content": "Local updates",
                "date": document.slug,
                "section_slug": "news",
            },
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 201, response.content)
        self.assertIn("## Neighborhood Pulse\nLocal updates", response.data["markdown"])
        self.assertNotIn("## News\n", response.data["markdown"])
        self.assertNotIn("## News & Interests", response.data["markdown"])

    def test_unknown_slug_keeps_derived_heading_behavior(self):
        document = self._daily_document("# 2026-07-27\n")

        response = self.client.post(
            self.daily_url,
            {
                "content": "Unknown section content",
                "date": document.slug,
                "section_slug": "custom-insights",
            },
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 201, response.content)
        self.assertIn(
            "## Custom Insights\nUnknown section content",
            response.data["markdown"],
        )
