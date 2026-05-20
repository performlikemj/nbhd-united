"""Chat session continuity contract — post workspace-routing removal.

Workspace-based chat routing was removed 2026-05-20 — see
docs/implementation/remove-workspace-chat-routing.md. The chat path must
build user_param = chat_id / line_user_id directly so OpenClaw routes
consecutive messages from the same user into one continuous session,
regardless of message content or tenant workspace state.

These tests assert the contract surface — that the auto-classifier
module is gone, the test-view URL routes are gone, and the chat path
source no longer constructs `:ws:` suffixes or imports the classifier.

Cron-fired session isolation lives at config level (sessionTarget
="isolated" + isolatedSession=True on each cron job entry) and is
covered by existing tests in apps/orchestrator/.
"""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path

from django.test import TestCase


class WorkspaceRoutingModuleRemovedTest(TestCase):
    """The auto-classifier module and its temporary test view are deleted."""

    def test_workspace_routing_module_removed(self):
        with self.assertRaises(ModuleNotFoundError):
            importlib.import_module("apps.router.workspace_routing")

    def test_test_workspace_sessions_view_removed(self):
        with self.assertRaises(ModuleNotFoundError):
            importlib.import_module("apps.router.test_workspace_sessions")


class ChatPathFlatUserParamTest(TestCase):
    """The chat entrypoints must not reintroduce workspace_routing imports
    or :ws: suffix construction.

    Defensive source-level guard against accidental re-coupling of chat
    routing to workspaces. The end-to-end forwarding behavior is covered
    by ``apps/router/test_poller.py`` and ``apps/router/tests_line.py``.
    """

    def _read_module_source(self, dotted: str) -> str:
        module = importlib.import_module(dotted)
        return Path(inspect.getfile(module)).read_text()

    def test_poller_does_not_import_workspace_routing(self):
        source = self._read_module_source("apps.router.poller")
        self.assertNotIn(
            "workspace_routing",
            source,
            "apps/router/poller.py must not import workspace_routing — chat sessionKey is flat per user.",
        )

    def test_poller_does_not_construct_ws_suffix(self):
        source = self._read_module_source("apps.router.poller")
        self.assertNotIn(
            ":ws:",
            source,
            "apps/router/poller.py must not build user_param with a :ws: suffix.",
        )

    def test_line_webhook_does_not_import_workspace_routing(self):
        source = self._read_module_source("apps.router.line_webhook")
        self.assertNotIn(
            "workspace_routing",
            source,
            "apps/router/line_webhook.py must not import workspace_routing.",
        )

    def test_line_webhook_does_not_construct_ws_suffix(self):
        source = self._read_module_source("apps.router.line_webhook")
        self.assertNotIn(
            ":ws:",
            source,
            "apps/router/line_webhook.py must not build user_param with a :ws: suffix.",
        )


class RoutingContextPluginHasNoCatalogueHookTest(TestCase):
    """The nbhd-routing-context plugin's before_prompt_build hook (workspace
    catalogue injection) was removed alongside chat routing. Only the
    degenerate-output guard remains."""

    def _plugin_source(self) -> str:
        path = (
            Path(__file__).resolve().parents[2]
            / "runtime"
            / "openclaw"
            / "plugins"
            / "nbhd-routing-context"
            / "index.js"
        )
        return path.read_text() if path.exists() else ""

    def test_plugin_does_not_register_before_prompt_build(self):
        source = self._plugin_source()
        if not source:
            self.skipTest("plugin source not present in worktree")
        # Match the actual hook registration call shape, not stray
        # mentions in comments / docstrings explaining the removal.
        self.assertNotIn(
            'api.on("before_prompt_build"',
            source,
            "nbhd-routing-context must not register before_prompt_build — workspace catalogue injection was removed.",
        )

    def test_plugin_still_guards_degenerate_output(self):
        source = self._plugin_source()
        if not source:
            self.skipTest("plugin source not present in worktree")
        self.assertIn("before_agent_finalize", source)
        self.assertIn("message_sending", source)


class JournalToolsHasNoWorkspaceSwitchTest(TestCase):
    """nbhd_workspace_switch was removed from the agent's tool surface."""

    def _plugin_source(self) -> str:
        path = (
            Path(__file__).resolve().parents[2] / "runtime" / "openclaw" / "plugins" / "nbhd-journal-tools" / "index.js"
        )
        return path.read_text() if path.exists() else ""

    def test_workspace_switch_tool_removed(self):
        source = self._plugin_source()
        if not source:
            self.skipTest("plugin source not present in worktree")
        # The comment trail mentioning removal is allowed; the tool name
        # registered with registerTool must not be.
        self.assertNotIn(
            'name: "nbhd_workspace_switch"',
            source,
            "nbhd_workspace_switch must not be registered as a tool.",
        )
