"""Static lock on the BYO Claude CLI tool-deny policy.

`runtime/openclaw/claude-settings.json` is the policy that
`runtime/openclaw/entrypoint.sh` materialises into `~/.claude/settings.json`
inside every BYO tenant container at boot. It denies the native Claude CLI
tools that a prompt-injected assistant would otherwise use to exfiltrate
secrets (Bash → `printenv`, Read → /proc/self/environ, WebFetch → outbound
exfil, Edit/Write → workspace tampering).

These tests catch silent removal of guard rails (refactor, accidental
merge, upstream tool renamed). They don't validate runtime behaviour —
that's tested on the canary after deploy.
"""

from __future__ import annotations

import json
import os

from django.test import SimpleTestCase

CLAUDE_SETTINGS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "runtime",
    "openclaw",
    "claude-settings.json",
)


def _load_settings() -> dict:
    with open(CLAUDE_SETTINGS_PATH) as f:
        return json.load(f)


class ByoClaudeSettingsPolicyTest(SimpleTestCase):
    def test_file_is_valid_json(self):
        cfg = _load_settings()
        self.assertIsInstance(cfg, dict)

    def test_has_permissions_deny_block(self):
        cfg = _load_settings()
        self.assertIn("permissions", cfg)
        self.assertIn("deny", cfg["permissions"])
        self.assertIsInstance(cfg["permissions"]["deny"], list)

    def test_denies_high_risk_tools(self):
        """Bash / Edit / Write / WebFetch / NotebookEdit / MultiEdit are
        the tools a prompt-injected assistant uses to exfil. Must be in
        deny. Read is intentionally NOT fully denied because OpenClaw
        injects workspace context that the assistant needs to read."""
        deny = _load_settings()["permissions"]["deny"]
        for tool in ("Bash", "Edit", "Write", "MultiEdit", "NotebookEdit", "WebFetch"):
            self.assertIn(tool, deny, f"{tool} must be in permissions.deny")

    def test_denies_sensitive_read_paths(self):
        """/proc/self/environ + /etc + credential files are the env-exfil
        paths via Read. Pattern-deny them even though Read itself is
        allowed for workspace context."""
        deny = _load_settings()["permissions"]["deny"]
        required_path_denies = [
            "Read(/proc/**)",
            "Read(/etc/**)",
            "Read(**/.credentials.json)",
        ]
        for pat in required_path_denies:
            self.assertIn(pat, deny, f"{pat} must be in permissions.deny")
