"""Write-time config validation gate — regression coverage.

Code-level guard for the config/binary schema-skew class (the 2026-07-05
suspended-tenant crash-loops: stale configs whose ``agents.defaults`` shape the
newer OpenClaw binary rejects with ``agents.defaults: Invalid input``). The file
is valid JSON, so every prior guard (the entrypoint's ``JSON.parse`` check, the
loose Python validator) passed it — but the gateway (:18789) refuses to start on
the schema-invalid content while the proxy (:8080) stays up, so Azure health
looks green while every message and cron dies with ``ECONNREFUSED``. The gate
also backstops any future generator bug that would emit an invalid
``agents.defaults``.

These tests pin:
  1. The strict ``agents.defaults`` shape check (unknown key, null, bad model).
  2. That REAL generated configs — default AND the maximal-feature-flag shape
     of the tenants that actually broke (friends + all experimental flags +
     OC 5.28 params/contextPruning) — pass the strict check (no false positive).
  3. That ``upload_config_to_file_share`` REFUSES an unparseable or
     schema-invalid config and does NOT touch the share (keeps last-good).
"""

from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings

from apps.orchestrator.config_generator import config_to_json, generate_openclaw_config
from apps.orchestrator.config_validator import (
    InvalidTenantConfigError,
    assert_config_writable,
    validate_openclaw_config,
)
from apps.tenants.services import create_tenant


def _minimal_valid_config() -> dict:
    """A complete config that passes BOTH the loose and strict checks.

    Includes the required top-level blocks (gateway/channels/tools/cron) so a
    ``validate_openclaw_config(..., strict=True) == []`` assertion isolates the
    strict ``agents.defaults`` behavior under test.
    """
    return {
        "gateway": {
            "mode": "local",
            "bind": "loopback",
            "auth": {"mode": "token", "token": "${NBHD_INTERNAL_API_KEY}"},
        },
        "channels": {"telegram": {"enabled": True}},
        "tools": {"deny": ["gateway"], "elevated": {"enabled": False}},
        "cron": {"enabled": True},
        "agents": {
            "defaults": {
                "model": {"primary": "deepseek/deepseek-v4", "fallbacks": ["gemma"]},
                "models": {"deepseek/deepseek-v4": {"alias": "deepseek"}},
                "compaction": {"mode": "safeguard", "memoryFlush": {"enabled": True}},
                "memorySearch": {"enabled": False},
                "heartbeat": {"every": "0m"},
                "subagents": {"maxConcurrent": 2, "model": "deepseek/deepseek-v4"},
            }
        },
    }


class StrictAgentsDefaultsTests(TestCase):
    def test_minimal_config_passes(self):
        self.assertEqual(validate_openclaw_config(_minimal_valid_config(), strict=True), [])
        # assert_config_writable must not raise on a valid config.
        assert_config_writable(_minimal_valid_config())

    def test_all_version_gated_direct_children_pass(self):
        """params / contextPruning / cliBackends / llm are all valid direct
        children — the exact keys the incident forensics flagged as suspects.
        They must NOT trip the gate (they booted clean at 10:56Z)."""
        cfg = _minimal_valid_config()
        cfg["agents"]["defaults"]["params"] = {"cacheRetention": "long"}
        cfg["agents"]["defaults"]["contextPruning"] = {"mode": "cache-ttl"}
        cfg["agents"]["defaults"]["cliBackends"] = {"claude-cli": {"command": "/opt/nbhd/x.sh"}}
        cfg["agents"]["defaults"]["llm"] = {"idleTimeoutSeconds": 300}
        self.assertEqual(validate_openclaw_config(cfg, strict=True), [])

    def test_unknown_direct_child_is_rejected(self):
        """The 2026-07-05 signature: an unrecognized direct child of
        agents.defaults → parent-level 'Invalid input'."""
        cfg = _minimal_valid_config()
        cfg["agents"]["defaults"]["bogusRuntimeKey"] = {"x": 1}
        errors = [i for i in validate_openclaw_config(cfg, strict=True) if i.severity == "error"]
        self.assertTrue(any(e.path == "agents.defaults" and "bogusRuntimeKey" in e.message for e in errors))
        with self.assertRaises(InvalidTenantConfigError):
            assert_config_writable(cfg)

    def test_null_direct_child_is_rejected(self):
        cfg = _minimal_valid_config()
        cfg["agents"]["defaults"]["heartbeat"] = None
        errors = [i for i in validate_openclaw_config(cfg, strict=True) if i.severity == "error"]
        self.assertTrue(any("heartbeat" in e.path and "null" in e.message for e in errors))

    def test_defaults_not_object_is_rejected(self):
        cfg = {"agents": {"defaults": "oops"}}
        errors = [i for i in validate_openclaw_config(cfg, strict=True) if i.severity == "error"]
        self.assertTrue(any(e.path == "agents.defaults" for e in errors))

    def test_empty_primary_model_is_rejected(self):
        cfg = _minimal_valid_config()
        cfg["agents"]["defaults"]["model"]["primary"] = ""
        with self.assertRaises(InvalidTenantConfigError):
            assert_config_writable(cfg)

    def test_fallbacks_must_be_list_of_strings(self):
        cfg = _minimal_valid_config()
        cfg["agents"]["defaults"]["model"]["fallbacks"] = [{"not": "a string"}]
        errors = [i for i in validate_openclaw_config(cfg, strict=True) if i.severity == "error"]
        self.assertTrue(any(e.path == "agents.defaults.model.fallbacks" for e in errors))


class GeneratedConfigStrictTests(TestCase):
    """Real generator output must never trip the strict gate."""

    def test_default_tenant_config_passes(self):
        tenant = create_tenant(display_name="StrictDefault", telegram_chat_id=707001)
        config = generate_openclaw_config(tenant)
        errors = [i for i in validate_openclaw_config(config, strict=True) if i.severity == "error"]
        self.assertEqual(errors, [], f"default config tripped strict gate: {errors}")

    def test_maximal_feature_flag_tenant_passes(self):
        """The shape of the tenants that actually broke: friends on + every
        experimental flag + OC 5.28 (params/contextPruning + built-in
        heartbeat with directPolicy/lightContext/etc.). This is the config
        whose agents.defaults must remain schema-clean."""
        tenant = create_tenant(display_name="StrictMaximal", telegram_chat_id=707002)
        tenant.openclaw_version = "2026.5.28"
        tenant.friends_enabled = True
        tenant.friends_agent_propose_enabled = True
        tenant.experimental_built_in_heartbeat = True
        tenant.experimental_typed_journal_lifecycle = True
        tenant.experimental_memory_core_enabled = True
        tenant.experimental_active_memory_enabled = True
        tenant.experimental_dreaming_enabled = True
        tenant.experimental_typed_crons = True
        tenant.fuel_enabled = True
        tenant.journal_shaping_enabled = True
        tenant.document_ingestion_enabled = True
        tenant.save()

        config = generate_openclaw_config(tenant)
        # Sanity: the version-gated direct children are actually present so
        # this test is exercising the real incident-cohort shape.
        defaults = config["agents"]["defaults"]
        self.assertIn("params", defaults)
        self.assertIn("contextPruning", defaults)
        self.assertNotEqual(defaults["heartbeat"], {"every": "0m"})  # built-in heartbeat active

        errors = [i for i in validate_openclaw_config(config, strict=True) if i.severity == "error"]
        self.assertEqual(errors, [], f"maximal-flags config tripped strict gate: {errors}")


@override_settings(AZURE_MOCK="true")
class UploadConfigGateTests(TestCase):
    """upload_config_to_file_share must refuse bad configs and keep last-good."""

    def setUp(self):
        self.tenant = create_tenant(display_name="UploadGate", telegram_chat_id=707003)
        self.valid_json = config_to_json(generate_openclaw_config(self.tenant))

    def test_valid_config_is_written(self):
        from apps.orchestrator.azure_client import upload_config_to_file_share

        with patch("apps.orchestrator.azure_client._put_share_file") as mock_put:
            upload_config_to_file_share(str(self.tenant.id), self.valid_json)
        mock_put.assert_called_once()

    def test_non_json_is_refused_and_not_written(self):
        from apps.orchestrator.azure_client import upload_config_to_file_share

        with (
            patch("apps.orchestrator.azure_client._put_share_file") as mock_put,
            self.assertRaises(InvalidTenantConfigError),
        ):
            upload_config_to_file_share(str(self.tenant.id), "{not valid json")
        mock_put.assert_not_called()

    def test_schema_invalid_config_is_refused_and_not_written(self):
        """A JSON-valid but schema-invalid config (unknown agents.defaults key)
        is the exact 2026-07-05 shape — refuse the write, keep last-good."""
        from apps.orchestrator.azure_client import upload_config_to_file_share

        bad = copy.deepcopy(json.loads(self.valid_json))
        bad["agents"]["defaults"]["bogusRuntimeKey"] = {"x": 1}
        bad_json = json.dumps(bad)

        with (
            patch("apps.orchestrator.azure_client._put_share_file") as mock_put,
            self.assertRaises(InvalidTenantConfigError),
        ):
            upload_config_to_file_share(str(self.tenant.id), bad_json)
        mock_put.assert_not_called()


class GenerateSmokeConfigCommandTests(TestCase):
    """The CI boot smoke depends on `generate_smoke_config --maximal`."""

    def test_maximal_command_writes_strict_valid_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "openclaw-maximal.json"
            call_command("generate_smoke_config", "--maximal", "--output", str(out))
            config = json.loads(out.read_text())

        errors = [i for i in validate_openclaw_config(config, strict=True) if i.severity == "error"]
        self.assertEqual(errors, [], f"generated smoke config failed strict validation: {errors}")
        # The maximal flags must actually surface (so the smoke exercises them).
        self.assertIn("params", config["agents"]["defaults"])
        paths = config.get("plugins", {}).get("load", {}).get("paths", [])
        self.assertTrue(any("nbhd-friends-tools" in p for p in paths))
        self.assertIn("/opt/nbhd/plugins/nbhd-journal-shaping", paths)
        self.assertIn("/opt/nbhd/plugins/nbhd-document-keep", paths)
        entries = config.get("plugins", {}).get("entries", {})
        self.assertIn("nbhd-journal-shaping", entries)
        self.assertTrue(entries["nbhd-journal-shaping"]["config"]["journalShapingEnabled"])
        self.assertIn("nbhd-document-keep", entries)
        self.assertTrue(entries["nbhd-document-keep"]["config"]["documentIngestionEnabled"])
