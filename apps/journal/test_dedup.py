"""Tests for server-side task/goal create dedup (apps/journal/dedup.py).

Guards the resurrection-loop fix: a maintenance/cron turn that re-derives a
task from journal prose must not recreate one the user already completed.
The canary 2026-06-07 pairs are exercised directly.
"""

from __future__ import annotations

from datetime import date, timedelta

from django.test import TestCase
from django.utils import timezone

from apps.tenants.models import Tenant, User

from .dedup import find_duplicate_goal, find_duplicate_task, titles_match
from .models import Goal, Task
from .reconciliation import apply_subtask_create


class TitlesMatchTest(TestCase):
    """Pure matcher — order/punctuation-insensitive, conservative on negatives."""

    def test_exact_modulo_case(self):
        self.assertTrue(titles_match("Customs clearance paperwork", "customs clearance paperwork"))

    def test_word_order_and_punctuation_insensitive(self):
        self.assertTrue(
            titles_match(
                "Google Cloud TLS update — trust new GTS root CAs",
                "Trust new GTS root CAs: Google Cloud TLS update",
            )
        )

    def test_containment_canary_customs(self):
        # The real 2026-06-07 resurrection pair.
        self.assertTrue(
            titles_match(
                "Fill out customs clearance paperwork for Jamaica shipments",
                "Customs clearance paperwork",
            )
        )

    def test_containment_canary_hotel(self):
        self.assertTrue(
            titles_match(
                "Book hotel for cousin's wedding in March (Jamaica)",
                "Book hotel for Jamaica wedding",
            )
        )

    def test_distinct_titles_do_not_match(self):
        self.assertFalse(titles_match("Book hotel for Jamaica wedding", "Acknowledge Intuit rejection"))
        self.assertFalse(titles_match("Pay student loan minimum", "Buy SheaMoisture pomade from iHerb"))

    def test_two_word_stub_does_not_swallow_specific_task(self):
        # The symmetric failure of the resurrection bug: a generic two-word stub
        # must not collapse a longer, more-specific task. (Review-surfaced.)
        self.assertFalse(titles_match("Call mom", "Call mom's lawyer"))
        self.assertFalse(titles_match("Pay rent", "Pay rent for March"))
        self.assertFalse(titles_match("Buy gift", "Buy gift card"))
        self.assertFalse(titles_match("Send invoice", "Send invoice reminder"))
        self.assertFalse(titles_match("Email boss", "Email boss's assistant"))

    def test_distinct_three_token_tasks_not_matched(self):
        self.assertFalse(titles_match("Pay electric bill", "Pay water bill"))

    def test_short_title_requires_exact(self):
        # A single content word must not collapse into a longer unrelated task.
        self.assertFalse(titles_match("Email", "Email Sarah the quarterly contract draft"))

    def test_stopword_only_titles_do_not_match(self):
        self.assertFalse(titles_match("the to a", "for of in"))


class FindDuplicateTaskTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="deduptask", password="pass")
        self.tenant = Tenant.objects.create(user=self.user, status="active")
        self.now = timezone.now()

    def test_matches_open_task(self):
        existing = Task.objects.create(tenant=self.tenant, title="Customs clearance paperwork")
        dup = find_duplicate_task(
            self.tenant,
            "Fill out customs clearance paperwork for Jamaica shipments",
            now=self.now,
        )
        self.assertIsNotNone(dup)
        self.assertEqual(dup.id, existing.id)

    def test_matches_recently_completed_task(self):
        existing = Task.objects.create(
            tenant=self.tenant,
            title="Fill out customs clearance paperwork for Jamaica shipments",
        )
        existing.complete()  # done + completed_at/updated_at = now
        dup = find_duplicate_task(self.tenant, "Customs clearance paperwork", now=self.now)
        self.assertIsNotNone(dup)
        self.assertEqual(dup.id, existing.id)
        self.assertEqual(dup.status, Task.Status.DONE)

    def test_completed_outside_window_not_matched(self):
        existing = Task.objects.create(tenant=self.tenant, title="Customs clearance paperwork")
        existing.complete()
        # 30 days later the completion is >14d old → no longer suppresses a re-create.
        dup = find_duplicate_task(
            self.tenant,
            "Customs clearance paperwork",
            now=self.now + timedelta(days=30),
        )
        self.assertIsNone(dup)

    def test_open_match_preferred_over_closed(self):
        closed = Task.objects.create(tenant=self.tenant, title="Customs clearance paperwork")
        closed.complete()
        open_task = Task.objects.create(tenant=self.tenant, title="Customs clearance paperwork")
        dup = find_duplicate_task(self.tenant, "Customs clearance paperwork", now=self.now)
        self.assertEqual(dup.id, open_task.id)

    def test_distinct_task_not_matched(self):
        Task.objects.create(tenant=self.tenant, title="Buy SheaMoisture pomade")
        dup = find_duplicate_task(self.tenant, "Acknowledge Intuit rejection", now=self.now)
        self.assertIsNone(dup)

    def test_tenant_isolation(self):
        other_user = User.objects.create_user(username="otherdedup", password="pass")
        other = Tenant.objects.create(user=other_user, status="active")
        Task.objects.create(tenant=other, title="Customs clearance paperwork")
        dup = find_duplicate_task(self.tenant, "Customs clearance paperwork", now=self.now)
        self.assertIsNone(dup)

    def test_blank_title_returns_none(self):
        Task.objects.create(tenant=self.tenant, title="Customs clearance paperwork")
        self.assertIsNone(find_duplicate_task(self.tenant, "   ", now=self.now))


class FindDuplicateGoalTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="dedupgoal", password="pass")
        self.tenant = Tenant.objects.create(user=self.user, status="active")
        self.now = timezone.now()

    def test_matches_active_goal(self):
        existing = Goal.objects.create(
            tenant=self.tenant,
            title="Achieve debt-free status and financial freedom",
        )
        dup = find_duplicate_goal(self.tenant, "Achieve debt-free status", now=self.now)
        self.assertIsNotNone(dup)
        self.assertEqual(dup.id, existing.id)

    def test_distinct_goal_not_matched(self):
        Goal.objects.create(tenant=self.tenant, title="Build the Yard Talk Mac app")
        dup = find_duplicate_goal(self.tenant, "Establish a Security Champions program", now=self.now)
        self.assertIsNone(dup)


class SubtaskCreateDedupTest(TestCase):
    """apply_subtask_create must not resurrect a completed top-level task or
    duplicate a sibling, but identical subtasks under different parents are OK."""

    def setUp(self):
        self.user = User.objects.create_user(username="subtaskdedup", password="pass")
        self.tenant = Tenant.objects.create(user=self.user, status="active")
        self.parent = Task.objects.create(tenant=self.tenant, title="Plan the Jamaica trip")

    def test_subtask_matching_completed_top_level_task_is_skipped(self):
        done = Task.objects.create(
            tenant=self.tenant,
            title="Fill out customs clearance paperwork for Jamaica shipments",
        )
        done.complete()
        result = apply_subtask_create(
            tenant=self.tenant,
            parent_task_id=str(self.parent.id),
            title="Customs clearance paperwork",
            source_date=date(2026, 6, 8),
        )
        self.assertIsNone(result)
        self.assertFalse(Task.objects.filter(parent_task=self.parent).exists())

    def test_subtask_matching_sibling_is_skipped(self):
        Task.objects.create(tenant=self.tenant, title="Book the flights", parent_task=self.parent)
        result = apply_subtask_create(
            tenant=self.tenant,
            parent_task_id=str(self.parent.id),
            title="Book the flights",
            source_date=date(2026, 6, 8),
        )
        self.assertIsNone(result)
        self.assertEqual(Task.objects.filter(parent_task=self.parent, title="Book the flights").count(), 1)

    def test_same_subtask_title_under_different_parent_is_allowed(self):
        other_parent = Task.objects.create(tenant=self.tenant, title="Plan the office move")
        Task.objects.create(tenant=self.tenant, title="Buy packing supplies", parent_task=other_parent)
        result = apply_subtask_create(
            tenant=self.tenant,
            parent_task_id=str(self.parent.id),
            title="Buy packing supplies",
            source_date=date(2026, 6, 8),
        )
        self.assertIsNotNone(result)
        self.assertTrue(Task.objects.filter(parent_task=self.parent, title="Buy packing supplies").exists())
