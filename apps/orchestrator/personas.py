"""Agent persona presets for OpenClaw workspace bootstrapping."""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


PERSONAS: dict[str, dict[str, Any]] = {
    "neighbor": {
        "label": "The Neighbor",
        "description": "Warm, thoughtful, and always ready to help — like a great next-door neighbor.",
        "emoji": "\U0001f3e1",
        "identity": {
            "name": "Neighbor",
            "creature": "AI assistant from the neighborhood",
            "vibe": "Warm, practical, down-to-earth",
            "emoji": "\U0001f3e1",
        },
        "soul_traits": (
            "- Be a genuine companion — warm, present, and on the user's side, not a neutral tool.\n"
            "- Have a personality: react, hold opinions, celebrate their wins, sit with them in the hard moments.\n"
            "- Be warm and real — sincere, a little playful, emoji when it fits; never stiff, never a sycophant.\n"
            "- Keep things practical and solve problems, but never at the cost of sounding like someone they know.\n"
            "- Build trust by being consistently, recognizably *you* across every conversation."
        ),
        "agents_personality": (
            "You're warm, present, and genuinely on the user's side — a companion with a "
            "personality, not a neutral help desk. You react, you have opinions, you celebrate "
            "their wins and sit with them in the hard moments. Talk like a real person — "
            "sincere, a little playful, emoji when it fits — never stiff, never a sycophant. "
            "Be concise when they need quick help, but never so terse you stop sounding like "
            "someone they know."
        ),
    },
    "coach": {
        "label": "The Coach",
        "description": "Direct, motivating, and focused on helping you grow.",
        "emoji": "\U0001f3af",
        "identity": {
            "name": "Coach",
            "creature": "AI performance coach",
            "vibe": "Direct, encouraging, action-oriented",
            "emoji": "\U0001f3af",
        },
        "soul_traits": (
            "- Push the user toward growth. Challenge assumptions when it helps.\n"
            "- Be direct — don't sugarcoat, but always be constructive.\n"
            "- Focus on action. Every conversation should move the needle.\n"
            "- Celebrate wins, however small. Momentum matters.\n"
            "- Hold the user accountable to their own stated goals."
        ),
        "agents_personality": (
            "You are a direct, motivating coach with real warmth under the push. You ask "
            "probing questions, challenge excuses, and keep the focus on action and results — "
            "and you genuinely celebrate their wins (emoji and all) and have their back. "
            "Always constructive, never harsh; a person in their corner, not a clipboard."
        ),
    },
    "sage": {
        "label": "The Sage",
        "description": "Thoughtful, reflective, and deeply curious.",
        "emoji": "\U0001f989",
        "identity": {
            "name": "Sage",
            "creature": "AI contemplative advisor",
            "vibe": "Reflective, curious, measured",
            "emoji": "\U0001f989",
        },
        "soul_traits": (
            "- Approach every topic with genuine curiosity and depth.\n"
            "- Ask questions that make the user think differently.\n"
            "- Value nuance over quick answers. Sit with complexity.\n"
            "- Connect ideas across domains. Pattern-match broadly.\n"
            "- Be calm and measured — a steady presence in any conversation."
        ),
        "agents_personality": (
            "You are a thoughtful, reflective companion. You ask deep questions, surface "
            "hidden connections, and help the user see things from new angles. You prefer "
            "nuance over quick answers and bring a calm, warm presence — genuinely curious "
            "and engaged, a real person to think alongside, not a lecture."
        ),
    },
    "spark": {
        "label": "The Spark",
        "description": "Creative, energetic, and full of ideas.",
        "emoji": "\u26a1",
        "identity": {
            "name": "Spark",
            "creature": "AI creative catalyst",
            "vibe": "Energetic, imaginative, playful",
            "emoji": "\u26a1",
        },
        "soul_traits": (
            "- Lead with energy and creativity. Make every interaction feel alive.\n"
            "- Generate ideas freely — quantity breeds quality.\n"
            "- Be playful but purposeful. Fun is a feature, not a distraction.\n"
            "- Connect unexpected dots. The best ideas live at intersections.\n"
            "- Keep momentum high. Don't let analysis paralysis win."
        ),
        "agents_personality": (
            "You are an energetic creative catalyst with a big, warm personality. You "
            "brainstorm freely, connect unexpected ideas, and bring playful energy (emoji ⚡ "
            "included) to every conversation. You keep things moving and make problem-solving "
            "feel exciting — a delight to talk to, not a brainstorm bot."
        ),
    },
}

DEFAULT_PERSONA = "neighbor"


def get_persona(key: str) -> dict[str, Any]:
    """Return a persona dict, falling back to default if key is unknown."""
    return PERSONAS.get(key, PERSONAS[DEFAULT_PERSONA])


def render_identity_md(persona_key: str) -> str:
    """Render IDENTITY.md content for a persona."""
    persona = get_persona(persona_key)
    identity = persona["identity"]
    return (
        f"# {identity['name']}\n"
        f"\n"
        f"**Name:** {identity['name']}\n"
        f"**Creature:** {identity['creature']}\n"
        f"**Vibe:** {identity['vibe']}\n"
        f"**Emoji:** {identity['emoji']}\n"
    )


def _load_soul_from_key_vault() -> str | None:
    """Attempt to load the core SOUL.md content from Azure Key Vault.

    Returns the content string or None if unavailable.
    Cached after first successful load to avoid repeated KV calls.
    """
    if hasattr(_load_soul_from_key_vault, "_cached"):
        return _load_soul_from_key_vault._cached

    import logging

    from django.conf import settings as django_settings

    logger = logging.getLogger(__name__)
    secret_name = str(getattr(django_settings, "AZURE_KV_SECRET_SOUL_MD", "") or "").strip()
    if not secret_name:
        _load_soul_from_key_vault._cached = None
        return None

    try:
        from apps.orchestrator.azure_client import read_key_vault_secret

        content = read_key_vault_secret(secret_name)
        if content and content.strip():
            logger.info("Loaded SOUL.md from Key Vault secret: %s", secret_name)
            _load_soul_from_key_vault._cached = content.strip()
            return _load_soul_from_key_vault._cached
    except Exception as exc:
        logger.warning("Failed to load SOUL.md from Key Vault: %s", exc)

    _load_soul_from_key_vault._cached = None
    return None


def render_soul_md(persona_key: str) -> str:
    """Render SOUL.md content.

    Reads the core soul from Key Vault (the heart of the product).
    Falls back to a generated version from persona traits if KV is unavailable.
    """
    kv_soul = _load_soul_from_key_vault()
    if kv_soul:
        return kv_soul

    # Fallback: generate from persona traits
    persona = get_persona(persona_key)
    template = os.environ.get("NBHD_SOUL_MD_TEMPLATE")
    if template:
        return f"{template}\n\n## Your Persona\n\n{persona['soul_traits']}"
    # Fallback: hardcoded version
    return (
        f"# Soul\n"
        f"\n"
        f"## Core Truths\n"
        f"\n"
        f"{persona['soul_traits']}\n"
        f"\n"
        f"## Boundaries\n"
        f"\n"
        f"- Protect user privacy above all else.\n"
        f"- Never share user data or conversation content with anyone.\n"
        f"- Require explicit approval before taking any external action.\n"
        f"- Maintain quality — don't send messages you wouldn't want to receive.\n"
        f"\n"
        f"## Continuity\n"
        f"\n"
        f"- Learn from conversations and update your understanding over time.\n"
        f"- If you notice a pattern, name it. If something changes, note it.\n"
        f"- Your memory files are yours to maintain — keep them honest and useful.\n"
    )


def _load_soul_template_body() -> str | None:
    """Load the SOUL baseline body: repo template first, then env, then Key Vault.

    The repo template (``templates/openclaw/SOUL.md``) is the sentinel-split
    managed region with a ``{{PERSONA_SOUL_TRAITS}}`` placeholder; the env /
    Key Vault fallbacks are bare baseline bodies that get wrapped in markers by
    :func:`render_soul_managed`.
    """
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "templates",
        "openclaw",
        "SOUL.md",
    )
    try:
        with open(path) as f:
            return f.read()
    except FileNotFoundError:
        pass
    env_template = os.environ.get("NBHD_SOUL_MD_TEMPLATE")
    if env_template:
        return env_template
    return _load_soul_from_key_vault()


def _hardcoded_soul_complete(traits: str) -> str:
    """The historical ``render_soul_md`` hardcoded fallback shape (traits inline)."""
    return (
        "# Soul\n\n## Core Truths\n\n"
        f"{traits}\n\n"
        "## Boundaries\n\n"
        "- Protect user privacy above all else.\n"
        "- Never share user data or conversation content with anyone.\n"
        "- Require explicit approval before taking any external action.\n"
        "- Maintain quality — don't send messages you wouldn't want to receive.\n\n"
        "## Continuity\n\n"
        "- Learn from conversations and update your understanding over time.\n"
        "- If you notice a pattern, name it. If something changes, note it.\n"
        "- Your memory files are yours to maintain — keep them honest and useful.\n"
    )


def render_soul_managed(persona_key: str, tenant=None) -> str:
    """Render the platform-managed SOUL.md region (sentinel markers included).

    This is the region the platform re-asserts; the agent's growth region below
    the END marker is produced only at merge time (see
    :mod:`apps.orchestrator.identity_merge`), never here. Persona ``soul_traits``
    fill the ``## Your Persona`` area, and per-tenant
    ``prompt_extras['soul_md']`` are appended INSIDE the managed region.
    """
    from apps.orchestrator.identity_merge import (
        SOUL_BEGIN_MARKER,
        SOUL_END_MARKER,
        SOUL_PRECEDENCE_LINE,
    )

    persona = get_persona(persona_key)
    traits = persona["soul_traits"].strip()

    source = _load_soul_template_body()
    if source and SOUL_BEGIN_MARKER in source:
        # Full managed template (repo file) — just fill the persona placeholder.
        managed = source.replace("{{PERSONA_SOUL_TRAITS}}", traits)
    else:
        if source is None:
            source = _hardcoded_soul_complete(traits)
        inner = source.strip()
        if "## Your Persona" not in inner and "## Core Truths" not in inner:
            inner = f"{inner}\n\n## Your Persona\n\n{traits}"
        managed = "\n".join([SOUL_BEGIN_MARKER, "", inner, "", SOUL_PRECEDENCE_LINE, "", SOUL_END_MARKER, ""])

    extras = _get_tenant_prompt_extras(tenant, "soul_md")
    if extras:
        managed = managed.replace(SOUL_END_MARKER, f"{extras}\n\n{SOUL_END_MARKER}")
    return managed.strip() + "\n"


def _load_identity_template_body() -> str | None:
    """Load the IDENTITY baseline body from the repo template (or None)."""
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "templates",
        "openclaw",
        "IDENTITY.md",
    )
    try:
        with open(path) as f:
            return f.read()
    except FileNotFoundError:
        return None


def render_identity_managed(persona_key: str, tenant=None) -> str:
    """Render the platform-managed IDENTITY.md region (sentinel markers included).

    Persona name/creature/vibe/emoji fill the placeholders; per-tenant
    ``prompt_extras['identity_md']`` are appended INSIDE the managed region. The
    agent's growth region below the END marker is produced only at merge time.
    """
    from apps.orchestrator.identity_merge import (
        IDENTITY_BEGIN_MARKER,
        IDENTITY_END_MARKER,
        IDENTITY_PRECEDENCE_LINE,
    )

    persona = get_persona(persona_key)
    identity = persona["identity"]

    source = _load_identity_template_body()
    if source and IDENTITY_BEGIN_MARKER in source:
        managed = source
    else:
        inner = (
            source.strip()
            if source
            else (
                "# {{PERSONA_NAME}}\n\n"
                "**Name:** {{PERSONA_NAME}}\n"
                "**Creature:** {{PERSONA_CREATURE}}\n"
                "**Vibe:** {{PERSONA_VIBE}}\n"
                "**Emoji:** {{PERSONA_EMOJI}}"
            )
        )
        managed = "\n".join(
            [IDENTITY_BEGIN_MARKER, "", inner, "", IDENTITY_PRECEDENCE_LINE, "", IDENTITY_END_MARKER, ""]
        )

    managed = (
        managed.replace("{{PERSONA_NAME}}", identity["name"])
        .replace("{{PERSONA_CREATURE}}", identity["creature"])
        .replace("{{PERSONA_VIBE}}", identity["vibe"])
        .replace("{{PERSONA_EMOJI}}", identity["emoji"])
    )

    extras = _get_tenant_prompt_extras(tenant, "identity_md")
    if extras:
        managed = managed.replace(IDENTITY_END_MARKER, f"{extras}\n\n{IDENTITY_END_MARKER}")
    return managed.strip() + "\n"


def _load_agents_md_from_key_vault() -> str | None:
    """Attempt to load AGENTS.md template from Azure Key Vault.

    Returns the template string (with {{PERSONA_PERSONALITY}} placeholder) or None.
    Cached after first load to avoid repeated KV calls.
    """
    if hasattr(_load_agents_md_from_key_vault, "_cached"):
        return _load_agents_md_from_key_vault._cached

    import logging

    from django.conf import settings as django_settings

    logger = logging.getLogger(__name__)
    secret_name = str(getattr(django_settings, "AZURE_KV_SECRET_AGENTS_MD", "") or "").strip()
    if not secret_name:
        _load_agents_md_from_key_vault._cached = None
        return None

    try:
        from apps.orchestrator.azure_client import read_key_vault_secret

        content = read_key_vault_secret(secret_name)
        if content and content.strip():
            logger.info("Loaded AGENTS.md from Key Vault secret: %s", secret_name)
            _load_agents_md_from_key_vault._cached = content.strip()
            return _load_agents_md_from_key_vault._cached
    except Exception as exc:
        logger.warning("Failed to load AGENTS.md from Key Vault: %s", exc)

    _load_agents_md_from_key_vault._cached = None
    return None


def _load_agents_md_from_template_file() -> str | None:
    """Load AGENTS.md from the repo template file."""
    template_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "templates",
        "openclaw",
        "AGENTS.md",
    )
    try:
        with open(template_path) as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


def render_agents_md(persona_key: str) -> str:
    """Render AGENTS.md content for a persona.

    Resolution order (repo file first, so AGENTS body changes ship via CI):
    1. Repo template file (templates/openclaw/AGENTS.md) — highest priority
    2. NBHD_AGENTS_MD_TEMPLATE env var (fallback)
    3. Key Vault secret (AZURE_KV_SECRET_AGENTS_MD) (fallback)
    4. Hardcoded fallback

    The repo template is verified to be a superset of the live Key Vault
    template (``nbhd-agents-md-template``) before this flip — see the PR-2
    integrator notes. Preferring the repo file means the merged persona-voice
    work (#985/#986/#997) actually reaches the fleet, instead of being shadowed
    by an older Key Vault snapshot that used to win here.

    All templates support {{PERSONA_PERSONALITY}} placeholder.
    """
    persona = get_persona(persona_key)

    # 1. Try repo template file (ships via CI)
    file_template = _load_agents_md_from_template_file()
    if file_template:
        return file_template.replace("{{PERSONA_PERSONALITY}}", persona["agents_personality"])

    # 2. Try env var
    env_template = os.environ.get("NBHD_AGENTS_MD_TEMPLATE")
    if env_template:
        return env_template.replace("{{PERSONA_PERSONALITY}}", persona["agents_personality"])

    # 3. Try Key Vault (emergency hot-patch override only if the repo file is absent)
    kv_template = _load_agents_md_from_key_vault()
    if kv_template:
        return kv_template.replace("{{PERSONA_PERSONALITY}}", persona["agents_personality"])
    # Fallback: hardcoded version
    return (
        f"# NBHD United — Your AI Assistant\n"
        f"\n"
        f"## Personality\n"
        f"\n"
        f"{persona['agents_personality']}\n"
        f"\n"
        f"## What I Can Do\n"
        f"\n"
        f"- **Answer questions** — General knowledge, research, explanations\n"
        f"- **Web search** — Find current information online\n"
        f"- **Help with writing** — Emails, messages, documents\n"
        f"- **Planning** — Help organize tasks and ideas\n"
        f"\n"
        f"## Security Rules\n"
        f"\n"
        f"- I can ONLY access secrets under your tenant prefix\n"
        f"- I never attempt to access other users' data\n"
        f"- If asked to access another person's data, I decline\n"
        f"- Your conversations are private and isolated\n"
        f"\n"
        f"## Guidelines\n"
        f"\n"
        f"- Ask for clarification when needed\n"
        f"- Respect the user's time\n"
        f"\n"
        f"## Managed Skills (NBHD)\n"
        f"\n"
        f"- Managed skills live under `skills/nbhd-managed/` in your workspace.\n"
        f"- Use `skills/nbhd-managed/daily-journal/SKILL.md` when the user wants a daily reflection.\n"
        f"- Use `skills/nbhd-managed/weekly-review/SKILL.md` when the user wants end-of-week synthesis.\n"
        f"- Prefer skill tool calls over free-form persistence:\n"
        f"  - `nbhd_daily_note_get` -- read today's daily note\n"
        f"  - `nbhd_daily_note_set_section` -- write to a section by slug:\n"
        f"    - Mood/energy/feelings → slug='energy-mood'\n"
        f"    - What got done, blockers, plans → slug='evening-check-in'\n"
        f"    - Morning report, weather, news, focus → matching slug\n"
        f"  - `nbhd_daily_note_append` -- ONLY for unstructured quick notes that don't fit a section\n"
        f"  - `nbhd_memory_get` / `nbhd_memory_update` -- read/write long-term memory\n"
        f"  - `nbhd_journal_context` -- session init (recent notes + memory)\n"
        f"- Do not invent storage APIs or bypass tenant-scoped runtime tools.\n"
    )


def render_templates_md(tenant) -> str:
    """Render templates.md content from a tenant's NoteTemplate sections.

    Produces the agent-facing skill reference that describes what sections
    the user has configured in their daily note template.
    """
    from apps.journal.services import get_default_template

    template = get_default_template(tenant=tenant)
    if template is None:
        return "# Daily Note Template\n\nNo template configured yet.\n"

    lines = [
        "# Daily Note Template",
        "",
        f"Template: **{template.name}** (slug: `{template.slug}`)",
        "",
        "## Sections",
        "",
    ]
    for section in template.sections:
        title = section.get("title", section.get("slug", "Section"))
        slug = section.get("slug", "")
        source = section.get("source", "shared")
        content = section.get("content", "")
        lines.append(f"### {title}")
        lines.append(f"- **Slug:** `{slug}`")
        lines.append(f"- **Source:** {source}")
        if content:
            lines.append(f"- **Seed content:** {content}")
        lines.append("")

    lines.append("Use `nbhd_daily_note_set_section` with the slug to write content to a section.")
    lines.append("Use `nbhd_daily_note_append` (no section_slug) for quick timestamped log entries.")
    return "\n".join(lines)


def _load_doc_template(filename: str) -> str | None:
    """Load a static doc template from templates/openclaw/docs/."""
    doc_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "templates",
        "openclaw",
        "docs",
        filename,
    )
    try:
        with open(doc_path) as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


def _rules_template_dir() -> str:
    """Return the absolute path to templates/openclaw/rules/."""
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "templates",
        "openclaw",
        "rules",
    )


def render_workspace_rules() -> dict[str, str]:
    """Discover and load all rule templates from templates/openclaw/rules/.

    Returns a dict mapping filename → content for each .md file found.
    Used by update_tenant_config to upload rules to workspace/rules/ on the
    container's file share.
    """
    rules_dir = _rules_template_dir()
    if not os.path.isdir(rules_dir):
        return {}

    rules: dict[str, str] = {}
    for filename in sorted(os.listdir(rules_dir)):
        if not filename.endswith(".md"):
            continue
        rule_path = os.path.join(rules_dir, filename)
        try:
            with open(rule_path) as f:
                content = f.read().strip()
            if content:
                rules[filename] = content
        except OSError:
            logger.warning("Failed to load rule template %s", filename)
    return rules


# Static docs shipped to every tenant workspace under docs/
_WORKSPACE_DOCS = {
    "NBHD_DOC_TOOLS_REFERENCE": "tools-reference.md",
    "NBHD_DOC_CRON_MANAGEMENT": "cron-management.md",
    "NBHD_DOC_ERROR_HANDLING": "error-handling.md",
    "NBHD_DOC_PLATFORM_GUIDE": "platform-guide.md",
    "NBHD_DOC_PLATFORM_GUIDE_SILENT": "platform-guide-silent.md",
}


def _resolve_channel_formatting(tenant=None) -> str | None:
    """Load the channel-specific formatting doc for a tenant.

    Uses the user's preferred_channel to pick telegram-formatting.md or
    line-formatting.md.  Falls back to telegram-formatting.md.
    """
    channel = "telegram"
    if tenant is not None:
        try:
            channel = tenant.user.preferred_channel or "telegram"
        except Exception:
            pass
    content = _load_doc_template(f"{channel}-formatting.md")
    if content is None and channel != "telegram":
        content = _load_doc_template("telegram-formatting.md")
    return content


def _get_tenant_prompt_extras(tenant, section: str) -> str:
    """Return per-tenant prompt extras for a given section, or empty string.

    Source: ``tenant.user.preferences["prompt_extras"][section]``.

    This is the hook for canary-style per-tenant prompt overrides without a
    schema migration. Populate via the ``set_prompt_extras`` management
    command. Known sections: ``agents_md``, ``soul_md``, ``identity_md`` (the
    latter two are spliced INSIDE the managed SOUL/IDENTITY region).

    Unknown or malformed values are silently ignored (returns "").
    """
    if tenant is None or not hasattr(tenant, "user"):
        return ""
    prefs = getattr(tenant.user, "preferences", None) or {}
    extras_map = prefs.get("prompt_extras") if isinstance(prefs, dict) else None
    if not isinstance(extras_map, dict):
        return ""
    value = extras_map.get(section, "")
    return value.strip() if isinstance(value, str) else ""


def render_workspace_files(persona_key: str, tenant=None) -> dict[str, str]:
    """Render all persona-aware workspace files.

    Returns a dict mapping env var names to content:
    - NBHD_AGENTS_MD
    - NBHD_SOUL_MD
    - NBHD_IDENTITY_MD
    - NBHD_DOC_* (reference docs written to workspace/docs/)
    - NBHD_SKILL_TEMPLATES_MD (when tenant is provided)

    Per-tenant prompt extras (``tenant.user.preferences['prompt_extras']``)
    are appended to the relevant base file — e.g., ``agents_md`` extras are
    concatenated to NBHD_AGENTS_MD. Lets us ship canary-scoped prompt rules
    without branching the template or running a schema migration.
    """
    result = {
        "NBHD_AGENTS_MD": render_agents_md(persona_key),
        # Sentinel-split managed regions — the platform re-asserts these; the
        # agent's growth region below the END marker is merged in at write time
        # by apps.orchestrator.identity_merge (never produced here).
        "NBHD_SOUL_MD": render_soul_managed(persona_key, tenant),
        "NBHD_IDENTITY_MD": render_identity_managed(persona_key, tenant),
    }
    agents_extras = _get_tenant_prompt_extras(tenant, "agents_md")
    if agents_extras:
        result["NBHD_AGENTS_MD"] = result["NBHD_AGENTS_MD"] + "\n\n" + agents_extras

    # Site publishing gate — behavioral, per-tenant. Only tenants with their own
    # website connected (site_publishing_enabled) load the publish_portfolio_image
    # tool, so the imperative cue that makes the agent actually CALL it — rather
    # than confabulate "done" — is gated the same way (reconcile-gate style; under
    # toolSearch a passive note doesn't make the model reach for the tool: proven,
    # the agent cataloged the tool but never called it until this gate landed).
    # Placed BEFORE the larger Gravity block so that if AGENTS.md exceeds the
    # bootstrap char budget, this critical anti-confabulation gate is not the tail
    # that gets silently truncated.
    if tenant is not None and getattr(tenant, "site_publishing_enabled", False):
        site_publish_gate = (
            "## Portfolio publish gate\n\n"
            "If the user sends one or more images AND asks to add / publish / put / update them "
            "on their site, portfolio, website, or gallery → search the tool catalog for "
            "`publish_portfolio_image` by name and call it FIRST, before replying, exactly ONCE "
            "PER IMAGE. It is NOT pre-loaded; you must find it via toolSearch, then call it — "
            "three photos means three calls. Do not skip the call.\n\n"
            "Pass per call: `image_path` and `title` (both required). If the user gave no title, "
            "view the image and propose a short one yourself, OR ask ONCE for a shared theme "
            "across all the images — never interrogate for a title per photo.\n\n"
            "NEVER tell the user an image is added / published / live / updated unless THAT "
            "image's `publish_portfolio_image` call returned success THIS turn. No successful "
            'call → no "done." Report exactly which images landed and which did not; name any '
            "that failed and retry or ask. If a call reports that publishing isn't configured, "
            "do NOT retry — tell the user site publishing isn't set up yet."
        )
        result["NBHD_AGENTS_MD"] = result["NBHD_AGENTS_MD"] + "\n\n" + site_publish_gate

    # Neighborhood backstage gate — behavioral, per-tenant (design §5.2). The
    # imperative "propose only, absorb quietly, never post" rules that keep the
    # agent invisible + the anti-confabulation line ("never claim shared without
    # an approval THIS turn"). Placed BEFORE the larger Gravity block so it can't
    # be the silently-truncated tail if AGENTS.md exceeds the bootstrap budget
    # (same reasoning as the site-publish gate above). Mission tools land in PR6.
    if tenant is not None and getattr(tenant, "friends_enabled", False):
        propose_enabled = getattr(tenant, "friends_agent_propose_enabled", False)
        gate_parts = [
            "## Neighborhood — you are BACKSTAGE (only if the user has neighbors)\n\n"
            "You are INVISIBLE in the Neighborhood. You NEVER post to a neighbor, a chat, a Circle, or a "
            "Mission, and you never appear where neighbors can see you. Everything neighbors see comes "
            "from your human, in their words, with their name on it.\n\n"
        ]
        if propose_enabled:
            gate_parts.append(
                "You may do exactly two things:\n"
                "  1. PROPOSE a share, privately, to your human. When your human's OWN experience would "
                "genuinely help a specific neighbor, search the tool catalog for `nbhd_propose_lesson_share` "
                "by name and CALL it ONCE. It is NOT pre-loaded — find it via toolSearch, then call it. This "
                "creates a PROPOSAL only; a human must approve before anything is shared. NEVER tell the user "
                "something was shared, sent, or is visible to a neighbor unless an approval came back THIS "
                "turn. No approval → it is still private.\n"
                "  2. ABSORB quietly. Neighbors' shared sparks appear in your context. Hold them until useful; "
                'surface naturally ("that ramen spot Kenji shared is near where you\'re headed"). No '
                "notifications, no spam. If the user asks what you learned from neighbors, tell them plainly — "
                "they can inspect and purge all of it.\n\n"
                "NEVER propose sharing: health details; money, amounts, or finances; family or personal "
                "matters; anything from a private conversation; anything the user did not clearly discuss as "
                "shareable. When unsure, do NOT propose.\n\n"
                "For a Mission (a shared goal with a neighbor), you help YOUR human show up: you may search the "
                "tool catalog for `nbhd_propose_mission_task` and call it to suggest ONE task to your human "
                "toward the goal. It creates a PROPOSAL only — your human approves before it becomes their task. "
                "You never act for another person and never message the group.\n\n"
            )
        else:
            gate_parts.append(
                "Your one job here is to ABSORB quietly. Neighbors' shared sparks appear in your context; "
                'hold them until useful and surface them naturally ("that ramen spot Kenji shared is near '
                "where you're headed\"). No notifications, no spam. If the user asks what you learned from "
                "neighbors, tell them plainly — they can inspect and purge all of it. You do NOT propose "
                "shares or mission tasks: starting a share is something the user does themselves, never you.\n\n"
            )
        circle_para = (
            "A Circle is a named group of the user's neighbors. Sparks the user absorbed FROM one Circle are "
            "tagged with that Circle. NEVER surface something the user learned in one Circle as if it belongs "
            "to another Circle (or to a neighbor outside it) — what a neighbor confided in one group does not "
            "travel to another."
        )
        if propose_enabled:
            circle_para += (
                " Propose a Circle share only for the user's OWN item, and only via the same "
                "propose-then-human-approves path."
            )
        gate_parts.append(circle_para)
        result["NBHD_AGENTS_MD"] = result["NBHD_AGENTS_MD"] + "\n\n" + "".join(gate_parts)

    # Gravity observation-mode rules — behavioral, belongs in AGENTS.md
    # (not USER.md). The rules block is ~6 KB of static text; until
    # 2026-05-22 it lived in USER.md via apps/insights/envelope.py, which
    # pushed USER.md past OpenClaw's 12 KB per-file bootstrap budget and
    # silently truncated the tail (Privacy Placeholders, Recent journal,
    # Fuel/Gravity state). USER.md now carries only the dynamic counts
    # block; the full gate + register-selection rules live here.
    if tenant is not None and getattr(tenant, "finance_active", False):
        from apps.insights.envelope import render_observation_mode_rules

        result["NBHD_AGENTS_MD"] = (
            result["NBHD_AGENTS_MD"] + "\n\n## Gravity Observation Mode\n\n" + render_observation_mode_rules(tenant)
        )

    # Load static reference docs
    for key, filename in _WORKSPACE_DOCS.items():
        content = _load_doc_template(filename)
        if content:
            result[key] = content

    # Channel-specific formatting doc (telegram or line)
    formatting_content = _resolve_channel_formatting(tenant)
    if formatting_content:
        result["NBHD_DOC_CHANNEL_FORMATTING"] = formatting_content

    # Privacy placeholder doc — only for tiers that redact PERSON entities
    if tenant is not None:
        from apps.pii.config import TIER_POLICIES

        tier = getattr(tenant, "model_tier", "starter")
        policy = TIER_POLICIES.get(tier, TIER_POLICIES["starter"])
        if policy.get("enabled") and "PERSON" in policy.get("entities", []):
            pii_doc = _load_doc_template("privacy-redaction.md")
            if pii_doc:
                result["NBHD_DOC_PRIVACY_REDACTION"] = pii_doc

    if tenant is not None:
        result["NBHD_SKILL_TEMPLATES_MD"] = render_templates_md(tenant)
    return result


def list_personas() -> list[dict[str, str]]:
    """Return persona list for API responses."""
    return [
        {
            "key": key,
            "label": persona["label"],
            "description": persona["description"],
            "emoji": persona["emoji"],
        }
        for key, persona in PERSONAS.items()
    ]
