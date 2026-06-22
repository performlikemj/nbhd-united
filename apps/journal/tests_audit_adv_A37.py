"""Audit tests for A37 cluster — cache-signal coverage for Task and Goal.

Confirms that post_save on Task and Goal bumps the 'journal' cache tag so that
DocumentDetailView's @tenant_cache(ttl=60, tag='journal') is invalidated after
typed-lifecycle mutations.  These are the receivers added in
apps/common/cache_signals.py as the fix for journal-core#2.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from apps.journal.models import Goal, Task
from apps.tenants.models import Tenant, User  # noqa: F401  (User used in setUp)


class TaskCacheSignalTest(TestCase):
    """post_save on Task must bump the 'journal' (and 'dashboard') tags."""

    def setUp(self):
        self.user = User.objects.create_user(username="a37-task", password="x")
        self.tenant = Tenant.objects.create(user=self.user, status="active")

    def _make_task(self, title="Test task"):
        return Task.objects.create(tenant=self.tenant, title=title)

    def test_task_create_bumps_journal_tag(self):
        with patch("apps.common.cache_signals.bump_tags") as mock_bump:
            self._make_task()
        calls = [set(call.args[1]) for call in mock_bump.call_args_list]
        self.assertTrue(
            any("journal" in tags for tags in calls),
            f"Expected 'journal' tag bump on Task create; got calls={calls}",
        )

    def test_task_save_bumps_dashboard_tag(self):
        task = self._make_task()
        with patch("apps.common.cache_signals.bump_tags") as mock_bump:
            task.title = "Updated"
            task.save(update_fields=["title", "updated_at"])
        calls = [set(call.args[1]) for call in mock_bump.call_args_list]
        self.assertTrue(
            any("dashboard" in tags for tags in calls),
            f"Expected 'dashboard' tag bump on Task save; got calls={calls}",
        )

    def test_task_complete_bumps_journal_tag(self):
        task = self._make_task()
        with patch("apps.common.cache_signals.bump_tags") as mock_bump:
            task.complete()
        calls = [set(call.args[1]) for call in mock_bump.call_args_list]
        self.assertTrue(
            any("journal" in tags for tags in calls),
            f"Expected 'journal' tag bump on Task.complete(); got calls={calls}",
        )

    def test_task_delete_bumps_journal_tag(self):
        task = self._make_task()
        with patch("apps.common.cache_signals.bump_tags") as mock_bump:
            task.delete()
        calls = [set(call.args[1]) for call in mock_bump.call_args_list]
        self.assertTrue(
            any("journal" in tags for tags in calls),
            f"Expected 'journal' tag bump on Task delete; got calls={calls}",
        )


class GoalCacheSignalTest(TestCase):
    """post_save on Goal must bump the 'journal' (and 'dashboard') tags."""

    def setUp(self):
        self.user = User.objects.create_user(username="a37-goal", password="x")
        self.tenant = Tenant.objects.create(user=self.user, status="active")

    def _make_goal(self, title="Test goal"):
        return Goal.objects.create(tenant=self.tenant, title=title)

    def test_goal_create_bumps_journal_tag(self):
        with patch("apps.common.cache_signals.bump_tags") as mock_bump:
            self._make_goal()
        calls = [set(call.args[1]) for call in mock_bump.call_args_list]
        self.assertTrue(
            any("journal" in tags for tags in calls),
            f"Expected 'journal' tag bump on Goal create; got calls={calls}",
        )

    def test_goal_mark_achieved_bumps_journal_tag(self):
        goal = self._make_goal()
        with patch("apps.common.cache_signals.bump_tags") as mock_bump:
            goal.mark_achieved()
        calls = [set(call.args[1]) for call in mock_bump.call_args_list]
        self.assertTrue(
            any("journal" in tags for tags in calls),
            f"Expected 'journal' tag bump on Goal.mark_achieved(); got calls={calls}",
        )

    def test_goal_abandon_bumps_dashboard_tag(self):
        goal = self._make_goal()
        with patch("apps.common.cache_signals.bump_tags") as mock_bump:
            goal.abandon()
        calls = [set(call.args[1]) for call in mock_bump.call_args_list]
        self.assertTrue(
            any("dashboard" in tags for tags in calls),
            f"Expected 'dashboard' tag bump on Goal.abandon(); got calls={calls}",
        )

    def test_goal_delete_bumps_journal_tag(self):
        goal = self._make_goal()
        with patch("apps.common.cache_signals.bump_tags") as mock_bump:
            goal.delete()
        calls = [set(call.args[1]) for call in mock_bump.call_args_list]
        self.assertTrue(
            any("journal" in tags for tags in calls),
            f"Expected 'journal' tag bump on Goal delete; got calls={calls}",
        )
