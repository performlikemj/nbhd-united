"""Tests for the throttled, exact-ID retirement command."""

from __future__ import annotations

import logging
from io import StringIO
from unittest.mock import call, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from apps.cron.gateway_client import GatewayError
from apps.cron.share_observer import ShareJSONInvalid, ShareObservation
from apps.tenants.models import Tenant, User


def _job(job_id: str, name: str = "_fuel:welcome") -> dict:
    return {
        "id": job_id,
        "name": name,
        "schedule": {"kind": "cron", "expr": "25 23 25 4 *"},
    }


class RetireQuarantinedCommandTests(TestCase):
    def setUp(self):
        user = User.objects.create_user(username="share-retirement", password="x")
        self.tenant = Tenant.objects.create(
            user=user,
            status=Tenant.Status.ACTIVE,
            container_id="oc-share-retirement",
            container_fqdn="oc-share-retirement.internal",
        )

    def _observation(self, jobs: list[dict]) -> ShareObservation:
        now = timezone.now()
        return ShareObservation(
            tenant_id=str(self.tenant.id),
            jobs=tuple(jobs),
            jobs_state={job["id"]: {} for job in jobs},
            count=len(jobs),
            digest="a" * 64,
            mtime=now,
            observed_at=now,
        )

    def test_missing_tenant_argument_is_refused(self):
        with self.assertRaises(CommandError):
            call_command(
                "retire_quarantined",
                "--name",
                "_fuel:welcome",
                "--bucket",
                "duplicate",
            )

    def test_missing_name_argument_is_refused(self):
        with self.assertRaises(CommandError):
            call_command(
                "retire_quarantined",
                "--tenant",
                str(self.tenant.id),
                "--bucket",
                "duplicate",
            )

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    @patch("apps.cron.share_observer.observe_share")
    def test_without_confirm_is_dry_run_and_lists_exact_ids(self, mock_observe, mock_gateway):
        mock_observe.return_value = self._observation([_job("id-1"), _job("id-2"), _job("other", "Other")])
        stdout = StringIO()

        call_command(
            "retire_quarantined",
            "--tenant",
            str(self.tenant.id),
            "--name",
            "_fuel:welcome",
            "--bucket",
            "duplicate",
            stdout=stdout,
        )

        output = stdout.getvalue()
        self.assertIn("WOULD_REMOVE", output)
        self.assertIn("jobId=id-1", output)
        self.assertIn("jobId=id-2", output)
        self.assertNotIn("jobId=other", output)
        self.assertIn("matched=2 removed=0 failed=0 remaining=2", output)
        mock_gateway.assert_not_called()

    @patch("apps.cron.management.commands.retire_quarantined.time.sleep")
    @patch("apps.cron.gateway_client.invoke_gateway_tool", return_value={})
    @patch("apps.cron.share_observer.observe_share")
    def test_confirm_respects_limit_and_gateway_receives_job_ids_only(
        self,
        mock_observe,
        mock_gateway,
        mock_sleep,
    ):
        mock_observe.return_value = self._observation(
            [_job("id-1"), _job("id-2"), _job("id-3"), _job("other", "Other")]
        )
        stdout = StringIO()

        call_command(
            "retire_quarantined",
            "--tenant",
            str(self.tenant.id),
            "--name",
            "_fuel:welcome",
            "--bucket",
            "duplicate",
            "--limit",
            "2",
            "--confirm",
            stdout=stdout,
        )

        self.assertEqual(
            mock_gateway.call_args_list,
            [
                call(
                    self.tenant,
                    "cron.remove",
                    {"jobId": "id-1"},
                    error_log_level=logging.ERROR,
                ),
                call(
                    self.tenant,
                    "cron.remove",
                    {"jobId": "id-2"},
                    error_log_level=logging.ERROR,
                ),
            ],
        )
        for gateway_call in mock_gateway.call_args_list:
            self.assertEqual(set(gateway_call.args[2]), {"jobId"})
            self.assertNotIn("name", gateway_call.args[2])
        mock_sleep.assert_called_once()
        self.assertIn("matched=3 removed=2 failed=0 remaining=1", stdout.getvalue())

    @patch("apps.cron.management.commands.retire_quarantined.time.sleep")
    @patch(
        "apps.cron.gateway_client.invoke_gateway_tool",
        side_effect=[{}, GatewayError("simulated removal failure"), {}],
    )
    @patch("apps.cron.share_observer.observe_share")
    def test_individual_remove_failure_is_logged_and_command_continues(
        self,
        mock_observe,
        mock_gateway,
        mock_sleep,
    ):
        mock_observe.return_value = self._observation([_job("id-1"), _job("id-2"), _job("id-3")])
        stdout = StringIO()
        stderr = StringIO()

        call_command(
            "retire_quarantined",
            "--tenant",
            str(self.tenant.id),
            "--name",
            "_fuel:welcome",
            "--bucket",
            "expired",
            "--confirm",
            stdout=stdout,
            stderr=stderr,
        )

        self.assertEqual(mock_gateway.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)
        self.assertIn("jobId=id-2", stderr.getvalue())
        self.assertIn("matched=3 removed=2 failed=1 remaining=1", stdout.getvalue())

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    @patch(
        "apps.cron.share_observer.observe_share",
        side_effect=ShareJSONInvalid("unparseable cron/jobs.json"),
    )
    def test_any_observation_guard_refuses_retirement(self, mock_observe, mock_gateway):
        with self.assertRaisesMessage(CommandError, "share observation refused retirement"):
            call_command(
                "retire_quarantined",
                "--tenant",
                str(self.tenant.id),
                "--name",
                "_fuel:welcome",
                "--bucket",
                "duplicate",
                "--confirm",
            )
        mock_observe.assert_called_once_with(self.tenant)
        mock_gateway.assert_not_called()

    def test_limit_above_hard_max_is_refused(self):
        with self.assertRaisesMessage(CommandError, "--limit must be between 1 and 100"):
            call_command(
                "retire_quarantined",
                "--tenant",
                str(self.tenant.id),
                "--name",
                "_fuel:welcome",
                "--bucket",
                "duplicate",
                "--limit",
                "101",
                "--confirm",
            )
