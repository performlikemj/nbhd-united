"""Tests for re-enabling memory-core with an ephemeral SQLite index.

PR #525 disabled OpenClaw's ``memory_search`` fleet-wide because the
SQLite index lived on the Azure File Share (SMB), where a container
kill mid-write left a 0-byte file. The original ban had two layers:

  - Tool policy denied ``memory_search`` + ``memory_get``
    (``apps/orchestrator/tool_policy.py``)
  - ``memorySearch.enabled`` was hard-coded ``False`` in the generated
    openclaw.json (``apps/orchestrator/config_generator.py``)

Phase 3 unwinds both behind a per-tenant flag
(``experimental_memory_core_enabled``) and adds an ``index-cache``
EmptyDir volume so the SQLite cache lives on container-local
ephemeral storage. Markdown files (the truth) stay on the workspace
share, where line-based content is SMB-safe.

These tests pin:

  - The new 2026.5.7 tool policy DOES allow memory_search/memory_get
  - The old 2026.4.15 policy still denies them (no retroactive change)
  - ``memorySearch`` shape is gated on the tenant flag
  - The ``{agentId}`` token in store.path is left intact for OpenClaw
    to interpolate (per ``dist/memory-search-DbWvVOpI.js:37``)
"""

from __future__ import annotations

from django.test import TestCase

from apps.orchestrator.config_generator import (
    _build_memory_search_config,
    generate_openclaw_config,
)
from apps.orchestrator.tool_policy import (
    OPENCLAW_CURRENT_VERSION,
    get_allowed_tools,
    get_denied_tools,
)
from apps.tenants.services import create_tenant


class MemoryToolPolicyTest(TestCase):
    """The tool policy must allow memory tools on the current OC version
    and keep them denied on the older versions."""

    def test_memory_tools_allowed_on_2026_5_7(self):
        denied = get_denied_tools(version="2026.5.7")
        self.assertNotIn("memory_search", denied)
        self.assertNotIn("memory_get", denied)

    def test_memory_tools_still_denied_on_2026_4_15(self):
        # Older fleet versions (suspended tenants we haven't bumped) MUST
        # keep the deny — the SMB+SQLite hostility was the bug and we
        # haven't shipped the EmptyDir fix to their image yet.
        denied = get_denied_tools(version="2026.4.15")
        self.assertIn("memory_search", denied)
        self.assertIn("memory_get", denied)

    def test_memory_tools_denied_on_2026_4_5(self):
        denied = get_denied_tools(version="2026.4.5")
        # 2026.4.5 predates the memory tool denial — it didn't have these
        # in the deny list because the deny list came in 4.15. Just sanity
        # check that pre-4.15 behavior isn't disturbed.
        self.assertEqual(len(denied), 7)

    def test_current_version_is_5_28(self):
        # If the canary bumps to a newer OC version, the policy registry
        # entries above need re-verification — this test fails so we
        # remember to look. 5.28 reuses the 2026.5.7 policy entry
        # (_resolve_policy returns the newest entry <= version); verified on
        # canary 148ccf1c — the 11-tool tools.deny fired correctly on 5.28.
        self.assertEqual(OPENCLAW_CURRENT_VERSION, "2026.5.28")

    def test_starter_allow_unchanged_across_4_15_to_5_7(self):
        allow_4_15 = set(get_allowed_tools(version="2026.4.15"))
        allow_5_7 = set(get_allowed_tools(version="2026.5.7"))
        self.assertEqual(allow_4_15, allow_5_7)


class MemorySearchConfigOffTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="MemoryOff", telegram_chat_id=730001)
        self.assertFalse(self.tenant.experimental_memory_core_enabled)

    def test_memory_search_disabled(self):
        cfg = _build_memory_search_config(self.tenant)
        self.assertEqual(cfg, {"enabled": False})

    def test_full_config_shape(self):
        config = generate_openclaw_config(self.tenant)
        self.assertFalse(config["agents"]["defaults"]["memorySearch"]["enabled"])
        # Off-path should NOT emit store.path — would be wasted config noise.
        self.assertNotIn("store", config["agents"]["defaults"]["memorySearch"])


class MemorySearchConfigOnTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="MemoryOn", telegram_chat_id=730002)
        self.tenant.experimental_memory_core_enabled = True
        self.tenant.save()

    def test_memory_search_enabled(self):
        cfg = _build_memory_search_config(self.tenant)
        self.assertTrue(cfg["enabled"])

    def test_store_path_under_index_cache_mount(self):
        cfg = _build_memory_search_config(self.tenant)
        path = cfg["store"]["path"]
        # Path must live under the index-cache EmptyDir mount point
        # (/home/node/.openclaw/index) — otherwise SQLite lands on the
        # share and we're back to the PR #525 corruption regime.
        self.assertTrue(
            path.startswith("/home/node/.openclaw/index/"),
            f"store.path must live under the index-cache mount; got {path!r}",
        )

    def test_store_path_preserves_agent_id_token(self):
        # The literal "{agentId}" must reach OpenClaw — it's interpolated
        # at index time per dist/memory-search-DbWvVOpI.js:37
        # (replaceAll("{agentId}", agentId)). A typo like "{agent_id}"
        # would silently write everyone to one file.
        cfg = _build_memory_search_config(self.tenant)
        self.assertIn("{agentId}", cfg["store"]["path"])

    def test_fts_tokenizer_is_trigram(self):
        # Mixed English / Japanese workspace + short technical tokens
        # ("69kg", "RPE 8") — trigram outperforms unicode61 default.
        cfg = _build_memory_search_config(self.tenant)
        self.assertEqual(cfg["store"]["fts"]["tokenizer"], "trigram")

    def test_full_config_shape(self):
        config = generate_openclaw_config(self.tenant)
        ms = config["agents"]["defaults"]["memorySearch"]
        self.assertTrue(ms["enabled"])
        self.assertIn("store", ms)
        self.assertIn("path", ms["store"])
