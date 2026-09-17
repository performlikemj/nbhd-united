"""Tests for the 2026.9.4 signed cron-file writer (apps/cron/share_cron_sync.py).

The signature and payload shape here MUST match what the in-container helper
(runtime/openclaw/nbhd-cron-sync.mjs) verifies: it HMAC-SHA256s the exact
``signed`` string with ``NBHD_INTERNAL_API_KEY`` and then parses it.
"""

from __future__ import annotations

import hmac
import json
import time
from hashlib import sha256
from unittest import mock

from django.test import TestCase, override_settings

from apps.cron.models import CronJob
from apps.cron.share_cron_sync import (
    _desired_jobs,
    build_signed_crons_doc,
    tenant_uses_file_cron_sync,
)
from apps.tenants.services import create_tenant

_KEY = "test-internal-key-xyz"


class TenantVersionGateTest(TestCase):
    def test_9_4_uses_file_sync_but_5_28_does_not(self):
        t = create_tenant(display_name="v", telegram_chat_id=810001)
        t.openclaw_version = "2026.9.4"
        self.assertTrue(tenant_uses_file_cron_sync(t))
        t.openclaw_version = "2026.9.5"
        self.assertTrue(tenant_uses_file_cron_sync(t))
        t.openclaw_version = "2026.5.28"
        self.assertFalse(tenant_uses_file_cron_sync(t))


def _mk(tenant, name, *, kind, enabled=True, managed=True, at_iso=None, message="hi"):
    schedule = {"kind": kind}
    if kind == "every":
        schedule["everyMs"] = 3600000
    elif kind == "cron":
        schedule["expr"] = "0 9 * * *"
    elif kind == "at":
        schedule["at"] = at_iso
    return CronJob.objects.create(
        tenant=tenant,
        name=name,
        enabled=enabled,
        managed=managed,
        data={"schedule": schedule, "payload": {"kind": "agentTurn", "message": message}},
    )


@override_settings(NBHD_INTERNAL_API_KEY=_KEY)
class DesiredJobsTest(TestCase):
    def setUp(self):
        self.t = create_tenant(display_name="dj", telegram_chat_id=810002)

    def test_includes_managed_recurring_excludes_disabled_and_unmanaged(self):
        _mk(self.t, "Morning Briefing", kind="cron")
        _mk(self.t, "Disabled One", kind="every", enabled=False)
        _mk(self.t, "Agent Owned", kind="every", managed=False)
        _mk(self.t, "heartbeat-main", kind="every")  # system self-cleaning name
        names = {j["name"] for j in _desired_jobs(self.t)}
        self.assertIn("Morning Briefing", names)
        self.assertNotIn("Disabled One", names)
        self.assertNotIn("Agent Owned", names)
        self.assertNotIn("heartbeat-main", names)

    def test_future_at_included_past_at_excluded(self):
        future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
        past = "2020-01-01T00:00:00Z"
        _mk(self.t, "Future Reminder", kind="at", managed=False, at_iso=future)
        _mk(self.t, "Past Reminder", kind="at", managed=False, at_iso=past)
        names = {j["name"] for j in _desired_jobs(self.t)}
        self.assertIn("Future Reminder", names)
        self.assertNotIn("Past Reminder", names)

    def test_each_job_carries_stable_nbhd_declaration_key(self):
        row = _mk(self.t, "R", kind="cron")
        jobs = _desired_jobs(self.t)
        self.assertEqual(jobs[0]["declarationKey"], f"nbhd:{row.id}")


@override_settings(NBHD_INTERNAL_API_KEY=_KEY)
class SignedDocTest(TestCase):
    def test_signature_matches_container_contract(self):
        t = create_tenant(display_name="sd", telegram_chat_id=810003)
        _mk(t, "R", kind="cron")
        data, count = build_signed_crons_doc(t)
        self.assertEqual(count, 1)
        doc = json.loads(data.decode("utf-8"))
        # The container HMACs the exact `signed` string with the key, then parses it.
        expected_sig = hmac.new(_KEY.encode("utf-8"), doc["signed"].encode("utf-8"), sha256).hexdigest()
        self.assertEqual(doc["sig"], expected_sig)
        jobs = json.loads(doc["signed"])
        self.assertEqual(jobs[0]["name"], "R")
        self.assertTrue(jobs[0]["declarationKey"].startswith("nbhd:"))

    @override_settings(NBHD_INTERNAL_API_KEY="")
    def test_refuses_to_sign_without_key(self):
        t = create_tenant(display_name="nk", telegram_chat_id=810004)
        with self.assertRaises(RuntimeError):
            build_signed_crons_doc(t)


@override_settings(NBHD_INTERNAL_API_KEY=_KEY)
class ReconcileRoutingTest(TestCase):
    """regenerate_tenant_crons routes 9.4 tenants to the signed file, not the
    gated gateway RPC; 5.28 tenants still use the gateway path."""

    def _tenant(self, version, chat_id):
        t = create_tenant(display_name="rt", telegram_chat_id=chat_id)
        t.postgres_cron_canonical = True
        t.container_fqdn = "oc-test.example.com"
        t.openclaw_version = version
        t.save()
        return t

    def test_9_4_writes_file_and_skips_gateway(self):
        from apps.orchestrator.cron_reconcile import regenerate_tenant_crons

        t = self._tenant("2026.9.4", 810101)
        _mk(t, "Morning Briefing", kind="cron")
        with (
            mock.patch("apps.orchestrator.azure_client._put_share_file") as put,
            mock.patch("apps.cron.gateway_client.invoke_gateway_tool") as invoke,
        ):
            summary = regenerate_tenant_crons(t)
        put.assert_called_once()
        invoke.assert_not_called()
        self.assertEqual(summary.get("file_synced"), 1)
        # The uploaded body is a validly-signed doc the container would accept.
        doc = json.loads(put.call_args.kwargs["data"].decode("utf-8"))
        expected = hmac.new(_KEY.encode(), doc["signed"].encode(), sha256).hexdigest()
        self.assertEqual(doc["sig"], expected)

    def test_5_28_still_uses_gateway(self):
        from apps.orchestrator.cron_reconcile import regenerate_tenant_crons

        t = self._tenant("2026.5.28", 810102)
        with (
            mock.patch("apps.orchestrator.azure_client._put_share_file") as put,
            mock.patch(
                "apps.cron.gateway_client.invoke_gateway_tool",
                return_value={"jobs": []},
            ) as invoke,
        ):
            regenerate_tenant_crons(t)
        put.assert_not_called()
        self.assertTrue(invoke.called)
