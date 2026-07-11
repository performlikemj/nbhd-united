"""Tests for the typed-cron service layer + the pre_save data-derivation signal.

Covers:
  - create_typed_cron validates payload, writes the row, derives data via signal
  - data drift: editing typed_payload regenerates data; freeform data is untouched
  - one-off (at-kind) crons set managed=False and trigger immediate gateway push
  - freeform creation requires user_confirmed_at; CHECK constraint enforces it
  - name collisions surface CronNameConflictError
"""

from __future__ import annotations

import json
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

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_at_cron_bookkeeping_failure_does_not_raise(self, mock_invoke):
        # The gateway ACCEPTS the job, but stamping gateway_job_id wedges on an idle
        # connection. That is a post-delivery bookkeeping blip — it must NOT propagate as
        # a push failure (callers roll back their own state on failure, and rolling back a
        # LIVE cron would drop a delivered message). The boundary is explicit: this raises
        # only for a failure BEFORE the gateway accepted the job.
        from django.db import OperationalError

        mock_invoke.return_value = {"details": {"id": "gw-id-xyz"}}
        real_save = CronJob.save

        def _flaky_save(cron_self, *args, **kwargs):
            if kwargs.get("update_fields") == ["gateway_job_id"]:
                raise OperationalError("idle connection wedge")
            return real_save(cron_self, *args, **kwargs)

        with patch.object(CronJob, "save", autospec=True, side_effect=_flaky_save):
            cron = create_typed_cron(
                tenant=self.tenant,
                pattern=CronPattern.PURE_REMINDER,
                typed_payload={"text": "x"},
                name="bookkeeping-blip",
                schedule=_ONE_OFF,
            )
        # No raise → the row survives, is unmanaged, and just misses the gateway_job_id.
        cron.refresh_from_db()
        self.assertFalse(cron.managed)
        self.assertEqual(cron.gateway_job_id, "")
        mock_invoke.assert_called_once()


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


class TypedCronContractBakingTests(TestCase):
    """The pre_save signal bakes get_outbound_contract() into data["description"]
    for every typed pattern; freeform/legacy rows carry no contract."""

    def setUp(self):
        self.tenant = _make_tenant()

    def test_pure_reminder_bakes_contains_rewrite_contract(self):
        cron = create_typed_cron(
            tenant=self.tenant,
            pattern=CronPattern.PURE_REMINDER,
            typed_payload={"text": "Drink water"},
            name="hydrate",
            schedule=_RECURRING,
        )
        cron.refresh_from_db()
        self.assertTrue(cron.data["description"].startswith("nbhd.v1 "))
        contract = json.loads(cron.data["description"][len("nbhd.v1 ") :])
        self.assertEqual(contract["v"], 1)
        self.assertEqual(contract["pattern"], "pure_reminder")
        self.assertEqual(contract["check"], {"kind": "contains", "text": "Drink water"})
        self.assertEqual(contract["on_fail"], {"action": "rewrite", "content": "Drink water"})

    def test_domain_summary_bakes_marker_revise_then_allow_contract(self):
        cron = create_typed_cron(
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
        cron.refresh_from_db()
        contract = json.loads(cron.data["description"][len("nbhd.v1 ") :])
        self.assertEqual(contract["check"], {"kind": "marker", "marker": "[block: task_summary]"})
        self.assertEqual(contract["on_fail"], {"action": "revise_then_allow", "max_revisions": 1})

    def test_freeform_cron_has_no_baked_contract(self):
        cron = create_freeform_cron(
            tenant=self.tenant,
            name="freeform-desc-test",
            data={
                "schedule": _RECURRING,
                "payload": {"kind": "agentTurn", "message": "untouched"},
            },
            user_confirmed_at=datetime(2026, 5, 27, 12, 0, tzinfo=UTC),
        )
        cron.refresh_from_db()
        self.assertNotIn("description", cron.data)

    def test_editing_typed_payload_rebakes_contract(self):
        cron = create_typed_cron(
            tenant=self.tenant,
            pattern=CronPattern.PURE_REMINDER,
            typed_payload={"text": "old text"},
            name="rebake",
            schedule=_RECURRING,
        )
        cron.refresh_from_db()
        cron.typed_payload = {"text": "new text"}
        cron.save()
        cron.refresh_from_db()
        contract = json.loads(cron.data["description"][len("nbhd.v1 ") :])
        self.assertEqual(contract["check"]["text"], "new text")

    def test_row_to_cron_dict_passes_description_through(self):
        """``_row_to_cron_dict`` (apps/orchestrator/cron_reconcile.py) strips only
        gateway-internal bookkeeping fields — the baked contract in
        ``data["description"]`` must survive into the gateway-shape dict
        unchanged, or the plugin never sees it at fire time."""
        from apps.orchestrator.cron_reconcile import _row_to_cron_dict

        cron = create_typed_cron(
            tenant=self.tenant,
            pattern=CronPattern.PURE_REMINDER,
            typed_payload={"text": "Drink water"},
            name="hydrate-passthrough",
            schedule=_RECURRING,
        )
        cron.refresh_from_db()
        job = _row_to_cron_dict(cron)
        self.assertEqual(job["description"], cron.data["description"])
        for stripped in ("id", "jobId", "createdAt", "state", "createdAtMs", "updatedAtMs"):
            self.assertNotIn(stripped, job)
