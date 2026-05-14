"""Regression coverage for payload-aware drift detection in
``regenerate_tenant_crons``.

Pre-this-PR the reconciler diffed by name only — a cron whose name
matched in both postgres (desired) and OC (existing) was "unchanged",
regardless of payload contents. Dashboard edits to system cron payloads
were silently dropped at the gateway boundary, and the canary 2026-05-13
incident kept resurrecting: postgres stored the right model, OC stored
a stale ``anthropic-cli/...`` value, but the reconciler never noticed.

These tests pin the contract that any drift in fields the user can't
customize (model, kind, schedule, enabled) — and meaningful drift in
the message body (after stripping the daily date preamble) — triggers
``cron.remove`` + ``cron.add`` to converge.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from apps.cron.models import CronJob
from apps.orchestrator.cron_reconcile import regenerate_tenant_crons
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


def _list_response(jobs: list[dict]) -> dict:
    return {"jobs": jobs, "total": len(jobs)}


def _gw_job(
    name: str,
    *,
    gateway_id: str,
    payload: dict | None = None,
    schedule: dict | None = None,
    enabled: bool = True,
    created_at_ms: int = 1_700_000_000_000,
) -> dict:
    """Job as returned by cron.list."""
    return {
        "id": gateway_id,
        "name": name,
        "schedule": schedule or {"kind": "cron", "expr": "0 7 * * *", "tz": "UTC"},
        "sessionTarget": "isolated",
        "payload": payload or {"kind": "agentTurn", "message": "body"},
        "enabled": enabled,
        "createdAtMs": created_at_ms,
    }


class _SetUpMixin:
    def _make_tenant(self):
        tenant = create_tenant(display_name="Drift Test", telegram_chat_id=999111222)
        tenant.status = Tenant.Status.ACTIVE
        tenant.container_id = "oc-drift-test"
        tenant.container_fqdn = "oc-drift-test.internal"
        tenant.postgres_cron_canonical = True
        tenant.save()
        return tenant

    def _make_row(self, tenant, *, name: str, data: dict) -> CronJob:
        return CronJob.objects.create(tenant=tenant, name=name, managed=True, data=data)


class PayloadModelDriftTests(_SetUpMixin, TestCase):
    """OC has a stale ``payload.model`` that's not in the tenant's tier
    allowlist — the canary 2026-05-13 anthropic-cli/... case. Reconciler
    must detect drift and recreate from postgres-side (clean) state.
    """

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_stale_anthropic_cli_model_in_oc_triggers_recreate(self, mock_invoke):
        tenant = self._make_tenant()
        # Postgres has the seed shape — no model.
        self._make_row(
            tenant,
            name="Morning Briefing",
            data={
                "name": "Morning Briefing",
                "schedule": {"kind": "cron", "expr": "0 7 * * *", "tz": "Asia/Tokyo"},
                "sessionTarget": "isolated",
                "payload": {"kind": "agentTurn", "message": "Good morning! body"},
                "enabled": True,
            },
        )
        # OC has the stale anthropic-cli/... model on the same cron.
        stale = _gw_job(
            "Morning Briefing",
            gateway_id="id-stale",
            payload={
                "kind": "agentTurn",
                "message": "Good morning! body",
                "model": "anthropic-cli/claude-sonnet-4-6",
            },
            schedule={"kind": "cron", "expr": "0 7 * * *", "tz": "Asia/Tokyo"},
        )
        mock_invoke.side_effect = [
            _list_response([stale]),
            {"ok": True},  # cron.remove
            {"ok": True},  # cron.add
        ]

        result = regenerate_tenant_crons(tenant)

        self.assertEqual(result["recreated"], 1)
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["removed"], 0)
        # The recreate flow: cron.list, cron.remove old, cron.add new.
        tools = [c.args[1] for c in mock_invoke.call_args_list]
        self.assertEqual(tools, ["cron.list", "cron.remove", "cron.add"])
        remove_args = mock_invoke.call_args_list[1].args[2]
        self.assertEqual(remove_args, {"jobId": "id-stale"})
        add_args = mock_invoke.call_args_list[2].args[2]
        self.assertEqual(add_args["job"]["name"], "Morning Briefing")
        # Critically: the recreated cron has no payload.model — clean.
        self.assertNotIn("model", add_args["job"]["payload"])


class MessageBodyDriftTests(_SetUpMixin, TestCase):
    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_message_body_drift_triggers_recreate(self, mock_invoke):
        """Postgres says one prompt body; OC stored a different one. Recreate."""
        tenant = self._make_tenant()
        self._make_row(
            tenant,
            name="Evening Check-in",
            data={
                "name": "Evening Check-in",
                "schedule": {"kind": "cron", "expr": "0 21 * * *", "tz": "UTC"},
                "sessionTarget": "isolated",
                "payload": {"kind": "agentTurn", "message": "New evening prompt"},
                "enabled": True,
            },
        )
        existing = _gw_job(
            "Evening Check-in",
            gateway_id="id-evening",
            payload={"kind": "agentTurn", "message": "Old evening prompt"},
            schedule={"kind": "cron", "expr": "0 21 * * *", "tz": "UTC"},
        )
        mock_invoke.side_effect = [_list_response([existing]), {"ok": True}, {"ok": True}]

        result = regenerate_tenant_crons(tenant)

        self.assertEqual(result["recreated"], 1)
        # Verify the new payload has the postgres message body, not OC's.
        add_args = mock_invoke.call_args_list[2].args[2]
        self.assertEqual(add_args["job"]["payload"]["message"], "New evening prompt")

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_date_only_difference_does_not_recreate(self, mock_invoke):
        """Both sides have the same body but different ``Current date and time:``
        preamble lines. ``strip_date_line`` makes them equal — no churn.
        """
        tenant = self._make_tenant()
        # Postgres has today's date in the preamble.
        postgres_msg = "Current date and time: 2026-05-14 09:00\n\nBriefing body."
        oc_msg = "Current date and time: 2026-05-13 09:00\n\nBriefing body."
        self._make_row(
            tenant,
            name="Morning Briefing",
            data={
                "name": "Morning Briefing",
                "schedule": {"kind": "cron", "expr": "0 7 * * *", "tz": "UTC"},
                "sessionTarget": "isolated",
                "payload": {"kind": "agentTurn", "message": postgres_msg},
                "enabled": True,
            },
        )
        existing = _gw_job(
            "Morning Briefing",
            gateway_id="id-mb",
            payload={"kind": "agentTurn", "message": oc_msg},
            schedule={"kind": "cron", "expr": "0 7 * * *", "tz": "UTC"},
        )
        mock_invoke.side_effect = [_list_response([existing])]

        result = regenerate_tenant_crons(tenant)

        self.assertEqual(result["recreated"], 0)
        self.assertEqual(result["unchanged"], 1)
        # Only cron.list called — no recreate, no churn.
        tools = [c.args[1] for c in mock_invoke.call_args_list]
        self.assertEqual(tools, ["cron.list"])


class ScheduleDriftTests(_SetUpMixin, TestCase):
    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_schedule_tz_drift_triggers_recreate(self, mock_invoke):
        """Timezone change: postgres updated to user's new tz, OC still on old."""
        tenant = self._make_tenant()
        self._make_row(
            tenant,
            name="Morning Briefing",
            data={
                "name": "Morning Briefing",
                "schedule": {"kind": "cron", "expr": "0 7 * * *", "tz": "Asia/Tokyo"},
                "sessionTarget": "isolated",
                "payload": {"kind": "agentTurn", "message": "body"},
                "enabled": True,
            },
        )
        existing = _gw_job(
            "Morning Briefing",
            gateway_id="id-mb",
            schedule={"kind": "cron", "expr": "0 7 * * *", "tz": "UTC"},  # ← stale tz
        )
        mock_invoke.side_effect = [_list_response([existing]), {"ok": True}, {"ok": True}]

        result = regenerate_tenant_crons(tenant)

        self.assertEqual(result["recreated"], 1)
        add_args = mock_invoke.call_args_list[2].args[2]
        self.assertEqual(add_args["job"]["schedule"]["tz"], "Asia/Tokyo")

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_schedule_expr_drift_triggers_recreate(self, mock_invoke):
        """User changed Morning Briefing time via dashboard — postgres has new
        expr, OC still on default."""
        tenant = self._make_tenant()
        self._make_row(
            tenant,
            name="Morning Briefing",
            data={
                "name": "Morning Briefing",
                "schedule": {"kind": "cron", "expr": "0 8 * * *", "tz": "UTC"},  # ← user moved to 8am
                "sessionTarget": "isolated",
                "payload": {"kind": "agentTurn", "message": "body"},
                "enabled": True,
            },
        )
        existing = _gw_job(
            "Morning Briefing",
            gateway_id="id-mb",
            schedule={"kind": "cron", "expr": "0 7 * * *", "tz": "UTC"},  # ← still 7am
        )
        mock_invoke.side_effect = [_list_response([existing]), {"ok": True}, {"ok": True}]

        result = regenerate_tenant_crons(tenant)

        self.assertEqual(result["recreated"], 1)
        add_args = mock_invoke.call_args_list[2].args[2]
        self.assertEqual(add_args["job"]["schedule"]["expr"], "0 8 * * *")


class HeartbeatTopLevelModelTests(_SetUpMixin, TestCase):
    """Heartbeat pins ``model`` at the top level of the job def. OpenClaw
    normalizes that into ``payload.model`` on store, so a naive compare
    would say DRIFT every sweep (the bug PR #533 fixed). The reconciler's
    drift detector must read ``model`` from both top-level and payload
    on the desired side to avoid spurious churn.
    """

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_heartbeat_top_level_model_does_not_cause_spurious_recreate(self, mock_invoke):
        tenant = self._make_tenant()
        self._make_row(
            tenant,
            name="Heartbeat Check-in",
            data={
                "name": "Heartbeat Check-in",
                "schedule": {"kind": "cron", "expr": "0 6,7,8 * * *", "tz": "UTC"},
                "sessionTarget": "isolated",
                "model": "openrouter/minimax/minimax-m2.7",  # ← top-level
                "payload": {"kind": "agentTurn", "message": "heartbeat body"},
                "enabled": True,
            },
        )
        # OC stored it normalized — model lives in payload.
        existing = _gw_job(
            "Heartbeat Check-in",
            gateway_id="id-hb",
            payload={
                "kind": "agentTurn",
                "message": "heartbeat body",
                "model": "openrouter/minimax/minimax-m2.7",  # ← payload-level
            },
            schedule={"kind": "cron", "expr": "0 6,7,8 * * *", "tz": "UTC"},
        )
        mock_invoke.side_effect = [_list_response([existing])]

        result = regenerate_tenant_crons(tenant)

        self.assertEqual(result["recreated"], 0)
        self.assertEqual(result["unchanged"], 1)
        # No cron.remove or cron.add — convergence detected.
        tools = [c.args[1] for c in mock_invoke.call_args_list]
        self.assertEqual(tools, ["cron.list"])
