"""Adversarial-audit cluster A03 regression tests.

FA-0341: the goal detail page (frontend/app/journal/goal/[slug]) renders the
full goal body, but the Horizons payload previously only emitted a 200-char
`preview`. Real goals exceed 200 chars (template scaffolding alone is ~190),
so the detail page showed a mid-sentence stub. The fix adds a full `markdown`
field to the goals[] serialization. These tests assert the full body is sent
(untruncated) for both typed and legacy goals, while `preview` stays capped.
"""

from __future__ import annotations

from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.journal.models import Document, Goal
from apps.tenants.services import create_tenant


class HorizonsGoalFullMarkdownTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="A03-Goals", telegram_chat_id=900930)
        self.client = APIClient()
        token = RefreshToken.for_user(self.tenant.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")

    def _goal_by_title(self, title):
        resp = self.client.get("/api/v1/dashboard/horizons/")
        self.assertEqual(resp.status_code, 200)
        for g in resp.json()["goals"]:
            if g["title"] == title:
                return g
        self.fail(f"goal {title!r} not in horizons payload")

    def test_typed_goal_emits_full_markdown_untruncated(self):
        long_body = "Target: " + ("x" * 400)  # well past the 200-char preview cap
        Goal.objects.create(
            tenant=self.tenant,
            title="Long typed goal",
            description=long_body,
            status=Goal.Status.ACTIVE,
        )
        g = self._goal_by_title("Long typed goal")
        self.assertIn("markdown", g)
        self.assertEqual(g["markdown"], long_body)
        # preview stays capped so the list cards are unchanged.
        self.assertLessEqual(len(g["preview"]), 200)

    def test_legacy_document_emits_full_markdown_untruncated(self):
        long_body = "Why: " + ("y" * 400)
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.GOAL,
            slug="legacy-long-goal",
            title="Long legacy goal",
            markdown=long_body,
        )
        g = self._goal_by_title("Long legacy goal")
        self.assertEqual(g["markdown"], long_body)
        self.assertLessEqual(len(g["preview"]), 200)

    def test_markdown_defaults_to_empty_string_when_blank(self):
        Goal.objects.create(
            tenant=self.tenant,
            title="Empty body goal",
            description="",
            status=Goal.Status.ACTIVE,
        )
        g = self._goal_by_title("Empty body goal")
        self.assertEqual(g["markdown"], "")
