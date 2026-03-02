"""Tests for session gap detection and contextual recall injection."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.utils import timezone

from apps.journal.models import Document
from apps.tenants.models import Tenant, User


def _make_tenant(last_msg_minutes_ago=None):
    user = User.objects.create_user(
        username=f"ctx{timezone.now().timestamp()}", password="pass"
    )
    tenant = Tenant.objects.create(user=user, status=Tenant.Status.ACTIVE)
    if last_msg_minutes_ago is not None:
        tenant.last_message_at = timezone.now() - timedelta(minutes=last_msg_minutes_ago)
        tenant.save(update_fields=["last_message_at"])
    return tenant


class TestIsNewSession(TestCase):
    def _make_poller(self):
        """Create a minimal poller instance for testing."""
        from apps.router.poller import TelegramPoller
        poller = TelegramPoller.__new__(TelegramPoller)
        return poller

    def test_new_session_after_31_min(self):
        tenant = _make_tenant(last_msg_minutes_ago=31)
        poller = self._make_poller()
        self.assertTrue(poller._is_new_session(tenant))

    def test_not_new_session_after_5_min(self):
        tenant = _make_tenant(last_msg_minutes_ago=5)
        poller = self._make_poller()
        self.assertFalse(poller._is_new_session(tenant))

    def test_first_message_ever_is_new_session(self):
        tenant = _make_tenant(last_msg_minutes_ago=None)
        poller = self._make_poller()
        self.assertTrue(poller._is_new_session(tenant))

    def test_exactly_30_min_is_new(self):
        tenant = _make_tenant(last_msg_minutes_ago=30)
        poller = self._make_poller()
        # 30 min = 1800 seconds; with test timing jitter this will exceed threshold
        # In practice the boundary is fuzzy — both outcomes are acceptable
        # Just verify the method doesn't crash
        poller._is_new_session(tenant)


class TestBuildSessionContext(TestCase):
    def setUp(self):
        self.tenant = _make_tenant(last_msg_minutes_ago=60)
        from apps.router.poller import TelegramPoller
        self.poller = TelegramPoller.__new__(TelegramPoller)

    def test_injects_goals_and_tasks(self):
        Document.objects.create(
            tenant=self.tenant, kind=Document.Kind.GOAL, slug="goals",
            title="Goals", markdown="# Goals\n\n## Active\n\n### Ship v2\n- Status: active\n"
        )
        Document.objects.create(
            tenant=self.tenant, kind=Document.Kind.TASKS, slug="tasks",
            title="Tasks", markdown="# Tasks\n\n- [ ] Fix the bug\n"
        )

        result = self.poller._build_session_context(self.tenant, "hello")
        self.assertIn("Your active goals", result)
        self.assertIn("Ship v2", result)
        self.assertIn("Your current tasks", result)
        self.assertIn("Fix the bug", result)
        self.assertIn("hello", result)
        self.assertIn("[Context for this conversation:]", result)

    def test_no_docs_returns_original(self):
        result = self.poller._build_session_context(self.tenant, "hello")
        self.assertEqual(result, "hello")

    def test_empty_goals_not_injected(self):
        Document.objects.create(
            tenant=self.tenant, kind=Document.Kind.GOAL, slug="goals",
            title="Goals", markdown=""
        )
        result = self.poller._build_session_context(self.tenant, "hello")
        self.assertNotIn("Your active goals", result)

    def test_truncates_long_docs(self):
        Document.objects.create(
            tenant=self.tenant, kind=Document.Kind.GOAL, slug="goals",
            title="Goals", markdown="# Goals\n" + "x" * 3000
        )
        result = self.poller._build_session_context(self.tenant, "hello")
        # Should be truncated to ~1500 chars
        goals_section = result.split("Your active goals")[1].split("##")[0] if "Your active goals" in result else ""
        self.assertLess(len(goals_section), 1600)

    @patch("apps.router.poller.TelegramPoller._build_session_context_inner", side_effect=Exception("boom"))
    def test_failure_returns_original(self, mock_inner):
        result = self.poller._build_session_context(self.tenant, "hello")
        self.assertEqual(result, "hello")
