"""OpenClaw tool policy for subscriber tenants.

Policy intentionally uses documented config keys:
- tools.allow
- tools.deny
- tools.elevated

Version-aware: tool groups changed in OpenClaw 2026.4.15
(group:automation folded into group:openclaw).
"""

from __future__ import annotations

from typing import Any

# ── Single source of truth for the current fleet version ────────────
# Bump this constant + Dockerfile.openclaw ARG when rolling out a new
# OpenClaw release.  Everything else (model default, config fallback,
# function defaults, tests) imports this value.
OPENCLAW_CURRENT_VERSION = "2026.5.28"


def _parse_version(v: str) -> tuple[int, ...]:
    """Parse 'YYYY.M.D' version string into a comparable tuple."""
    return tuple(int(x) for x in v.split("."))


def openclaw_version_for_image_tag(image_tag: str) -> str:
    """Map a container image tag to the OpenClaw version it ships.

    Image tags are ``<version>-<sha>`` (e.g. ``2026.5.28-755d789``). The
    config generator keys its config SCHEMA off ``tenant.openclaw_version``
    (the agents.defaults.llm vs params/contextPruning split at the 5.0 / 5.28
    boundaries). If that field drifts behind the running image, generated
    configs use an older schema the image rejects (``agents.defaults: Invalid
    input``) and the container crash-loops. Any code path that changes a
    tenant's image MUST also update ``openclaw_version`` via this mapping so
    the two stay in lockstep (mirrors ``bump_openclaw_version_for_tenant``).

    Bare-sha / ``latest`` tags carry no version prefix → fall back to
    ``OPENCLAW_CURRENT_VERSION`` (the version shipped by the current fleet
    image, which is what those tags resolve to in practice).
    """
    import re

    match = re.match(r"^(\d{4}\.\d+\.\d+)", image_tag or "")
    return match.group(1) if match else OPENCLAW_CURRENT_VERSION


# ── 2026.4.5 policy (original) ──────────────────────────────────────

_DENIED_TOOLS_2026_4_5: tuple[str, ...] = (
    "gateway",
    "sessions_spawn",
    "sessions_send",
    "sessions_list",
    "sessions_history",
    "session_status",
    "agents_list",
)

_STARTER_ALLOW_2026_4_5: tuple[str, ...] = (
    "group:web",
    "group:plugins",
    "group:automation",
    "tts",
    "image",
)

# ── 2026.4.15 policy ────────────────────────────────────────────────
# group:automation folded into group:openclaw; expanded deny list
# for tools with no surface in Telegram-only containers.

_DENIED_TOOLS_2026_4_15: tuple[str, ...] = _DENIED_TOOLS_2026_4_5 + (
    "sessions_yield",
    "subagents",
    "message",
    "browser",
    "canvas",
    "nodes",
    "code_execution",
    "music_generate",
    "video_generate",
    # OpenClaw built-in memory tools — denied because they back onto the
    # per-tenant SQLite at memory/main.sqlite on Azure File Share, which
    # corrupts on container kill mid-write. Memory routes through Django:
    # nbhd_memory_get / nbhd_memory_update for direct access,
    # nbhd_journal_search for search.
    "memory_search",
    "memory_get",
)

_STARTER_ALLOW_2026_4_15: tuple[str, ...] = (
    "group:openclaw",
    "group:plugins",
)

# ── 2026.5.7 policy ─────────────────────────────────────────────────
# Restores ``memory_search`` and ``memory_get`` to the allow surface
# alongside the architecture change that moves the SQLite index off
# the Azure File Share (see ``apps/orchestrator/azure_client.py`` —
# ``index-cache`` EmptyDir mount) and gates ``memorySearch.enabled``
# per tenant via ``experimental_memory_core_enabled``. Tools allowed
# fleet-wide are harmless when ``memorySearch.enabled`` is false on
# a given tenant — the gateway returns "memory search disabled" if
# the agent calls them anyway.

_DENIED_TOOLS_2026_5_7: tuple[str, ...] = tuple(
    t for t in _DENIED_TOOLS_2026_4_15 if t not in ("memory_search", "memory_get")
)

_STARTER_ALLOW_2026_5_7: tuple[str, ...] = _STARTER_ALLOW_2026_4_15

# ── 2026.5.28 policy ────────────────────────────────────────────────
# Adds the built-in ``pdf`` tool to the allow surface so an app-uploaded PDF
# (chat ingress, [Document attached: <path>] marker) is readable via the tool's
# native + PDFium extraction-fallback modes. ``pdf`` is NOT a member of
# ``group:openclaw`` or ``group:plugins`` (verified against the OpenClaw
# POLICY_TOOL_GROUPS), so it must be granted by name. Tool availability ALSO
# requires a resolvable PDF-capable model — see ``config_generator`` where
# ``agents.defaults.pdfModel`` is pinned (the pdf factory-availability check has
# no ``modelHasVision`` fast-path, unlike the ``image`` tool).
#
# Also denies ``web_fetch`` (docs/upload-security-threat-model.md P0-0/P0-0b).
# ``web_fetch`` is a member of ``group:openclaw`` (verified against the
# extracted OpenClaw 2026.5.28 ``POLICY_TOOL_GROUPS``) and registers by
# default — its own ``enabled`` flag defaults to ``true`` and NBHD never sets
# ``tools.web.fetch.enabled``/``.provider``, so a keyless bundled provider
# auto-activates fleet-wide with zero API keys configured. On a
# document-injection turn (AC-1 in the threat model) that's a zero-click GET
# with tenant data in the query string to an attacker-controlled URL —
# wrapping the tool's *response* via ``wrapExternalContent`` does nothing to
# stop the outbound request itself. The only production dependency was the
# morning-briefing weather step, rerouted to ``web_search`` (already
# allowed, already wrapped) in ``config_generator._MORNING_BRIEFING_PROMPT_TEMPLATE``
# — see that template for the graceful-degradation handling.

_DENIED_TOOLS_2026_5_28: tuple[str, ...] = _DENIED_TOOLS_2026_5_7 + ("web_fetch",)

_STARTER_ALLOW_2026_5_28: tuple[str, ...] = _STARTER_ALLOW_2026_5_7 + ("pdf",)

# ── Version registry (newest first) ─────────────────────────────────

_POLICY_VERSIONS: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = [
    ("2026.5.28", _STARTER_ALLOW_2026_5_28, _DENIED_TOOLS_2026_5_28),
    ("2026.5.7", _STARTER_ALLOW_2026_5_7, _DENIED_TOOLS_2026_5_7),
    ("2026.4.15", _STARTER_ALLOW_2026_4_15, _DENIED_TOOLS_2026_4_15),
    ("2026.4.5", _STARTER_ALLOW_2026_4_5, _DENIED_TOOLS_2026_4_5),
]

# ── Backward-compat aliases (imported by existing tests) ────────────

DENIED_TOOLS = _DENIED_TOOLS_2026_4_5
STARTER_ALLOW = _STARTER_ALLOW_2026_4_5


def _resolve_policy(version: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return (allow, deny) for the given OpenClaw version."""
    v = _parse_version(version)
    for entry_version, allow, deny in _POLICY_VERSIONS:
        if v >= _parse_version(entry_version):
            return allow, deny
    # Fallback to oldest known policy
    return _POLICY_VERSIONS[-1][1], _POLICY_VERSIONS[-1][2]


def get_allowed_tools(tier: str = "starter", version: str = OPENCLAW_CURRENT_VERSION) -> list[str]:
    """Return documented allow-list entries for a subscriber tier."""
    allow, _ = _resolve_policy(version)
    return list(allow)


def get_denied_tools(version: str = OPENCLAW_CURRENT_VERSION) -> list[str]:
    """Return the deny-list for the given OpenClaw version."""
    _, deny = _resolve_policy(version)
    return list(deny)


def generate_tool_config(tier: str = "starter", version: str = OPENCLAW_CURRENT_VERSION) -> dict[str, Any]:
    """Generate the OpenClaw `tools` config block for subscriber tenants."""
    return {
        "allow": get_allowed_tools(tier, version=version),
        "deny": get_denied_tools(version=version),
        # Prevent host-elevated execution for subscriber agents.
        "elevated": {
            "enabled": False,
        },
        # Keep web search explicitly enabled for deterministic behavior.
        "web": {
            "search": {
                "enabled": True,
            },
        },
    }
