"""Security audit for generated OpenClaw tenant configs.

Checks NBHD-specific security invariants beyond what the structural
validator catches. Designed to run at provisioning time and during
config updates — logs findings to PlatformIssueLog.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Same patterns as pre-commit-secrets hook and config_validator
_SECRET_RE = re.compile(r"(sk-ant-|sk-or-v1-|sk-proj-|AAAAAAAAAAAAA|tvly-dev-)")


@dataclass
class SecurityFinding:
    """A single security audit finding."""

    severity: str  # "error" or "warning"
    check: str  # short check name for logging
    message: str


def audit_config_security(config: dict[str, Any]) -> list[SecurityFinding]:
    """Audit a generated OpenClaw config for security issues.

    Returns a list of findings. Empty list means the config passes.
    """
    findings: list[SecurityFinding] = []

    # ── Gateway must not be externally accessible ──
    gw = config.get("gateway", {})
    bind = gw.get("bind", "")
    if bind in ("0.0.0.0", "all", ""):
        findings.append(
            SecurityFinding(
                "error",
                "gateway_bind",
                f"Gateway bind is '{bind}' — must be 'loopback' for tenant containers",
            )
        )

    # ── Auth token must be env var reference, not literal ──
    token = gw.get("auth", {}).get("token", "")
    if token and not token.startswith("${"):
        findings.append(
            SecurityFinding(
                "error",
                "gateway_token_literal",
                "Gateway auth token is a literal value — must use ${ENV_VAR} reference",
            )
        )

    # ── Elevated execution must be disabled ──
    elevated = config.get("tools", {}).get("elevated", {})
    if elevated.get("enabled") is not False:
        findings.append(
            SecurityFinding(
                "error",
                "elevated_enabled",
                "Elevated tool execution is enabled — must be disabled for tenant configs",
            )
        )

    # ── Gateway tool must be denied to subscribers ──
    deny = set(config.get("tools", {}).get("deny", []))
    if "gateway" not in deny:
        findings.append(
            SecurityFinding(
                "error",
                "gateway_not_denied",
                "Gateway tool not in deny list — subscribers must not invoke raw gateway tools",
            )
        )

    # ── Plugin allow-list must match entries (no orphans) ──
    plugins = config.get("plugins")
    if plugins is not None:
        allow_set = set(plugins.get("allow", []))
        entries_set = set(plugins.get("entries", {}).keys())
        orphans = allow_set - entries_set
        if orphans:
            findings.append(
                SecurityFinding(
                    "warning",
                    "plugin_orphans",
                    f"Plugin(s) allowed but missing entries: {sorted(orphans)}",
                )
            )

    # ── Scan env block for leaked secrets ──
    env = config.get("env", {})
    for key, value in env.items():
        if isinstance(value, str) and not value.startswith("${") and _SECRET_RE.search(value):
            findings.append(
                SecurityFinding(
                    "error",
                    "env_secret_leak",
                    f"Env var '{key}' contains a literal secret — use Key Vault reference",
                )
            )

    return findings
