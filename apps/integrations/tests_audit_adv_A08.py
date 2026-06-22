"""Adversarial audit regression tests for cluster A08.

integrations#1: RuntimeDailyNoteAppendView used an unanchored substring search
(`md.find("## " + heading)`) to locate a daily-note section. That clobbered
the wrong section in two ways:
  (1) prefix collision — "## Report" matched "## Report Card",
  (2) embedded "## " inside another section's body shifted the slice boundary.
The fix anchors the heading match to a full line. These tests assert the
correct section is updated and neighbouring sections are preserved.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.journal.models import Document
from apps.tenants.models import Tenant
from apps.tenants.test_utils import seed_internal_key

User = get_user_model()


@override_settings(NBHD_INTERNAL_API_KEY="test-key")
class DailyNoteSectionAnchoringTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="a08user", password="pass")
        self.tenant = Tenant.objects.create(user=self.user, status="active")
        seed_internal_key(self.tenant)
        self.client = APIClient()
        self.headers = {
            "HTTP_X_NBHD_INTERNAL_KEY": "test-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }
        self.url = f"/api/v1/integrations/runtime/{self.tenant.id}/daily-note/append/"

    def _seed(self, markdown: str, slug: str = "2026-03-01"):
        return Document.objects.create(
            tenant=self.tenant,
            kind="daily",
            slug=slug,
            title=slug,
            markdown=markdown,
        )

    def test_prefix_collision_does_not_clobber_longer_heading(self):
        """`## Report` must target the Report section, not `## Report Card`."""
        doc = self._seed(
            "## Report Card\nGrade: A\n\n## Report\nold report\n",
        )
        resp = self.client.post(
            self.url,
            {"content": "new report", "date": doc.slug, "section_slug": "report"},
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 201)
        md = resp.data["markdown"]
        # The longer, prefix-overlapping section is untouched.
        self.assertIn("## Report Card\nGrade: A", md)
        # The targeted section's body was replaced.
        self.assertIn("## Report\nnew report", md)
        self.assertNotIn("old report", md)

    def test_embedded_hash_hash_line_in_other_body_not_a_boundary(self):
        """A non-heading line cannot be confused for the section's heading line."""
        # "## Report" appears mid-line inside the Log body; it must NOT be matched
        # as the section heading by the (now line-anchored) search.
        doc = self._seed(
            "## Log\nI read the ## Report doc today\n\n## Report\nold body\n",
        )
        resp = self.client.post(
            self.url,
            {"content": "fresh body", "date": doc.slug, "section_slug": "report"},
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 201)
        md = resp.data["markdown"]
        # Log section body (with the mid-line "## Report" mention) is preserved.
        self.assertIn("I read the ## Report doc today", md)
        # Real Report section was updated.
        self.assertIn("## Report\nfresh body", md)
        self.assertNotIn("old body", md)

    def test_standard_section_update_preserves_neighbours(self):
        """Baseline: updating a middle section leaves siblings intact."""
        doc = self._seed(
            "## Morning Report\nold morning\n\n## Log\nold log\n\n## Evening Check-in\nold evening\n",
        )
        resp = self.client.post(
            self.url,
            {"content": "new log", "date": doc.slug, "section_slug": "log"},
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 201)
        md = resp.data["markdown"]
        self.assertIn("## Morning Report\nold morning", md)
        self.assertIn("## Log\nnew log", md)
        self.assertIn("## Evening Check-in\nold evening", md)
        self.assertNotIn("old log", md)
