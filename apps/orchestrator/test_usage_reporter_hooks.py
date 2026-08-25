"""Canary gate, config snapshots, and probe coverage for usage hooks."""

from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.billing.models import UsageRecord
from apps.orchestrator.config_generator import generate_openclaw_config, usage_hooks_enabled
from apps.orchestrator.config_validator import validate_openclaw_config
from apps.tenants.services import create_tenant


class UsageHooksGateTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Usage Hooks Gate", telegram_chat_id=908101)

    def test_gate_parsing_is_fail_closed_and_supports_fleet_lever(self):
        other = "00000000-0000-4000-8000-000000000999"
        cases = (
            ("", False),
            (f" {other}, {str(self.tenant.id).upper()} ", True),
            ("*", True),
            ("garbage", False),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw), override_settings(USAGE_HOOKS_TENANT_IDS=raw):
                self.assertEqual(usage_hooks_enabled(self.tenant), expected)


class UsageReporterConfigSnapshotTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Usage Hooks Config", telegram_chat_id=908102)

    def _usage_entry(self, *, helper_gate: str = "", usage_gate: str = ""):
        with override_settings(
            SUBAGENT_TENANT_IDS=helper_gate,
            USAGE_HOOKS_TENANT_IDS=usage_gate,
        ):
            config = generate_openclaw_config(self.tenant)
        return config, config["plugins"]["entries"]["nbhd-usage-reporter"]

    def test_none_snapshot_is_byte_identical_to_historical_entry(self):
        config, entry = self._usage_entry()
        self.assertEqual(entry, {"enabled": True})

        with override_settings(
            SUBAGENT_TENANT_IDS="00000000-0000-4000-8000-000000000998",
            USAGE_HOOKS_TENANT_IDS="garbage",
        ):
            nonmatching = generate_openclaw_config(self.tenant)
        self.assertEqual(nonmatching, config)

    def test_helper_snapshot(self):
        config, entry = self._usage_entry(helper_gate=str(self.tenant.id))
        self.assertEqual(
            entry,
            {
                "enabled": True,
                "hooks": {"allowConversationAccess": True},
                "config": {"meterScopes": ["helper"]},
            },
        )
        self.assertFalse([i for i in validate_openclaw_config(config, strict=True) if i.severity == "error"])

    def test_cron_snapshot(self):
        config, entry = self._usage_entry(usage_gate=str(self.tenant.id))
        self.assertEqual(
            entry,
            {
                "enabled": True,
                "hooks": {"allowConversationAccess": True},
                "config": {"meterScopes": ["cron"]},
            },
        )
        self.assertFalse([i for i in validate_openclaw_config(config, strict=True) if i.severity == "error"])

    def test_both_snapshot(self):
        config, entry = self._usage_entry(
            helper_gate=str(self.tenant.id),
            usage_gate=str(self.tenant.id),
        )
        self.assertEqual(
            entry,
            {
                "enabled": True,
                "hooks": {"allowConversationAccess": True},
                "config": {"meterScopes": ["helper", "cron"]},
            },
        )
        self.assertFalse([i for i in validate_openclaw_config(config, strict=True) if i.severity == "error"])


class UsageHooksProbeTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Usage Hooks Probe", telegram_chat_id=908103)

    def test_probe_prints_recent_counts_by_event_type(self):
        UsageRecord.objects.create(tenant=self.tenant, event_type="cron_message")
        UsageRecord.objects.create(tenant=self.tenant, event_type="cron_message")
        UsageRecord.objects.create(tenant=self.tenant, event_type="message")
        stale = UsageRecord.objects.create(tenant=self.tenant, event_type="subagent_message")
        UsageRecord.objects.filter(id=stale.id).update(created_at=timezone.now() - timedelta(minutes=61))

        stdout = StringIO()
        call_command(
            "usage_hooks_probe",
            "--tenant",
            str(self.tenant.id),
            "--since-minutes",
            "60",
            stdout=stdout,
        )

        output = stdout.getvalue()
        self.assertIn("cron_message: 2", output)
        self.assertIn("message: 1", output)
        self.assertNotIn("subagent_message", output)
        self.assertIn("total: 3", output)
