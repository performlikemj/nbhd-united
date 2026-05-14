"""Shape contract for generated ``openclaw.json`` configs.

OpenClaw's runtime is forgiving on unknown keys and its redactor masks
schema-validation warnings on stdout/stderr — so a misspelled key or a
wrong-enum value in ``apps/orchestrator/config_generator.py`` can ship
silently and break a feature without surfacing in tests or logs (see
``feedback_openclaw_config_schema_check.md``).

This test pins the *shape* of the keys we explicitly emit, sourced from
``npm pack openclaw@<canary-version>`` schema inspection done at merge
time. It does NOT validate against the full OpenClaw schema — that would
require a Node sidecar in CI. It catches:

  - typos / casing drift in keys we own
  - wrong enum values (e.g. ``promptStyle: "balansed"``)
  - out-of-range numerics
  - regressions where a new tenant flag flips a value to the wrong type

Pre-merge discipline still applies: when adding or changing a key here,
extract the canary's OpenClaw version source via ``npm pack openclaw@<v>``
and grep ``dist/`` to confirm the schema. Then add an assertion below.

Source-of-truth references (OpenClaw 2026.5.7, the canary version at
this test's authoring time) — re-verify when the canary bumps:

  - commitments shape:  ``dist/runtime-schema-OL6hE5dN.js:18704-18711``
  - heartbeat fallback: ``dist/heartbeat-runner-DpQCcYf2.js:365``
  - activeHours shape:  ``dist/heartbeat-runner-DpQCcYf2.js:297-302``
  - memorySearch store: ``dist/memory-search-DbWvVOpI.js:37-42``
                        (``{agentId}`` token IS interpolated)
  - active-memory enums: ``dist/extensions/active-memory/openclaw.plugin.json``
"""

from __future__ import annotations

import re
from typing import Any

from django.test import TestCase

from apps.tenants.services import create_tenant

from .config_generator import generate_openclaw_config

# OpenClaw-documented enum values for the keys we set. Sourced from
# ``npm pack openclaw@2026.5.7`` — see file docstring for paths.
_HEARTBEAT_TARGET_ENUM = {"none", "last"}
_HEARTBEAT_EVERY_PATTERN = re.compile(r"^\d+[smhd]$")  # e.g. "30m", "1h"
_TIME_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")  # 24h HH:MM
_ACTIVE_MEMORY_QUERY_MODES = {"message", "recent", "full"}
_ACTIVE_MEMORY_PROMPT_STYLES = {
    "balanced",
    "strict",
    "contextual",
    "recall-heavy",
    "precision-heavy",
    "preference-only",
}
_ACTIVE_MEMORY_ALLOWED_CHAT_TYPES = {"direct", "group", "channel", "explicit"}
_FTS_TOKENIZER_ENUM = {"unicode61", "trigram"}


def _get(config: dict, dotted: str) -> Any:
    """Walk a dotted path; return ``None`` if any segment is missing."""
    cur: Any = config
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


class OpenclawSchemaShapeTest(TestCase):
    """Generated config must match the verified OpenClaw schema shapes.

    These assertions are intentionally narrow: they only check keys
    ``config_generator.py`` explicitly emits. Anything OpenClaw adds at
    runtime (defaults filled in by the gateway) is out of scope.
    """

    def setUp(self):
        self.tenant = create_tenant(
            display_name="SchemaShape",
            telegram_chat_id=998877,
        )
        self.config = generate_openclaw_config(self.tenant)

    # ── Heartbeat ─────────────────────────────────────────────────────

    def test_heartbeat_every_matches_pattern(self):
        every = _get(self.config, "agents.defaults.heartbeat.every")
        self.assertIsNotNone(every, "agents.defaults.heartbeat.every missing")
        self.assertRegex(every, _HEARTBEAT_EVERY_PATTERN)

    def test_heartbeat_target_is_valid_enum_when_set(self):
        target = _get(self.config, "agents.defaults.heartbeat.target")
        if target is not None:
            self.assertIn(target, _HEARTBEAT_TARGET_ENUM)

    def test_heartbeat_active_hours_shape_when_set(self):
        active = _get(self.config, "agents.defaults.heartbeat.activeHours")
        if active is None:
            return
        self.assertIsInstance(active, dict)
        self.assertIn("start", active)
        self.assertIn("end", active)
        self.assertRegex(active["start"], _TIME_HHMM)
        self.assertRegex(active["end"], _TIME_HHMM)
        if "timezone" in active:
            self.assertIsInstance(active["timezone"], str)

    def test_heartbeat_ack_max_chars_is_nonneg_int_when_set(self):
        ack = _get(self.config, "agents.defaults.heartbeat.ackMaxChars")
        if ack is not None:
            self.assertIsInstance(ack, int)
            self.assertGreaterEqual(ack, 0)

    # ── Commitments ───────────────────────────────────────────────────

    def test_commitments_shape_when_set(self):
        commitments = self.config.get("commitments")
        if commitments is None:
            return
        self.assertIsInstance(commitments, dict)
        if "enabled" in commitments:
            self.assertIsInstance(commitments["enabled"], bool)
        if "maxPerDay" in commitments:
            self.assertIsInstance(commitments["maxPerDay"], int)
            # Plausibility band — OpenClaw default is 3, anything outside
            # 1–50 is almost certainly a bug, not a legitimate config.
            self.assertGreaterEqual(commitments["maxPerDay"], 1)
            self.assertLessEqual(commitments["maxPerDay"], 50)

    # ── memorySearch ──────────────────────────────────────────────────

    def test_memory_search_enabled_is_bool(self):
        enabled = _get(self.config, "agents.defaults.memorySearch.enabled")
        self.assertIsInstance(enabled, bool)

    def test_memory_search_store_path_is_string_when_set(self):
        path = _get(self.config, "agents.defaults.memorySearch.store.path")
        if path is None:
            return
        self.assertIsInstance(path, str)
        # If we use the ``{agentId}`` token, it must be the exact literal
        # OpenClaw replaces — a typo like ``{agent_id}`` would silently
        # leave the literal in the path and write everyone to one file.
        if "{" in path:
            self.assertIn("{agentId}", path, msg=f"unexpected token in {path}")

    def test_memory_search_fts_tokenizer_when_set(self):
        tok = _get(self.config, "agents.defaults.memorySearch.store.fts.tokenizer")
        if tok is not None:
            self.assertIn(tok, _FTS_TOKENIZER_ENUM)

    # ── active-memory plugin ──────────────────────────────────────────

    def test_active_memory_plugin_shape_when_present(self):
        plugin = _get(self.config, "plugins.entries.active-memory")
        if plugin is None:
            return
        self.assertIsInstance(plugin, dict)
        self.assertIn("enabled", plugin)
        self.assertIsInstance(plugin["enabled"], bool)
        cfg = plugin.get("config")
        if cfg is None:
            return
        if "queryMode" in cfg:
            self.assertIn(cfg["queryMode"], _ACTIVE_MEMORY_QUERY_MODES)
        if "promptStyle" in cfg:
            self.assertIn(cfg["promptStyle"], _ACTIVE_MEMORY_PROMPT_STYLES)
        if "allowedChatTypes" in cfg:
            self.assertIsInstance(cfg["allowedChatTypes"], list)
            for v in cfg["allowedChatTypes"]:
                self.assertIn(v, _ACTIVE_MEMORY_ALLOWED_CHAT_TYPES)
        if "timeoutMs" in cfg:
            self.assertIsInstance(cfg["timeoutMs"], int)
            self.assertGreaterEqual(cfg["timeoutMs"], 250)
            self.assertLessEqual(cfg["timeoutMs"], 120_000)
        if "setupGraceTimeoutMs" in cfg:
            self.assertIsInstance(cfg["setupGraceTimeoutMs"], int)
            self.assertGreaterEqual(cfg["setupGraceTimeoutMs"], 0)
            self.assertLessEqual(cfg["setupGraceTimeoutMs"], 30_000)
        if "maxSummaryChars" in cfg:
            self.assertIsInstance(cfg["maxSummaryChars"], int)
            self.assertGreater(cfg["maxSummaryChars"], 0)
        if "agents" in cfg:
            self.assertIsInstance(cfg["agents"], list)
            self.assertTrue(all(isinstance(a, str) for a in cfg["agents"]))

    # ── Sanity: known top-level keys ──────────────────────────────────

    def test_no_unexpected_top_level_keys(self):
        """Guardrail against typos at the top level.

        OpenClaw silently ignores unknown top-level keys, so a typo like
        ``commitmnets`` would compile clean but do nothing. Pin the
        allowlist; if a new key lands in config_generator, this assertion
        forces an explicit decision to add it.
        """
        allowed = {
            "agents",
            "auth",
            "channels",
            "commitments",
            "cron",
            "env",
            "gateway",
            "logging",
            "messages",
            "models",
            "plugins",
            "session",  # session.reset.{mode,idleMinutes} — verified in openclaw@2026.5.7 runtime-schema; added 2026-05-14 (CONTINUITY_workspace-routing-fix.md, Phase 5)
            "telemetry",
            "tools",
            "workspace",
        }
        unexpected = set(self.config.keys()) - allowed
        self.assertFalse(
            unexpected,
            f"Unexpected top-level key(s) in openclaw.json: {sorted(unexpected)}. "
            "Either add them to the allowlist (after npm-pack-verifying the schema) "
            "or fix the typo.",
        )
