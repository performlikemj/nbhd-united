"""Regression coverage for the dedup pre-pass in regenerate_tenant_crons.

The bug (canary 2026-05-11 snapshot):
``regenerate_tenant_crons`` built ``current_managed = {name: job}`` from
``cron.list``, which silently collapses same-name duplicates to one dict
entry. Any duplicate in OC's SQLite was invisible to the reconciler's
``to_add`` / ``to_remove`` diff and accumulated indefinitely. The canary's
13:00:13 UTC snapshot showed Personal Question (×2), Project Check-in (×2),
and Gravity Weekly Check-in (×2) — likely from pre-Postgres-canonical
state that the 2026-05-01 cutover never cleaned from the runtime.

Behavioral risk: when two crons share a name in the runtime, BOTH fire on
their respective schedules. If one has a stale payload (e.g. pre-PR-506
shape) and the other has the current one, which fires "first" is
undefined, and the user gets the wrong message — or worse, two messages.

The fix adds a pre-pass: group ``current_jobs`` by name, keep the newest
by ``createdAtMs``, reap the rest via ``cron.remove`` before the main
add/remove diff runs.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from apps.cron.gateway_client import GatewayError
from apps.cron.models import CronJob
from apps.orchestrator.cron_reconcile import regenerate_tenant_crons
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


def _list_response(jobs: list[dict]) -> dict:
    """Wrap a job list in OpenClaw's cron.list response envelope."""
    return {"jobs": jobs, "total": len(jobs)}


def _gw_job(name: str, *, gateway_id: str, created_at_ms: int, schedule_expr: str = "0 7 * * *") -> dict:
    """Job as returned by cron.list, with createdAtMs for dedup ordering."""
    return {
        "id": gateway_id,
        "name": name,
        "schedule": {"kind": "cron", "expr": schedule_expr, "tz": "UTC"},
        "sessionTarget": "isolated",
        "payload": {"kind": "agentTurn", "message": "test"},
        "enabled": True,
        "createdAtMs": created_at_ms,
    }


class RegenerateDedupTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Dedup Test", telegram_chat_id=753951357)
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_id = "oc-dedup-test"
        self.tenant.container_fqdn = "oc-dedup-test.internal"
        self.tenant.postgres_cron_canonical = True
        self.tenant.save()

        # One desired Postgres row per cron name (the canary state).
        CronJob.objects.create(
            tenant=self.tenant,
            name="Personal Question",
            managed=True,
            data={
                "name": "Personal Question",
                "schedule": {"kind": "cron", "expr": "0 6 * * *", "tz": "Asia/Tokyo"},
                "sessionTarget": "isolated",
                "payload": {"kind": "agentTurn", "message": "test"},
                "enabled": True,
            },
        )

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_dedup_reaps_older_duplicate(self, mock_invoke):
        """The canary scenario: two SQLite jobs share a name, the older one
        is reaped, the newer one is kept (and the reconciler doesn't try to
        re-add the cron because the kept entry still satisfies the diff)."""
        # cron.list returns TWO jobs with the same name, different createdAtMs.
        # Newer = id-new (createdAtMs higher). Older = id-old.
        mock_invoke.side_effect = [
            _list_response(
                [
                    _gw_job("Personal Question", gateway_id="id-old", created_at_ms=1_700_000_000_000),
                    _gw_job("Personal Question", gateway_id="id-new", created_at_ms=1_710_000_000_000),
                ]
            ),
            {"ok": True},  # cron.remove for the older dup
        ]

        result = regenerate_tenant_crons(self.tenant)

        # Exactly one cron.remove call, against the older duplicate.
        remove_calls = [c for c in mock_invoke.call_args_list if c.args[1] == "cron.remove"]
        self.assertEqual(len(remove_calls), 1)
        self.assertEqual(remove_calls[0].args[2], {"jobId": "id-old"})

        # No cron.add called — kept duplicate already satisfies the desired set.
        add_calls = [c for c in mock_invoke.call_args_list if c.args[1] == "cron.add"]
        self.assertEqual(add_calls, [])

        self.assertEqual(result["duplicates_reaped"], 1)
        # The kept dup is counted as "unchanged" by the diff.
        self.assertEqual(result["unchanged"], 1)

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_dedup_handles_multiple_dup_groups(self, mock_invoke):
        """Several distinct names each duplicated — all extras reaped in one pass."""
        CronJob.objects.create(
            tenant=self.tenant,
            name="Project Check-in",
            managed=True,
            data={
                "name": "Project Check-in",
                "schedule": {"kind": "cron", "expr": "0 9 * * 1-5", "tz": "Asia/Tokyo"},
                "sessionTarget": "isolated",
                "payload": {"kind": "agentTurn", "message": "test"},
                "enabled": True,
            },
        )

        mock_invoke.side_effect = [
            _list_response(
                [
                    _gw_job("Personal Question", gateway_id="pq-old", created_at_ms=1_700_000_000_000),
                    _gw_job("Personal Question", gateway_id="pq-new", created_at_ms=1_710_000_000_000),
                    _gw_job("Project Check-in", gateway_id="pc-old", created_at_ms=1_700_000_000_000),
                    _gw_job("Project Check-in", gateway_id="pc-new", created_at_ms=1_710_000_000_000),
                ]
            ),
            {"ok": True},  # cron.remove for pq-old
            {"ok": True},  # cron.remove for pc-old
        ]

        result = regenerate_tenant_crons(self.tenant)

        remove_calls = [c for c in mock_invoke.call_args_list if c.args[1] == "cron.remove"]
        reaped_ids = sorted(c.args[2]["jobId"] for c in remove_calls)
        self.assertEqual(reaped_ids, ["pc-old", "pq-old"])
        self.assertEqual(result["duplicates_reaped"], 2)

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_no_dedup_when_no_duplicates(self, mock_invoke):
        """A clean container — no extra cron.remove calls fired."""
        mock_invoke.side_effect = [
            _list_response(
                [_gw_job("Personal Question", gateway_id="pq-only", created_at_ms=1_710_000_000_000)],
            ),
        ]

        result = regenerate_tenant_crons(self.tenant)

        self.assertEqual(result["duplicates_reaped"], 0)
        remove_calls = [c for c in mock_invoke.call_args_list if c.args[1] == "cron.remove"]
        self.assertEqual(remove_calls, [])

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_dedup_skips_unmanaged_prefixes(self, mock_invoke):
        """`_sync:*` and ``kind:"at"`` jobs are intentionally outside the
        reconciler's purview — duplicates among them aren't reaped here
        (they self-clean via cron's deleteAfterRun, or are managed by
        the agent). Verifies we don't accidentally widen the blast radius.
        """
        mock_invoke.side_effect = [
            _list_response(
                [
                    {
                        "id": "sync-1",
                        "name": "_sync:Morning Briefing",
                        "schedule": {"kind": "cron", "expr": "5 7 12 5 *", "tz": "UTC"},
                        "createdAtMs": 1_700_000_000_000,
                    },
                    {
                        "id": "sync-2",
                        "name": "_sync:Morning Briefing",
                        "schedule": {"kind": "cron", "expr": "5 7 12 5 *", "tz": "UTC"},
                        "createdAtMs": 1_710_000_000_000,
                    },
                    # The legit Personal Question (single copy).
                    _gw_job("Personal Question", gateway_id="pq-only", created_at_ms=1_710_000_000_000),
                ]
            ),
        ]

        result = regenerate_tenant_crons(self.tenant)

        # _sync:* duplicates are NOT touched.
        remove_calls = [c for c in mock_invoke.call_args_list if c.args[1] == "cron.remove"]
        self.assertEqual(remove_calls, [])
        self.assertEqual(result["duplicates_reaped"], 0)

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_dedup_continues_on_individual_remove_failure(self, mock_invoke):
        """One failing cron.remove must not stop the rest of the dedup batch."""
        CronJob.objects.create(
            tenant=self.tenant,
            name="Project Check-in",
            managed=True,
            data={
                "name": "Project Check-in",
                "schedule": {"kind": "cron", "expr": "0 9 * * 1-5", "tz": "Asia/Tokyo"},
                "sessionTarget": "isolated",
                "payload": {"kind": "agentTurn", "message": "test"},
                "enabled": True,
            },
        )

        mock_invoke.side_effect = [
            _list_response(
                [
                    _gw_job("Personal Question", gateway_id="pq-old", created_at_ms=1_700_000_000_000),
                    _gw_job("Personal Question", gateway_id="pq-new", created_at_ms=1_710_000_000_000),
                    _gw_job("Project Check-in", gateway_id="pc-old", created_at_ms=1_700_000_000_000),
                    _gw_job("Project Check-in", gateway_id="pc-new", created_at_ms=1_710_000_000_000),
                ]
            ),
            GatewayError("503 first remove failed"),  # pq-old: fails
            {"ok": True},  # pc-old: succeeds
        ]

        result = regenerate_tenant_crons(self.tenant)

        # Exactly one succeeded, one errored.
        self.assertEqual(result["duplicates_reaped"], 1)
        self.assertGreaterEqual(result["errors"], 1)
