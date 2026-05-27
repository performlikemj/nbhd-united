"""Tests for the typed-cron service layer + the pre_save data-derivation signal.

Covers:
  - create_typed_cron validates payload, writes the row, derives data via signal
  - data drift: editing typed_payload regenerates data; freeform data is untouched
  - one-off (at-kind) crons set managed=False and trigger immediate gateway push
  - freeform creation requires user_confirmed_at; CHECK constraint enforces it
  - name collisions surface CronNameConflictError
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

from django.db.utils import IntegrityError
from django.test import TestCase

from apps.cron.models import CronCreationPath, CronJob, CronJobSource, CronPattern
from apps.cron.services import (
    CronNameConflictError,
    TypedCronError,
    create_freeform_cron,
    create_typed_cron,
    fetch_cron_pattern_context,
    validate_typed_cron_outbound,
)
from apps.tenants.models import Tenant, User

_RECURRING = {"kind": "cron", "expr": "0 8 * * 2", "tz": "Asia/Tokyo"}
_ONE_OFF = {"kind": "at", "at": "2099-01-01T15:00:00+09:00"}


def _make_tenant():
    user = User.objects.create_user(username="typedcrontest", password="x")
    return Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE,
        container_id="oc-test",
        container_fqdn="oc-test.internal.azurecontainerapps.io",
        postgres_cron_canonical=False,  # off → no QStash regen enqueue
    )


class CreateTypedCronTests(TestCase):
    def setUp(self):
        self.tenant = _make_tenant()

    def test_creates_pure_reminder_and_signal_derives_data(self):
        cron = create_typed_cron(
            tenant=self.tenant,
            pattern=CronPattern.PURE_REMINDER,
            typed_payload={"text": "Take out trash"},
            name="trash-tuesday",
            schedule=_RECURRING,
        )
        cron.refresh_from_db()
        self.assertEqual(cron.pattern, "pure_reminder")
        self.assertEqual(cron.creation_path, CronCreationPath.TYPED)
        self.assertEqual(cron.typed_payload, {"text": "Take out trash"})
        # Signal-derived data:
        self.assertEqual(cron.data["sessionTarget"], "isolated")
        self.assertEqual(cron.data["payload"]["toolsAllow"], ["nbhd_send_to_user"])
        self.assertIn("Take out trash", cron.data["payload"]["message"])
        self.assertEqual(cron.data["schedule"], _RECURRING)
        self.assertTrue(cron.managed)  # recurring → managed

    def test_recreating_with_same_typed_payload_does_not_churn_data(self):
        cron = create_typed_cron(
            tenant=self.tenant,
            pattern=CronPattern.PURE_REMINDER,
            typed_payload={"text": "x"},
            name="a",
            schedule=_RECURRING,
        )
        cron.refresh_from_db()
        first_message = cron.data["payload"]["message"]
        # A re-save with identical fields should NOT regenerate (signal
        # short-circuits when nothing changed).
        cron.save()
        cron.refresh_from_db()
        self.assertEqual(cron.data["payload"]["message"], first_message)

    def test_editing_typed_payload_regenerates_data(self):
        cron = create_typed_cron(
            tenant=self.tenant,
            pattern=CronPattern.PURE_REMINDER,
            typed_payload={"text": "old text"},
            name="a",
            schedule=_RECURRING,
        )
        cron.refresh_from_db()
        cron.typed_payload = {"text": "new text"}
        cron.save()
        cron.refresh_from_db()
        self.assertIn("new text", cron.data["payload"]["message"])
        self.assertNotIn("old text", cron.data["payload"]["message"])

    def test_invalid_pattern_raises(self):
        with self.assertRaises(TypedCronError) as cm:
            create_typed_cron(
                tenant=self.tenant,
                pattern="bogus",
                typed_payload={},
                name="a",
                schedule=_RECURRING,
            )
        self.assertEqual(cm.exception.code, "invalid_pattern")

    def test_invalid_schedule_kind_raises(self):
        with self.assertRaises(TypedCronError) as cm:
            create_typed_cron(
                tenant=self.tenant,
                pattern=CronPattern.PURE_REMINDER,
                typed_payload={"text": "x"},
                name="a",
                schedule={"kind": "monthly"},
            )
        self.assertEqual(cm.exception.code, "invalid_schedule")

    def test_name_collision_raises_conflict(self):
        create_typed_cron(
            tenant=self.tenant,
            pattern=CronPattern.PURE_REMINDER,
            typed_payload={"text": "x"},
            name="dup",
            schedule=_RECURRING,
        )
        with self.assertRaises(CronNameConflictError):
            create_typed_cron(
                tenant=self.tenant,
                pattern=CronPattern.PURE_REMINDER,
                typed_payload={"text": "y"},
                name="dup",
                schedule=_RECURRING,
            )

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_at_kind_cron_pushes_immediately_and_marks_unmanaged(self, mock_invoke):
        mock_invoke.return_value = {"details": {"id": "gw-id-123"}}
        cron = create_typed_cron(
            tenant=self.tenant,
            pattern=CronPattern.PURE_REMINDER,
            typed_payload={"text": "Pick up dry cleaning"},
            name="oneshot",
            schedule=_ONE_OFF,
        )
        cron.refresh_from_db()
        self.assertFalse(cron.managed)
        self.assertEqual(cron.gateway_job_id, "gw-id-123")
        mock_invoke.assert_called_once()
        call_args = mock_invoke.call_args
        # First positional is the tool name
        self.assertEqual(call_args.args[1], "cron.add")
        job = call_args.args[2]["job"]
        self.assertEqual(job["schedule"], _ONE_OFF)


class FreeformCronTests(TestCase):
    def setUp(self):
        self.tenant = _make_tenant()
        self.confirmed_at = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)

    def test_creates_freeform_with_confirmation(self):
        cron = create_freeform_cron(
            tenant=self.tenant,
            name="freeform-one",
            data={
                "name": "freeform-one",
                "schedule": _RECURRING,
                "sessionTarget": "isolated",
                "payload": {"kind": "agentTurn", "message": "Do whatever"},
                "delivery": {"mode": "none"},
                "enabled": True,
            },
            user_confirmed_at=self.confirmed_at,
        )
        cron.refresh_from_db()
        self.assertEqual(cron.creation_path, CronCreationPath.FREEFORM)
        self.assertIsNone(cron.pattern)
        self.assertEqual(cron.user_confirmed_at, self.confirmed_at)

    def test_freeform_without_confirmation_raises(self):
        with self.assertRaises(TypedCronError) as cm:
            create_freeform_cron(
                tenant=self.tenant,
                name="x",
                data={"schedule": _RECURRING},
                user_confirmed_at=None,
            )
        self.assertEqual(cm.exception.code, "missing_confirmation")

    def test_signal_does_not_overwrite_freeform_data(self):
        cron = create_freeform_cron(
            tenant=self.tenant,
            name="ff",
            data={
                "schedule": _RECURRING,
                "payload": {"kind": "agentTurn", "message": "untouched"},
            },
            user_confirmed_at=self.confirmed_at,
        )
        cron.refresh_from_db()
        # Re-save shouldn't touch data because creation_path != TYPED.
        cron.save()
        cron.refresh_from_db()
        self.assertEqual(cron.data["payload"]["message"], "untouched")

    def test_db_check_constraint_blocks_freeform_without_confirmation(self):
        # Bypassing the service to hit the DB constraint directly.
        with self.assertRaises(IntegrityError):
            CronJob.objects.create(
                tenant=self.tenant,
                name="direct-bypass",
                creation_path=CronCreationPath.FREEFORM,
                user_confirmed_at=None,
                data={"schedule": _RECURRING},
                source=CronJobSource.USER,
            )

    def test_db_check_constraint_blocks_typed_without_pattern(self):
        with self.assertRaises(IntegrityError):
            CronJob.objects.create(
                tenant=self.tenant,
                name="typed-no-pattern",
                creation_path=CronCreationPath.TYPED,
                pattern=None,
                data={"schedule": _RECURRING},
                source=CronJobSource.USER,
            )


class PatternContextLookupTests(TestCase):
    def setUp(self):
        self.tenant = _make_tenant()
        self.cron = create_typed_cron(
            tenant=self.tenant,
            pattern=CronPattern.DOMAIN_SUMMARY,
            typed_payload={
                "query_tool": "nbhd_task_list",
                "render_block": "task_summary",
                "query_args": {"status": "open"},
            },
            name="weekly-task-rollup",
            schedule=_RECURRING,
        )

    def test_returns_pattern_and_payload(self):
        ctx = fetch_cron_pattern_context(self.tenant.id, "weekly-task-rollup")
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx["pattern"], "domain_summary")
        self.assertEqual(ctx["typed_payload"]["query_tool"], "nbhd_task_list")
        self.assertIn("domain_summary", ctx["prompt_injection"])

    def test_returns_none_for_unknown_cron(self):
        self.assertIsNone(fetch_cron_pattern_context(self.tenant.id, "no-such-cron"))


class ValidateTypedCronOutboundTests(TestCase):
    def setUp(self):
        self.tenant = _make_tenant()
        create_typed_cron(
            tenant=self.tenant,
            pattern=CronPattern.PURE_REMINDER,
            typed_payload={"text": "Drink water"},
            name="hydrate",
            schedule=_RECURRING,
        )

    def test_passes_verbatim(self):
        result = validate_typed_cron_outbound(
            tenant_id=self.tenant.id,
            cron_name="hydrate",
            content="Drink water",
        )
        self.assertTrue(result["ok"])

    def test_rejects_drift_and_returns_fallback(self):
        result = validate_typed_cron_outbound(
            tenant_id=self.tenant.id,
            cron_name="hydrate",
            content="You should consider hydration",
        )
        self.assertFalse(result["ok"])
        self.assertIn("verbatim", (result.get("reason") or "").lower())
        self.assertIn("hydrate", result.get("fallback_content", ""))

    def test_unknown_cron_passes_through(self):
        result = validate_typed_cron_outbound(
            tenant_id=self.tenant.id,
            cron_name="no-such",
            content="x",
        )
        self.assertTrue(result["ok"])
