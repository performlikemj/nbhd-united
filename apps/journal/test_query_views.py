"""End-to-end tests for ``apps.journal.query_views.JournalQueryView``.

Mirrors the finance query-view tests: hits the real URL, exercises auth,
the envelope shape, per-resource filter validation, and date windowing
against multiple ``window_field`` options.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.journal.models import Goal, JournalEntry, Task
from apps.tenants.services import create_tenant
from apps.tenants.test_utils import seed_internal_key


@override_settings(NBHD_INTERNAL_API_KEY="test-internal-key")
class JournalQueryViewTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="JournalQuery", telegram_chat_id=902001)
        seed_internal_key(self.tenant)
        self.other = create_tenant(display_name="JournalQueryOther", telegram_chat_id=902002)
        # User tz so "today" math is deterministic against our fake clock.
        self.tenant.user.timezone = "America/Los_Angeles"
        self.tenant.user.save(update_fields=["timezone"])

        # Entries across a couple of dates + moods.
        JournalEntry.objects.create(
            tenant=self.tenant,
            date=date(2026, 5, 10),
            mood="grateful",
            energy="high",
            wins=["finished the report"],
            challenges=["sore knee"],
            reflection="good day",
            raw_text="raw",
        )
        JournalEntry.objects.create(
            tenant=self.tenant,
            date=date(2026, 5, 14),
            mood="anxious",
            energy="low",
            wins=[],
            challenges=["deadline pressure"],
            reflection="long",
            raw_text="raw",
        )
        # An entry that belongs to a different tenant — must never leak.
        JournalEntry.objects.create(
            tenant=self.other,
            date=date(2026, 5, 14),
            mood="leak",
            energy="high",
            wins=[],
            challenges=[],
            reflection="",
            raw_text="raw",
        )

        # Goals: one active with target_date in window, one achieved long ago.
        self.goal_active = Goal.objects.create(
            tenant=self.tenant,
            title="Ship typed lifecycle",
            description="",
            pillar="lessons",
            status=Goal.Status.ACTIVE,
            target_date=date(2026, 5, 31),
        )
        self.goal_done = Goal.objects.create(
            tenant=self.tenant,
            title="Old win",
            description="",
            pillar="fuel",
            status=Goal.Status.ACHIEVED,
            target_date=date(2026, 1, 1),
            achieved_at=datetime(2026, 1, 5, 12, 0, tzinfo=UTC),
        )

        # Tasks: open with due date inside window; done in same window (completed_at);
        # overdue (past due, not done); no-due-date.
        self.task_open = Task.objects.create(
            tenant=self.tenant,
            title="Write the doc",
            status=Task.Status.OPEN,
            pillar="lessons",
            due_date=date(2026, 5, 20),
            parent_goal=self.goal_active,
        )
        self.task_done = Task.objects.create(
            tenant=self.tenant,
            title="Pay loan",
            status=Task.Status.DONE,
            pillar="gravity",
            due_date=date(2026, 5, 5),
            completed_at=datetime(2026, 5, 6, 9, 0, tzinfo=UTC),
        )
        self.task_overdue = Task.objects.create(
            tenant=self.tenant,
            title="Reply email",
            status=Task.Status.OPEN,
            pillar="lessons",
            due_date=date(2026, 5, 1),
        )
        self.task_no_due = Task.objects.create(
            tenant=self.tenant,
            title="Idle thought",
            status=Task.Status.OPEN,
            pillar="lessons",
            due_date=None,
        )

    def _post(self, body, *, tenant_id=None, key="test-internal-key"):
        tid = tenant_id or str(self.tenant.id)
        return self.client.post(
            f"/api/v1/journal/runtime/{tid}/query/",
            data=body,
            content_type="application/json",
            HTTP_X_NBHD_INTERNAL_KEY=key,
            HTTP_X_NBHD_TENANT_ID=tid,
        )

    # ── Auth + envelope ───────────────────────────────────────────────

    def test_missing_key_401(self):
        self.assertEqual(self._post({"resource": "tasks"}, key="").status_code, 401)

    def test_wrong_tenant_scope_401(self):
        r = self.client.post(
            f"/api/v1/journal/runtime/{self.tenant.id}/query/",
            data={"resource": "tasks"},
            content_type="application/json",
            HTTP_X_NBHD_INTERNAL_KEY="test-internal-key",
            HTTP_X_NBHD_TENANT_ID=str(self.other.id),
        )
        self.assertEqual(r.status_code, 401)

    def test_response_envelope_has_meta(self):
        r = self._post({"resource": "tasks"})
        self.assertEqual(r.status_code, 200)
        meta = r.json()["meta"]
        for k in (
            "schema_version",
            "computed_at",
            "tenant_tz",
            "as_of",
            "window_resolved_to",
            "row_count",
            "has_more",
            "query_hash",
        ):
            self.assertIn(k, meta)
        self.assertEqual(meta["tenant_tz"], "America/Los_Angeles")
        self.assertTrue(meta["query_hash"].startswith("sha256:"))

    def test_tenant_isolation(self):
        # Even with no window filter, the "leak" entry from self.other must not appear.
        r = self._post({"resource": "entries", "window": {"kind": "all"}})
        moods = {row["mood"] for row in r.json()["data"]}
        self.assertNotIn("leak", moods)

    # ── Entries ───────────────────────────────────────────────────────

    def test_entries_default_window_field_is_date(self):
        r = self._post({"resource": "entries", "window": {"kind": "between", "value": ["2026-05-12", "2026-05-15"]}})
        moods = sorted(row["mood"] for row in r.json()["data"])
        self.assertEqual(moods, ["anxious"])

    def test_entries_filter_energy(self):
        r = self._post({"resource": "entries", "window": {"kind": "all"}, "filter": {"energy": "high"}})
        moods = sorted(row["mood"] for row in r.json()["data"])
        self.assertEqual(moods, ["grateful"])

    def test_entries_count_by_energy(self):
        r = self._post(
            {
                "resource": "entries",
                "window": {"kind": "all"},
                "aggregate": "count",
                "group_by": "energy",
            }
        )
        data = {row["group"]["energy"]: row["count"] for row in r.json()["data"]}
        self.assertEqual(data, {"high": 1, "low": 1})

    # ── Tasks ──────────────────────────────────────────────────────────

    def test_tasks_default_window_field_is_due_date(self):
        # Window 2026-05-10 → 2026-05-25 catches task_open (due 05-20) but
        # not task_done (due 05-05) or task_overdue (due 05-01).
        r = self._post(
            {
                "resource": "tasks",
                "window": {"kind": "between", "value": ["2026-05-10", "2026-05-25"]},
            }
        )
        titles = sorted(row["title"] for row in r.json()["data"])
        self.assertEqual(titles, ["Write the doc"])

    def test_tasks_window_field_completed_at(self):
        # Same window resolved against completed_at picks up task_done (completed 05-06).
        # Both 05-06 falls outside 05-10..05-25 — try a wider window.
        r = self._post(
            {
                "resource": "tasks",
                "window": {"kind": "between", "value": ["2026-05-01", "2026-05-31"]},
                "window_field": "completed_at",
            }
        )
        titles = sorted(row["title"] for row in r.json()["data"])
        self.assertEqual(titles, ["Pay loan"])

    def test_tasks_filter_status(self):
        r = self._post({"resource": "tasks", "filter": {"status": "done"}})
        titles = sorted(row["title"] for row in r.json()["data"])
        self.assertEqual(titles, ["Pay loan"])

    def test_tasks_filter_overdue(self):
        # Freeze "now" so the overdue check is deterministic. Tenant tz is LA.
        frozen = datetime(2026, 5, 10, 17, 0, tzinfo=UTC)
        with patch("apps.journal.query_views.dj_tz.now", return_value=frozen):
            r = self._post({"resource": "tasks", "filter": {"overdue": True}})
        titles = sorted(row["title"] for row in r.json()["data"])
        self.assertEqual(titles, ["Reply email"])

    def test_tasks_unknown_filter_400(self):
        r = self._post({"resource": "tasks", "filter": {"colour": "red"}})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"], "unknown_filter_keys")

    def test_tasks_unknown_window_field_400(self):
        r = self._post(
            {
                "resource": "tasks",
                "window": {"kind": "all"},
                "window_field": "tomorrow",
            }
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"], "unknown_window_field")

    def test_tasks_count_by_status(self):
        r = self._post(
            {
                "resource": "tasks",
                "aggregate": "count",
                "group_by": "status",
            }
        )
        data = {row["group"]["status"]: row["count"] for row in r.json()["data"]}
        self.assertEqual(data, {"open": 3, "done": 1})

    def test_tasks_sum_not_allowed(self):
        r = self._post(
            {
                "resource": "tasks",
                "aggregate": "sum",
                "aggregate_field": "title",
            }
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"], "invalid_aggregate_field")

    # ── Goals ──────────────────────────────────────────────────────────

    def test_goals_default_window_field_is_target_date(self):
        r = self._post(
            {
                "resource": "goals",
                "window": {"kind": "between", "value": ["2026-05-01", "2026-05-31"]},
            }
        )
        titles = sorted(row["title"] for row in r.json()["data"])
        self.assertEqual(titles, ["Ship typed lifecycle"])

    def test_goals_filter_status(self):
        r = self._post({"resource": "goals", "filter": {"status": "achieved"}})
        titles = sorted(row["title"] for row in r.json()["data"])
        self.assertEqual(titles, ["Old win"])

    def test_goals_filter_pillar(self):
        r = self._post({"resource": "goals", "filter": {"pillar": "fuel"}})
        titles = sorted(row["title"] for row in r.json()["data"])
        self.assertEqual(titles, ["Old win"])

    # ── Field projection ──────────────────────────────────────────────

    def test_fields_projection_keeps_identifier(self):
        r = self._post({"resource": "tasks", "fields": ["title"]})
        row = r.json()["data"][0]
        self.assertIn("id", row)
        self.assertIn("title", row)
        self.assertNotIn("status", row)

    def test_unknown_fields_400(self):
        r = self._post({"resource": "tasks", "fields": ["bogus"]})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"], "unknown_fields")

    # ── Determinism ───────────────────────────────────────────────────

    def test_query_hash_stable_for_same_query(self):
        a = self._post({"resource": "tasks", "filter": {"status": "open"}}).json()["meta"]["query_hash"]
        b = self._post({"resource": "tasks", "filter": {"status": "open"}}).json()["meta"]["query_hash"]
        self.assertEqual(a, b)

    def test_query_hash_changes_when_filter_changes(self):
        a = self._post({"resource": "tasks", "filter": {"status": "open"}}).json()["meta"]["query_hash"]
        b = self._post({"resource": "tasks", "filter": {"status": "done"}}).json()["meta"]["query_hash"]
        self.assertNotEqual(a, b)
