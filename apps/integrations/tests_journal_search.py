"""Tests for the RuntimeJournalSearchView endpoint."""
from __future__ import annotations

from django.test import TestCase
from django.test.utils import override_settings

from apps.journal.models import Document
from apps.tenants.services import create_tenant


@override_settings(NBHD_INTERNAL_API_KEY="shared-key")
class RuntimeJournalSearchViewTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Search Tenant", telegram_chat_id=808080)
        self.other_tenant = create_tenant(display_name="Other Tenant", telegram_chat_id=909090)

        self.doc1 = Document.objects.create(
            tenant=self.tenant,
            kind="daily",
            slug="2026-02-10",
            title="Daily Note 2026-02-10",
            markdown="Today I worked on the **garden** project and planted tomatoes.",
        )
        self.doc2 = Document.objects.create(
            tenant=self.tenant,
            kind="goal",
            slug="fitness",
            title="Fitness Goal",
            markdown="Run a marathon by end of year. Training plan includes weekly long runs.",
        )
        self.doc3 = Document.objects.create(
            tenant=self.other_tenant,
            kind="daily",
            slug="2026-02-10",
            title="Other Tenant Note",
            markdown="This tenant also has a garden but should not appear.",
        )

    def _url(self, tenant_id=None):
        tid = tenant_id or self.tenant.id
        return f"/api/v1/integrations/runtime/{tid}/journal/search/"

    def _headers(self, tenant_id=None, key="shared-key"):
        return {
            "HTTP_X_NBHD_INTERNAL_KEY": key,
            "HTTP_X_NBHD_TENANT_ID": str(tenant_id or self.tenant.id),
        }

    def test_search_returns_matching_documents(self):
        response = self.client.get(self._url(), {"q": "garden"}, **self._headers())
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["query"], "garden")
        self.assertGreaterEqual(body["count"], 1)
        slugs = [r["slug"] for r in body["results"]]
        self.assertIn("2026-02-10", slugs)

    def test_search_respects_kind_filter(self):
        response = self.client.get(
            self._url(), {"q": "marathon", "kind": "goal"}, **self._headers()
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertGreaterEqual(body["count"], 1)
        for result in body["results"]:
            self.assertEqual(result["kind"], "goal")

    def test_search_kind_filter_excludes_other_kinds(self):
        response = self.client.get(
            self._url(), {"q": "garden", "kind": "goal"}, **self._headers()
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["count"], 0)

    def test_empty_query_returns_400(self):
        response = self.client.get(self._url(), {"q": ""}, **self._headers())
        self.assertEqual(response.status_code, 400)

    def test_missing_query_returns_400(self):
        response = self.client.get(self._url(), **self._headers())
        self.assertEqual(response.status_code, 400)

    def test_search_does_not_leak_other_tenants_docs(self):
        response = self.client.get(self._url(), {"q": "garden"}, **self._headers())
        self.assertEqual(response.status_code, 200)
        body = response.json()
        slugs_and_titles = [(r["slug"], r["title"]) for r in body["results"]]
        for slug, title in slugs_and_titles:
            self.assertNotEqual(title, "Other Tenant Note")

    def test_requires_internal_auth(self):
        response = self.client.get(self._url(), {"q": "garden"})
        self.assertEqual(response.status_code, 401)

    def test_respects_limit_parameter(self):
        response = self.client.get(
            self._url(), {"q": "garden", "limit": "1"}, **self._headers()
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertLessEqual(body["count"], 1)
