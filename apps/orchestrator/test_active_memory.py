"""Tests for the active-memory plugin entry in generated openclaw.json.

OpenClaw's active-memory plugin is a blocking pre-reply recall
sub-agent that injects relevant memory into the main agent's context
before the reply is composed. Docs: ``docs/concepts/active-memory.md``
in the OpenClaw npm package. Schema:
``dist/extensions/active-memory/openclaw.plugin.json``.

Phase 4 enables the plugin behind the canary flag
``experimental_active_memory_enabled``. The plugin depends on
``memory_search`` working, which means
``experimental_memory_core_enabled`` must also be True. The config
generator refuses to emit the plugin entry if memory-core is off and
logs a warning so the canary observes the intended setup explicitly.
"""

from __future__ import annotations

import logging

from django.test import TestCase

from apps.orchestrator.config_generator import (
    _build_active_memory_plugin_entry,
    generate_openclaw_config,
)
from apps.tenants.services import create_tenant


class ActiveMemoryFlagOffTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="AMOff", telegram_chat_id=740001)
        self.assertFalse(self.tenant.experimental_active_memory_enabled)

    def test_helper_returns_none(self):
        self.assertIsNone(_build_active_memory_plugin_entry(self.tenant))

    def test_full_config_omits_active_memory(self):
        config = generate_openclaw_config(self.tenant)
        entries = config.get("plugins", {}).get("entries", {})
        self.assertNotIn("active-memory", entries)


class ActiveMemoryFlagOnButMemoryCoreOffTest(TestCase):
    """Active-memory needs memory-core as its recall backend; config_generator
    must refuse to emit the plugin and log a warning when the precondition
    isn't met."""

    def setUp(self):
        self.tenant = create_tenant(display_name="AMnoMC", telegram_chat_id=740002)
        self.tenant.experimental_active_memory_enabled = True
        self.tenant.experimental_memory_core_enabled = False  # explicit
        self.tenant.save()

    def test_helper_returns_none_and_logs_warning(self):
        with self.assertLogs("apps.orchestrator.config_generator", level=logging.WARNING) as cm:
            entry = _build_active_memory_plugin_entry(self.tenant)
        self.assertIsNone(entry)
        joined = "\n".join(cm.output)
        self.assertIn("active-memory plugin", joined.lower())
        self.assertIn("memory-core", joined.lower())

    def test_full_config_omits_active_memory_when_dependency_missing(self):
        with self.assertLogs("apps.orchestrator.config_generator", level=logging.WARNING):
            config = generate_openclaw_config(self.tenant)
        entries = config.get("plugins", {}).get("entries", {})
        self.assertNotIn("active-memory", entries)


class ActiveMemoryFullyEnabledTest(TestCase):
    """Both flags on — plugin entry must be present with the verified shape."""

    def setUp(self):
        self.tenant = create_tenant(display_name="AMOn", telegram_chat_id=740003)
        self.tenant.experimental_memory_core_enabled = True
        self.tenant.experimental_active_memory_enabled = True
        self.tenant.save()

    def test_helper_returns_validated_shape(self):
        entry = _build_active_memory_plugin_entry(self.tenant)
        self.assertIsNotNone(entry)
        self.assertTrue(entry["enabled"])
        cfg = entry["config"]
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["agents"], ["main"])
        self.assertEqual(cfg["allowedChatTypes"], ["direct"])
        # Enum values per dist/extensions/active-memory/openclaw.plugin.json
        self.assertEqual(cfg["queryMode"], "recent")
        self.assertEqual(cfg["promptStyle"], "balanced")
        # Numeric ranges
        self.assertGreaterEqual(cfg["timeoutMs"], 250)
        self.assertLessEqual(cfg["timeoutMs"], 120_000)
        self.assertGreaterEqual(cfg["setupGraceTimeoutMs"], 0)
        self.assertLessEqual(cfg["setupGraceTimeoutMs"], 30_000)
        self.assertGreater(cfg["maxSummaryChars"], 0)

    def test_full_config_includes_active_memory(self):
        config = generate_openclaw_config(self.tenant)
        entries = config["plugins"]["entries"]
        self.assertIn("active-memory", entries)
        self.assertTrue(entries["active-memory"]["enabled"])
