"""One-shot ("at") cron lifecycle — the retirement sweep + the conditional name constraint.

THE BUG THESE PIN. OpenClaw deletes an at-kind job from its own store when it fires
(``deleteAfterRun``), but nothing tells Django — there is NO container→control-plane
feedback path for cron fires. So the Postgres row stayed ``enabled=True`` forever, and
because ``(tenant, name)`` uniqueness was UNCONDITIONAL, the name stayed squatted:

    "remind me at 3pm to call Mom"  → works
    "remind me at 3pm to call Mom"  → 409 CronNameConflictError. Forever.

Observed in production: CronJob 162 ("Water reminder at 3pm") fired in-container on
2026-07-12 and was still ``enabled=True`` two days later, holding its name.

Two halves of the fix, both exercised here:
  * uniqueness is now scoped to ``enabled=True`` (apps/cron/models.py) — a retired row
    releases its name;
  * ``expire_finished_at_crons_task`` (apps/cron/tasks.py) is what retires it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

from django.db.utils import IntegrityError
from django.test import TestCase
from django.utils import timezone

from apps.cron.models import CronCreationPath, CronJob, CronJobSource, CronPattern
from apps.cron.services import create_typed_cron
from apps.cron.tasks import (
    AT_CRON_GRACE,
    INTERNAL_AT_CRON_RETENTION,
    cleanup_internal_crons_task,
    expire_finished_at_crons_task,
)
from apps.tenants.models import Tenant, User


def _make_tenant(username: str) -> Tenant:
    user = User.objects.create_user(username=username, password="x")
    return Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE,
        container_id="oc-test",
        container_fqdn="oc-test.internal.azurecontainerapps.io",
        postgres_cron_canonical=False,  # off → no QStash regen enqueue
    )


def _at(dt: datetime) -> dict:
    return {"kind": "at", "at": dt.isoformat().replace("+00:00", "Z")}


class ConditionalNameConstraintTests(TestCase):
    """The constraint releases a name once the row is retired — but only then."""

    def setUp(self):
        self.tenant = _make_tenant("cron-constraint")

    def test_retired_and_active_row_share_a_name(self):
        CronJob.objects.create(tenant=self.tenant, name="Call Mom", enabled=False, data={})
        CronJob.objects.create(tenant=self.tenant, name="Call Mom", enabled=True, data={})
        self.assertEqual(CronJob.objects.filter(tenant=self.tenant, name="Call Mom").count(), 2)

    def test_two_ACTIVE_rows_with_one_name_still_rejected(self):
        CronJob.objects.create(tenant=self.tenant, name="Call Mom", enabled=True, data={})
        with self.assertRaises(IntegrityError) as ctx:
            CronJob.objects.create(tenant=self.tenant, name="Call Mom", enabled=True, data={})
        # The constraint NAME is load-bearing: create_typed_cron / create_freeform_cron
        # match on this substring in the IntegrityError text to raise
        # CronNameConflictError. A rename here silently turns a clean 409 into a 500.
        self.assertIn("cron_unique_tenant_name", str(ctx.exception))


class ReminderReuseRegressionTests(TestCase):
    """THE user story. This test FAILS on main."""

    def setUp(self):
        self.tenant = _make_tenant("cron-reuse")

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_same_reminder_can_be_set_again_after_the_first_one_fires(self, mock_gateway):
        mock_gateway.return_value = {"ok": True}

        first = create_typed_cron(
            tenant=self.tenant,
            pattern=CronPattern.PURE_REMINDER,
            typed_payload={"text": "Call Mom"},
            name="Call Mom",
            schedule=_at(timezone.now() - timedelta(hours=3)),
        )
        self.assertTrue(first.enabled)
        self.assertFalse(first.managed)  # at-kind ⇒ unmanaged, container-owned

        # The container fired it and deleted its own copy. Nothing told Django.
        # The sweep is the only thing that retires the row.
        expire_finished_at_crons_task()
        first.refresh_from_db()
        self.assertFalse(first.enabled)

        # The user asks for the same reminder tomorrow. On main this raised
        # CronNameConflictError (→ HTTP 409) and did so forever after.
        second = create_typed_cron(
            tenant=self.tenant,
            pattern=CronPattern.PURE_REMINDER,
            typed_payload={"text": "Call Mom"},
            name="Call Mom",
            schedule=_at(timezone.now() + timedelta(hours=2)),
        )
        self.assertTrue(second.enabled)
        self.assertNotEqual(second.id, first.id)


class ExpireFinishedAtCronsSweepTests(TestCase):
    def setUp(self):
        self.tenant = _make_tenant("cron-sweep")

    def _row(self, name, *, schedule, managed=False, enabled=True):
        return CronJob.objects.create(
            tenant=self.tenant,
            name=name,
            enabled=enabled,
            managed=managed,
            data={"schedule": schedule} if schedule is not None else {},
        )

    def test_spent_at_cron_is_retired(self):
        row = self._row("spent", schedule=_at(timezone.now() - timedelta(hours=3)))
        result = expire_finished_at_crons_task()
        row.refresh_from_db()
        self.assertFalse(row.enabled)
        self.assertEqual(result["expired"], 1)
        self.assertIn(row.id, result["ids"])

    def test_future_at_cron_is_untouched(self):
        row = self._row("future", schedule=_at(timezone.now() + timedelta(days=1)))
        expire_finished_at_crons_task()
        row.refresh_from_db()
        self.assertTrue(row.enabled)

    def test_within_grace_is_untouched(self):
        # Fired, but only just — a container hibernated across the fire time may still
        # run it late on wake, so the grace window leaves it alone.
        row = self._row("just-fired", schedule=_at(timezone.now() - (AT_CRON_GRACE / 2)))
        expire_finished_at_crons_task()
        row.refresh_from_db()
        self.assertTrue(row.enabled)

    def test_recurring_managed_cron_is_never_touched(self):
        # A daily briefing has no "at" — and is managed by the reconciler. The sweep
        # must not go anywhere near it, whatever its dates say. ``kind`` is the only
        # discriminator, so this must hold without any managed-flag filtering.
        row = self._row(
            "daily-briefing",
            schedule={"kind": "cron", "expr": "0 8 * * *", "tz": "Asia/Tokyo"},
            managed=True,
        )
        expire_finished_at_crons_task()
        row.refresh_from_db()
        self.assertTrue(row.enabled)

    def test_a_MIRRORED_at_cron_is_retired_too(self):
        """``managed=True`` must NOT exempt a spent one-shot.

        ``upsert_jobs_to_cache`` (apps/cron/cache.py) mirrors gateway jobs into Postgres
        without passing ``managed``, so it takes the model default of True. A one-shot
        that a console open mirrored is therefore ``managed=True`` — and a sweep that
        filtered on ``managed=False`` would skip it forever, leaving it squatting its
        name with no retirement path. That is the original bug, reintroduced through a
        side door.
        """
        row = self._row(
            "mirrored-one-shot",
            schedule=_at(timezone.now() - timedelta(hours=3)),
            managed=True,
        )
        expire_finished_at_crons_task()
        row.refresh_from_db()
        self.assertFalse(row.enabled, "a mirrored (managed=True) spent at-cron kept squatting its name")

    def test_naive_at_timestamp_is_handled(self):
        # A schedule written without a timezone suffix must not raise on comparison and
        # must still be retired. Treated as UTC.
        row = self._row("naive", schedule={"kind": "at", "at": "2026-07-12T15:00:00"})
        expire_finished_at_crons_task()
        row.refresh_from_db()
        self.assertFalse(row.enabled)

    def test_retirement_stamps_updated_at(self):
        # .update() bypasses auto_now, so the task sets it explicitly — "when was this
        # retired?" is the first forensic question.
        row = self._row("spent", schedule=_at(timezone.now() - timedelta(hours=3)))
        before = row.updated_at
        expire_finished_at_crons_task()
        row.refresh_from_db()
        self.assertFalse(row.enabled)
        self.assertGreater(row.updated_at, before)

    def test_malformed_schedule_is_skipped_not_crashed(self):
        # A broken writer must stay VISIBLE (warning log), never be silently retired
        # and never take the sweep down with it.
        bad_at = self._row("bad-at", schedule={"kind": "at", "at": "not-a-timestamp"})
        no_at = self._row("no-at", schedule={"kind": "at"})
        no_schedule = self._row("no-schedule", schedule=None)

        result = expire_finished_at_crons_task()  # must not raise

        for row in (bad_at, no_at, no_schedule):
            row.refresh_from_db()
            self.assertTrue(row.enabled, f"{row.name} was retired on an unparseable schedule")
        self.assertEqual(result["expired"], 0)

    def test_sweep_is_idempotent(self):
        self._row("spent", schedule=_at(timezone.now() - timedelta(hours=3)))
        self.assertEqual(expire_finished_at_crons_task()["expired"], 1)
        self.assertEqual(expire_finished_at_crons_task()["expired"], 0)

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_sweep_does_not_re_render_data(self, mock_gateway):
        """Pins the bulk ``.update()`` choice.

        A per-row ``.save()`` would fire the pre_save contract-baking signal and
        re-render ``data`` on a dead row, and post_save would schedule a push to a
        container that already deleted its copy. Assert the payload is byte-identical
        across the sweep.
        """
        mock_gateway.return_value = {"ok": True}
        row = create_typed_cron(
            tenant=self.tenant,
            pattern=CronPattern.PURE_REMINDER,
            typed_payload={"text": "hydrate"},
            name="hydrate",
            schedule=_at(timezone.now() - timedelta(hours=3)),
        )
        row.refresh_from_db()
        data_before = row.data
        payload_before = row.typed_payload

        expire_finished_at_crons_task()

        row.refresh_from_db()
        self.assertFalse(row.enabled)
        self.assertEqual(row.data, data_before)
        self.assertEqual(row.typed_payload, payload_before)
        self.assertEqual(row.creation_path, CronCreationPath.TYPED)


class TaskMapWiringTests(TestCase):
    def test_expire_finished_at_crons_resolves(self):
        from apps.cron.views import TASK_MAP

        self.assertIn("expire_finished_at_crons", TASK_MAP)
        module_path, func_name = TASK_MAP["expire_finished_at_crons"].rsplit(".", 1)
        from importlib import import_module

        func = import_module(module_path)
        self.assertTrue(callable(getattr(func, func_name)))

    def test_cleanup_internal_crons_resolves(self):
        from importlib import import_module

        from apps.cron.views import TASK_MAP

        self.assertIn("cleanup_internal_crons", TASK_MAP)
        module_path, func_name = TASK_MAP["cleanup_internal_crons"].rsplit(".", 1)
        func = import_module(module_path)
        self.assertTrue(callable(getattr(func, func_name)))


class CleanupInternalCronsSweepTests(TestCase):
    def setUp(self):
        self.tenant = _make_tenant("internal-cron-cleanup")

    def _row(
        self,
        name: str,
        *,
        source: str,
        enabled: bool = False,
        schedule_kind: str = "at",
        age: timedelta = INTERNAL_AT_CRON_RETENTION + timedelta(hours=1),
        pattern: str | None = None,
    ) -> CronJob:
        row = CronJob.objects.create(
            tenant=self.tenant,
            name=name,
            source=source,
            enabled=enabled,
            managed=False,
            pattern=pattern,
            data={
                "schedule": (
                    {"kind": "at", "at": "2026-08-01T00:00:00Z"}
                    if schedule_kind == "at"
                    else {"kind": "cron", "expr": "0 7 * * *", "tz": "UTC"}
                )
            },
        )
        CronJob.objects.filter(pk=row.pk).update(updated_at=timezone.now() - age)
        row.refresh_from_db()
        return row

    def test_deletes_stale_disabled_workout_congrats_row(self):
        row = self._row(
            "_congrats-162769ab",
            source=CronJobSource.SYSTEM,
            pattern=CronPattern.WORKOUT_CONGRATS,
        )

        result = cleanup_internal_crons_task()

        self.assertEqual(result, {"deleted": 1, "ids": [row.id]})
        self.assertFalse(CronJob.objects.filter(pk=row.pk).exists())

    def test_deletes_other_stale_disabled_internal_at_row(self):
        row = self._row("_sync:finished", source=CronJobSource.AGENT)

        cleanup_internal_crons_task()

        self.assertFalse(CronJob.objects.filter(pk=row.pk).exists())

    def test_preserves_enabled_user_seeded_recent_and_recurring_rows(self):
        enabled = self._row("_congrats-enabled", source=CronJobSource.SYSTEM, enabled=True)
        user = self._row("_x", source=CronJobSource.USER)
        seeded = self._row("Morning Briefing", source=CronJobSource.SYSTEM)
        recent = self._row(
            "_congrats-recent",
            source=CronJobSource.SYSTEM,
            age=INTERNAL_AT_CRON_RETENTION - timedelta(minutes=1),
        )
        recurring = self._row("_fuel:recurring", source=CronJobSource.FUEL_SESSION, schedule_kind="cron")

        result = cleanup_internal_crons_task()

        self.assertEqual(result, {"deleted": 0, "ids": []})
        for row in (enabled, user, seeded, recent, recurring):
            self.assertTrue(CronJob.objects.filter(pk=row.pk).exists(), row.name)
