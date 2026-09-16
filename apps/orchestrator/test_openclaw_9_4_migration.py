"""OpenClaw 2026.9.4 config-schema migration.

Guards ``_migrate_config_to_openclaw_9_4`` and its version gate:
``generate_openclaw_config`` emits the 2026.5.28 baseline shape and rewrites it
to the 2026.9.4 schema ONLY for tenants whose ``openclaw_version >= 2026.9.4``,
so a mixed-version rollout stays safe (a still-on-5.28 tenant whose config
regenerates keeps the valid 5.28 shape). Transforms were verified against
``openclaw@2026.9.4 doctor`` on 2026-09-16.
"""

from __future__ import annotations

from django.test import SimpleTestCase, TestCase, override_settings

from apps.orchestrator.config_generator import (
    _migrate_config_to_openclaw_9_4,
    generate_openclaw_config,
)
from apps.tenants.services import create_tenant


def _sample_5_28_config() -> dict:
    return {
        "agents": {
            "defaults": {
                "pdfMaxBytesMb": 10,
                "envelopeTimezone": "user",
                "userTimezone": "Asia/Tokyo",
                "compaction": {
                    "memoryFlush": {
                        "enabled": True,
                        "softThresholdTokens": 4000,
                        "systemPrompt": "steer",
                        "prompt": "review",
                    }
                },
                "heartbeat": {"every": "1h", "skipWhenBusy": True},
                "memorySearch": {
                    "enabled": True,
                    "store": {"path": "/p/{agentId}.sqlite", "fts": {"tokenizer": "trigram"}},
                },
            }
        },
        "tools": {
            "media": {
                "audio": {
                    "enabled": True,
                    "models": [{"provider": "openai", "model": "gpt-4o-mini-transcribe"}],
                }
            }
        },
        "logging": {"redactSensitive": "tools", "redactPatterns": ["x"]},
        "commitments": {"enabled": True, "maxPerDay": 3},
        "plugins": {"bundledDiscovery": "compat", "entries": {}},
    }


class OpenClaw94MigrationTransformTest(SimpleTestCase):
    """Pure transform (no DB)."""

    def setUp(self):
        self.cfg = _sample_5_28_config()
        _migrate_config_to_openclaw_9_4(self.cfg)

    def test_pdf_max_renamed(self):
        d = self.cfg["agents"]["defaults"]
        self.assertEqual(d["pdfMaxMb"], 10)
        self.assertNotIn("pdfMaxBytesMb", d)

    def test_envelope_timezone_dropped(self):
        self.assertNotIn("envelopeTimezone", self.cfg["agents"]["defaults"])

    def test_memory_flush_prompts_dropped_toggle_kept(self):
        mf = self.cfg["agents"]["defaults"]["compaction"]["memoryFlush"]
        self.assertNotIn("systemPrompt", mf)
        self.assertNotIn("prompt", mf)
        self.assertTrue(mf["enabled"])
        self.assertEqual(mf["softThresholdTokens"], 4000)

    def test_heartbeat_skip_when_busy_dropped(self):
        self.assertNotIn("skipWhenBusy", self.cfg["agents"]["defaults"]["heartbeat"])
        self.assertEqual(self.cfg["agents"]["defaults"]["heartbeat"]["every"], "1h")

    def test_memory_search_moved_to_top_level(self):
        d = self.cfg["agents"]["defaults"]
        self.assertNotIn("memorySearch", d)
        ms = self.cfg["memory"]["search"]
        self.assertTrue(ms["enabled"])
        # store.path dropped (indexes live in each agent DB); FTS preserved.
        self.assertNotIn("path", ms.get("store", {}))
        self.assertEqual(ms["store"]["fts"]["tokenizer"], "trigram")

    def test_media_models_capability_tagged(self):
        media = self.cfg["tools"]["media"]
        self.assertNotIn("models", media["audio"])
        self.assertTrue(media["audio"]["enabled"])
        self.assertEqual(
            media["models"],
            [{"provider": "openai", "model": "gpt-4o-mini-transcribe", "capabilities": ["audio"]}],
        )

    def test_redact_sensitive_dropped_patterns_kept(self):
        self.assertNotIn("redactSensitive", self.cfg["logging"])
        self.assertEqual(self.cfg["logging"]["redactPatterns"], ["x"])

    def test_commitments_dropped(self):
        self.assertNotIn("commitments", self.cfg)

    def test_bundled_discovery_dropped_entries_kept(self):
        self.assertNotIn("bundledDiscovery", self.cfg["plugins"])
        self.assertIn("entries", self.cfg["plugins"])


class OpenClaw94MigrationGateTest(TestCase):
    """The migration is version-gated so mixed-version rollout is safe."""

    def test_9_4_tenant_gets_migrated_shape(self):
        tenant = create_tenant(display_name="Mig94", telegram_chat_id=740001)
        tenant.openclaw_version = "2026.9.4"
        config = generate_openclaw_config(tenant)
        defaults = config["agents"]["defaults"]
        self.assertIn("pdfMaxMb", defaults)
        self.assertNotIn("pdfMaxBytesMb", defaults)
        self.assertNotIn("memorySearch", defaults)
        self.assertIn("search", config.get("memory", {}))
        self.assertNotIn("commitments", config)
        self.assertNotIn("redactSensitive", config["logging"])
        self.assertNotIn("skipWhenBusy", defaults.get("heartbeat", {}))

    def test_5_28_tenant_keeps_legacy_shape(self):
        tenant = create_tenant(display_name="Mig528", telegram_chat_id=740002)
        tenant.openclaw_version = "2026.5.28"
        config = generate_openclaw_config(tenant)
        defaults = config["agents"]["defaults"]
        self.assertIn("pdfMaxBytesMb", defaults)
        self.assertIn("memorySearch", defaults)
        self.assertIn("commitments", config)
        self.assertIn("redactSensitive", config["logging"])


@override_settings(
    OPENCLAW_CRON_ENFORCEMENT_PLUGIN_ID="nbhd-cron-enforcement",
    OPENCLAW_DOC_TAINT_GUARD_PLUGIN_ID="nbhd-doc-taint-guard",
    OPENCLAW_ROUTING_CONTEXT_PLUGIN_ID="nbhd-routing-context",
)
class OpenClaw94PluginConversationAccessTest(SimpleTestCase):
    """9.4 blocks conversation-reading hooks unless the plugin entry sets
    hooks.allowConversationAccess. The migration grants it to the three nbhd
    plugins that register such hooks — but only when the plugin is loaded."""

    @staticmethod
    def _migrate(plugins):
        cfg = {"plugins": plugins}
        _migrate_config_to_openclaw_9_4(cfg)
        return cfg["plugins"]

    def test_flag_granted_to_loaded_conversation_hook_plugins(self):
        plugins = self._migrate(
            {
                "load": {
                    "paths": [
                        "/opt/nbhd/plugins/nbhd-cron-enforcement",
                        "/opt/nbhd/plugins/nbhd-doc-taint-guard",
                        "/opt/nbhd/plugins/nbhd-routing-context",
                        "/opt/nbhd/plugins/nbhd-fuel-tools",
                    ]
                },
                "entries": {"nbhd-doc-taint-guard": {"config": {"mode": "log_only"}}},
                "bundledDiscovery": "compat",
            }
        )
        entries = plugins["entries"]
        self.assertTrue(entries["nbhd-cron-enforcement"]["hooks"]["allowConversationAccess"])
        self.assertTrue(entries["nbhd-routing-context"]["hooks"]["allowConversationAccess"])
        # existing config on the entry is preserved alongside the new hooks block
        self.assertTrue(entries["nbhd-doc-taint-guard"]["hooks"]["allowConversationAccess"])
        self.assertEqual(entries["nbhd-doc-taint-guard"]["config"]["mode"], "log_only")
        # a non-conversation-hook plugin gets no phantom entry / flag
        self.assertNotIn("nbhd-fuel-tools", entries)
        self.assertNotIn("bundledDiscovery", plugins)

    def test_no_flag_for_unloaded_plugin(self):
        plugins = self._migrate({"load": {"paths": ["/opt/nbhd/plugins/nbhd-fuel-tools"]}, "entries": {}})
        self.assertNotIn("nbhd-cron-enforcement", plugins.get("entries", {}))
