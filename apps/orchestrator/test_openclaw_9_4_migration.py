"""OpenClaw 2026.9.4 config-schema migration.

Guards ``_migrate_config_to_openclaw_9_4`` and its version gate:
``generate_openclaw_config`` emits the 2026.5.28 baseline shape and rewrites it
to the 2026.9.4 schema ONLY for tenants whose ``openclaw_version >= 2026.9.4``,
so a mixed-version rollout stays safe (a still-on-5.28 tenant whose config
regenerates keeps the valid 5.28 shape). Transforms were verified against
``openclaw@2026.9.4 doctor`` on 2026-09-16.
"""

from __future__ import annotations

from django.test import SimpleTestCase, TestCase

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
                "bootstrapMaxChars": 26000,
                "bootstrapTotalMaxChars": 80000,
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

    def test_bootstrap_budgets_survive_migration(self):
        defaults = self.cfg["agents"]["defaults"]
        self.assertEqual(defaults["bootstrapMaxChars"], 26000)
        self.assertEqual(defaults["bootstrapTotalMaxChars"], 80000)

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

    def test_web_search_enabled_via_vendored_brave(self):
        # 9.4 restores web search through the image-vendored brave provider on
        # plugins.load.paths (no boot-time npm install). Provider is pinned to
        # brave and perplexity is turned off so OPENROUTER_API_KEY doesn't
        # trigger perplexity's boot install.
        search = self.cfg["tools"]["web"]["search"]
        self.assertIs(search["enabled"], True)
        self.assertEqual(search["provider"], "brave")
        self.assertIn(
            "/opt/nbhd/vendored/brave-project/node_modules/@openclaw/brave-plugin", self.cfg["plugins"]["load"]["paths"]
        )
        self.assertIs(self.cfg["plugins"]["entries"]["brave"]["enabled"], True)
        self.assertIs(self.cfg["plugins"]["entries"]["perplexity"]["enabled"], False)


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
        # Web search restored via the vendored brave provider on 9.4.
        self.assertIs(config["tools"]["web"]["search"]["enabled"], True)
        self.assertEqual(config["tools"]["web"]["search"]["provider"], "brave")
        self.assertIn(
            "/opt/nbhd/vendored/brave-project/node_modules/@openclaw/brave-plugin", config["plugins"]["load"]["paths"]
        )

    def test_5_28_tenant_keeps_legacy_shape(self):
        tenant = create_tenant(display_name="Mig528", telegram_chat_id=740002)
        tenant.openclaw_version = "2026.5.28"
        config = generate_openclaw_config(tenant)
        defaults = config["agents"]["defaults"]
        self.assertIn("pdfMaxBytesMb", defaults)
        self.assertIn("memorySearch", defaults)
        self.assertIn("commitments", config)
        self.assertIn("redactSensitive", config["logging"])
        # 5.28 keeps web search (no forced disable) and does NOT get the 9.4
        # image-only vendored brave path.
        self.assertNotEqual(
            config.get("tools", {}).get("web", {}).get("search", {}).get("enabled"),
            False,
        )
        self.assertNotIn(
            "/opt/nbhd/vendored/brave-project/node_modules/@openclaw/brave-plugin",
            config.get("plugins", {}).get("load", {}).get("paths", []),
        )
