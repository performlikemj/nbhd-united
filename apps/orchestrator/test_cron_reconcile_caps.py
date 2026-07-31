"""Safety-cap and fail-closed coverage for the tenant cron reconciler."""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from apps.cron.models import CronJob
from apps.orchestrator.cron_reconcile import (
    MAX_OPS_PER_PASS,
    MAX_REMOVES_PER_PASS,
    regenerate_tenant_crons,
)
from apps.platform_logs.models import PlatformIssueLog
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


def _desired_row(tenant: Tenant, name: str) -> CronJob:
    return CronJob(
        tenant=tenant,
        name=name,
        managed=True,
        data={
            "name": name,
            "schedule": {"kind": "cron", "expr": "0 7 * * *", "tz": "UTC"},
            "sessionTarget": "isolated",
            "payload": {"kind": "agentTurn", "message": name},
            "enabled": True,
        },
    )


def _gateway_job(name: str) -> dict:
    return {
        "id": f"id-{name}",
        "name": name,
        "schedule": {"kind": "cron", "expr": "0 7 * * *", "tz": "UTC"},
        "sessionTarget": "isolated",
        "payload": {"kind": "agentTurn", "message": name},
        "enabled": True,
    }


class CronReconcileObservationTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Observation Test", telegram_chat_id=864209753)
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_id = "oc-observation-test"
        self.tenant.container_fqdn = "oc-observation-test.internal"
        self.tenant.postgres_cron_canonical = True
        self.tenant.save()

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_malformed_observation_aborts_without_mutations_and_counts_error(self, mock_invoke):
        CronJob.objects.bulk_create([_desired_row(self.tenant, "Morning Briefing")])
        malformed_responses = [
            None,
            "not-json",
            {},
            {"details": {}},
            {"jobs": "not-a-list"},
            {"jobs": [None]},
            {"jobs": [{"id": "missing-name"}]},
        ]

        for response in malformed_responses:
            with self.subTest(response=response):
                mock_invoke.reset_mock()
                mock_invoke.side_effect = None
                mock_invoke.return_value = response

                with self.assertLogs("apps.orchestrator.cron_reconcile", level="ERROR") as logs:
                    summary = regenerate_tenant_crons(self.tenant)

                self.assertEqual(summary["errors"], 1)
                self.assertEqual(summary["added"], 0)
                self.assertEqual(summary["removed"], 0)
                self.assertEqual(summary["recreated"], 0)
                self.assertFalse(summary["capped"])
                mutation_calls = [
                    call for call in mock_invoke.call_args_list if call.args[1] in {"cron.add", "cron.remove"}
                ]
                self.assertEqual(mutation_calls, [])
                self.assertTrue(any("invalid cron.list observation" in message for message in logs.output))

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_valid_empty_observation_on_zero_job_tenant_is_normal(self, mock_invoke):
        mock_invoke.return_value = {"details": {"jobs": []}}

        summary = regenerate_tenant_crons(self.tenant)

        self.assertEqual(summary["errors"], 0)
        self.assertEqual(summary["added"], 0)
        self.assertEqual(summary["removed"], 0)
        self.assertEqual(summary["unchanged"], 0)
        self.assertFalse(summary["capped"])
        mock_invoke.assert_called_once_with(self.tenant, "cron.list", {"includeDisabled": True})


class CronReconcileOperationCapTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Operation Cap Test", telegram_chat_id=975310864)
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_id = "oc-operation-cap-test"
        self.tenant.container_fqdn = "oc-operation-cap-test.internal"
        self.tenant.postgres_cron_canonical = True
        self.tenant.save()

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_total_cap_adds_before_removes_and_second_pass_drains_remainder(self, mock_invoke):
        desired_names = [f"desired-{i:02d}" for i in range(20)]
        CronJob.objects.bulk_create([_desired_row(self.tenant, name) for name in reversed(desired_names)])
        gateway_jobs = [_gateway_job(f"stale-{i:02d}") for i in reversed(range(10))]
        mutation_order: list[tuple[str, str]] = []

        def _invoke(tenant, tool, args):
            if tool == "cron.list":
                return {"jobs": [dict(job) for job in gateway_jobs]}
            if tool == "cron.add":
                job = dict(args["job"])
                job["id"] = f"id-{job['name']}"
                gateway_jobs.append(job)
                mutation_order.append(("add", job["name"]))
            if tool == "cron.remove":
                job = next(job for job in gateway_jobs if job["id"] == args["jobId"])
                gateway_jobs.remove(job)
                mutation_order.append(("remove", job["name"]))
            return {"ok": True}

        mock_invoke.side_effect = _invoke
        with self.assertLogs("apps.orchestrator.cron_reconcile", level="WARNING") as logs:
            first = regenerate_tenant_crons(self.tenant)

        self.assertTrue(first["capped"])
        self.assertEqual(first["added"], 20)
        self.assertEqual(first["removed"], MAX_OPS_PER_PASS - 20)
        self.assertEqual(len(mutation_order), MAX_OPS_PER_PASS)
        self.assertEqual(
            mutation_order,
            [
                *[("add", name) for name in desired_names],
                *[("remove", f"stale-{i:02d}") for i in range(5)],
            ],
        )
        self.assertTrue(any("operation plan capped=true" in message for message in logs.output))

        issue = PlatformIssueLog.objects.get(
            tenant=self.tenant,
            category=PlatformIssueLog.Category.OTHER,
            tool_name="cron.reconcile",
        )
        self.assertEqual(issue.severity, PlatformIssueLog.Severity.MEDIUM)
        self.assertIn("Remaining work is deferred", issue.detail)

        mutation_order.clear()
        second = regenerate_tenant_crons(self.tenant)

        self.assertFalse(second["capped"])
        self.assertEqual(second["added"], 0)
        self.assertEqual(second["removed"], 5)
        self.assertEqual(
            mutation_order,
            [("remove", f"stale-{i:02d}") for i in range(5, 10)],
        )
        self.assertEqual({job["name"] for job in gateway_jobs}, set(desired_names))
        self.assertEqual(
            PlatformIssueLog.objects.filter(tenant=self.tenant, tool_name="cron.reconcile").count(),
            1,
        )

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_removals_are_capped_independently_and_drain_next_pass(self, mock_invoke):
        gateway_jobs = [_gateway_job(f"stale-{i:02d}") for i in reversed(range(15))]
        removed_names: list[str] = []

        def _invoke(tenant, tool, args):
            if tool == "cron.list":
                return {"details": {"jobs": [dict(job) for job in gateway_jobs]}}
            if tool == "cron.remove":
                job = next(job for job in gateway_jobs if job["id"] == args["jobId"])
                gateway_jobs.remove(job)
                removed_names.append(job["name"])
            return {"ok": True}

        mock_invoke.side_effect = _invoke
        first = regenerate_tenant_crons(self.tenant)

        self.assertTrue(first["capped"])
        self.assertEqual(first["removed"], MAX_REMOVES_PER_PASS)
        self.assertEqual(
            removed_names,
            [f"stale-{i:02d}" for i in range(MAX_REMOVES_PER_PASS)],
        )
        issue = PlatformIssueLog.objects.get(tenant=self.tenant, tool_name="cron.reconcile")
        self.assertEqual(issue.severity, PlatformIssueLog.Severity.HIGH)

        removed_names.clear()
        second = regenerate_tenant_crons(self.tenant)

        self.assertFalse(second["capped"])
        self.assertEqual(second["removed"], 5)
        self.assertEqual(
            removed_names,
            [f"stale-{i:02d}" for i in range(MAX_REMOVES_PER_PASS, 15)],
        )
        self.assertEqual(gateway_jobs, [])
