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


class InvalidTenantConfigError(ValueError):
    """A rendered openclaw.json is unsafe to write to a tenant's share.

    Raised by ``assert_config_writable`` (the write-time validation gate in
    ``azure_client.upload_config_to_file_share``) so a schema-invalid config
    never overwrites the tenant's last-good file and bricks its gateway on the
    next boot.

    Context: on 2026-07-05 a config/binary schema skew (stale Apr/Jun configs
    whose ``agents.defaults`` shape the newer OpenClaw binary rejects with
    ``agents.defaults: Invalid input``) crash-looped suspended tenants — the
    gateway (:18789) refuses to start on a schema-invalid config while the proxy
    (:8080) stays up, so Azure health looks green while every message and cron
    dies with ``ECONNREFUSED``. This gate is the code-level guard for that class
    and for any future generator bug that would emit an invalid ``agents.defaults``.
    (The same-day outage of the two *active* friends tenants had a separate root
    cause — a plugin missing from the image; see the Dockerfile/CI packaging guard.)
    """


# Keys the config generator legitimately places under ``agents.defaults``
# (the literal in generate_openclaw_config plus the version-gated additions:
# llm for OC<5.0, cliBackends for BYO, params/contextPruning for >=5.28).
# OpenClaw's own Zod schema rejects UNKNOWN keys here with the generic
# ``agents.defaults: Invalid input`` verdict — so an unrecognized key (from a
# runtime re-serialization, a config migration, or a future generator bug) is
# the highest-signal corruption marker we can check without the binary.
_AGENTS_DEFAULTS_ALLOWED_KEYS = frozenset(
    {
        "model",
        "models",
        "workspace",
        "userTimezone",
        "envelopeTimezone",
        "bootstrapMaxChars",
        "bootstrapTotalMaxChars",
        "compaction",
        "memorySearch",
        "heartbeat",
        "maxConcurrent",
        "subagents",
        "llm",
        "cliBackends",
        "params",
        "contextPruning",
    }
)

# Sub-blocks under agents.defaults that the generator ALWAYS emits as objects.
_AGENTS_DEFAULTS_OBJECT_KEYS = ("compaction", "memorySearch", "heartbeat", "subagents")


def _validate_agents_defaults_strict(config: dict[str, Any], issues: list[ConfigIssue]) -> None:
    """Strict shape check of the Django-owned ``agents.defaults`` block.

    A best-effort Python approximation of OpenClaw's Zod schema for the one
    block that bricked tenants on 2026-07-05. It catches the failure classes
    that produce ``agents.defaults: Invalid input``: a non-object defaults, an
    unrecognized key, a null value, or a malformed ``model`` (empty/absent
    primary, non-list fallbacks). It deliberately does NOT try to mirror every
    nested Zod rule — the binary-backed CI smoke and boot-time doctor cover the
    long tail. The write gate treats any ``error`` here as "keep last-good".
    """
    agents = config.get("agents")
    if not isinstance(agents, dict):
        issues.append(ConfigIssue("error", "agents", "must be an object"))
        return
    defaults = agents.get("defaults")
    if not isinstance(defaults, dict):
        issues.append(ConfigIssue("error", "agents.defaults", "must be an object (Invalid input)"))
        return

    unknown = sorted(set(defaults) - _AGENTS_DEFAULTS_ALLOWED_KEYS)
    if unknown:
        issues.append(
            ConfigIssue(
                "error",
                "agents.defaults",
                f"unrecognized key(s) {unknown} — OpenClaw's schema rejects unknown "
                "agents.defaults keys with 'Invalid input'",
            )
        )

    for key, value in defaults.items():
        if value is None:
            issues.append(ConfigIssue("error", f"agents.defaults.{key}", "must not be null (Invalid input)"))

    model = defaults.get("model")
    if not isinstance(model, dict):
        issues.append(ConfigIssue("error", "agents.defaults.model", "must be an object"))
    else:
        primary = model.get("primary")
        if not isinstance(primary, str) or not primary:
            issues.append(ConfigIssue("error", "agents.defaults.model.primary", "must be a non-empty string"))
        fallbacks = model.get("fallbacks")
        if not isinstance(fallbacks, list) or not all(isinstance(f, str) for f in fallbacks):
            issues.append(ConfigIssue("error", "agents.defaults.model.fallbacks", "must be a list of strings"))

    models = defaults.get("models")
    if models is not None and not isinstance(models, dict):
        issues.append(ConfigIssue("error", "agents.defaults.models", "must be an object"))

    for key in _AGENTS_DEFAULTS_OBJECT_KEYS:
        value = defaults.get(key)
        if value is not None and not isinstance(value, dict):
            issues.append(ConfigIssue("error", f"agents.defaults.{key}", "must be an object"))


def assert_config_writable(config: dict[str, Any], tier: str = "starter") -> None:
    """Write-time gate: raise ``InvalidTenantConfigError`` on any error-severity issue.

    Runs the strict validation and turns error-severity issues into a raise so
    the caller (``upload_config_to_file_share``) refuses the write and keeps the
    tenant's last-good config on the share. Warnings do not block.
    """
    issues = validate_openclaw_config(config, tier=tier, strict=True)
    errors = [i for i in issues if i.severity == "error"]
    if errors:
        raise InvalidTenantConfigError("; ".join(f"{e.path}: {e.message}" for e in errors))


def validate_openclaw_config(
    config: dict[str, Any],
    tier: str = "starter",
    *,
    strict: bool = False,
) -> list[ConfigIssue]:
    """Validate a generated OpenClaw config dict.

    Returns a list of issues found. Empty list means the config is valid.

    ``strict=True`` adds a shape check of the Django-owned ``agents.defaults``
    block (unknown keys, null values, malformed ``model``) — the class of defect
    that fails OpenClaw's own Zod schema with ``agents.defaults: Invalid input``
    but which the loose default checks below miss. Used by the write-time gate
    and the CI binary smoke; kept opt-in so existing callers are unaffected.
    """
    issues: list[ConfigIssue] = []

    if strict:
        _validate_agents_defaults_strict(config, issues)

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
    # Defensive: the write gate runs this on arbitrary (possibly corrupted)
    # content, so tolerate non-dict shapes without raising — the strict pass
    # above already reports them as errors.
    agents = config.get("agents", {})
    defaults = agents.get("defaults", {}) if isinstance(agents, dict) else {}
    model = defaults.get("model", {}) if isinstance(defaults, dict) else {}
    primary = model.get("primary", "") if isinstance(model, dict) else ""
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
