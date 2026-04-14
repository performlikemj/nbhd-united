"""Validate generated OpenClaw configs against NBHD-specific invariants.

Catches the class of bugs that shipped in PR #283 (unrecognized config
keys crashed tenant containers). Callable from unit tests, CI smoke
tests, and the provisioning pipeline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Secret patterns — same as pre-commit-secrets hook
_SECRET_RE = re.compile(r"(sk-ant-|sk-or-v1-|sk-proj-|AAAAAAAAAAAAA|tvly-dev-)")

# Tools that all tenants must deny
_REQUIRED_DENIED_TOOLS = {"gateway"}

# Channels where 'capabilities' is NOT a valid key
_CHANNELS_WITHOUT_CAPABILITIES = {"line"}


@dataclass
class ConfigIssue:
    """A single config validation issue."""

    severity: str  # "error" or "warning"
    path: str  # dotted config path, e.g. "gateway.bind"
    message: str


def validate_openclaw_config(
    config: dict[str, Any],
    tier: str = "starter",
) -> list[ConfigIssue]:
    """Validate a generated OpenClaw config dict.

    Returns a list of issues found. Empty list means the config is valid.
    """
    issues: list[ConfigIssue] = []

    # ── Required top-level keys ──
    for key in ("gateway", "channels", "agents", "tools", "cron"):
        if key not in config:
            issues.append(ConfigIssue("error", key, f"Required top-level key '{key}' missing"))

    # ── Gateway security ──
    gw = config.get("gateway", {})
    if gw.get("mode") != "local":
        issues.append(ConfigIssue("error", "gateway.mode", f"Expected 'local', got '{gw.get('mode')}'"))
    if gw.get("bind") != "loopback":
        issues.append(ConfigIssue("error", "gateway.bind", f"Expected 'loopback', got '{gw.get('bind')}'"))

    auth = gw.get("auth", {})
    if auth.get("mode") != "token":
        issues.append(ConfigIssue("error", "gateway.auth.mode", f"Expected 'token', got '{auth.get('mode')}'"))
    token = auth.get("token", "")
    if token and not token.startswith("${"):
        issues.append(
            ConfigIssue(
                "error", "gateway.auth.token", "Token must be an env var reference (${...}), not a literal value"
            )
        )

    # ── Tool policy ──
    tools = config.get("tools", {})
    deny = set(tools.get("deny", []))
    for required in _REQUIRED_DENIED_TOOLS:
        if required not in deny:
            issues.append(ConfigIssue("error", "tools.deny", f"Required denied tool '{required}' missing"))

    elevated = tools.get("elevated", {})
    if elevated.get("enabled") is not False:
        issues.append(
            ConfigIssue("error", "tools.elevated.enabled", "Elevated execution must be disabled for tenant configs")
        )

    # ── Model config ──
    agents = config.get("agents", {})
    defaults = agents.get("defaults", {})
    model = defaults.get("model", {})
    primary = model.get("primary", "")
    if not primary:
        issues.append(ConfigIssue("error", "agents.defaults.model.primary", "Primary model must be set"))

    # ── Plugin wiring consistency ──
    plugins = config.get("plugins")
    if plugins is not None:
        allow_list = set(plugins.get("allow", []))
        entries = set(plugins.get("entries", {}).keys())
        orphan_allow = allow_list - entries
        if orphan_allow:
            issues.append(
                ConfigIssue(
                    "error",
                    "plugins",
                    f"Plugin(s) in allow list but missing from entries: {sorted(orphan_allow)}",
                )
            )
        orphan_entries = entries - allow_list
        if orphan_entries:
            issues.append(
                ConfigIssue(
                    "warning",
                    "plugins",
                    f"Plugin(s) in entries but not in allow list: {sorted(orphan_entries)}",
                )
            )

    # ── Channel config — PR #283 guard ──
    # Some channels (LINE) reject 'capabilities'; Telegram allows it.
    channels = config.get("channels", {})
    for ch_name, ch_config in channels.items():
        if isinstance(ch_config, dict) and "capabilities" in ch_config and ch_name in _CHANNELS_WITHOUT_CAPABILITIES:
            issues.append(
                ConfigIssue(
                    "error",
                    f"channels.{ch_name}.capabilities",
                    f"'{ch_name}' channel rejects 'capabilities' key (PR #283)",
                )
            )

    # ── No bare secrets in config values ──
    _scan_for_secrets(config, "", issues)

    return issues


def _scan_for_secrets(
    obj: Any,
    path: str,
    issues: list[ConfigIssue],
) -> None:
    """Recursively scan config values for secret patterns."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            _scan_for_secrets(value, f"{path}.{key}" if path else key, issues)
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            _scan_for_secrets(value, f"{path}[{i}]", issues)
    elif isinstance(obj, str) and obj.startswith("${"):
        pass  # env var reference, safe
    elif isinstance(obj, str) and _SECRET_RE.search(obj):
        issues.append(
            ConfigIssue(
                "error",
                path,
                "Config value matches secret pattern — use env var reference instead",
            )
        )
