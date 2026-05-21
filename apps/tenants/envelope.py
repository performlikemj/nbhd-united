"""USER.md ``Profile`` section — pulled from ``tenant.user`` fields.

Always-on (no feature flag), comes first in the rendered region (order=10)
because it grounds every later section in who the user actually is.
"""

from __future__ import annotations

from apps.orchestrator.envelope_registry import register_section
from apps.tenants.models import Tenant, User


@register_section(
    key="profile",
    heading="## Profile",
    enabled=lambda t: True,
    refresh_on=(User,),
    order=10,
)
def render_profile(tenant: Tenant) -> str:
    """Compact profile block — lines for fields the user has actually set.

    Default values (``display_name="Friend"``, ``timezone="UTC"``,
    ``language="en"``) are suppressed so the block stays short.
    ``preferred_channel`` always renders because it's load-bearing for
    routing/formatting decisions.
    """
    user = getattr(tenant, "user", None)
    if user is None:
        return ""

    lines: list[str] = []

    display_name = (getattr(user, "display_name", "") or "").strip()
    if display_name and display_name != "Friend":
        lines.append(f"- Display name: {display_name}")

    user_tz = (getattr(user, "timezone", "") or "").strip()
    if user_tz and user_tz != "UTC":
        lines.append(f"- Timezone: {user_tz}")

    preferred_channel = (getattr(user, "preferred_channel", "") or "").strip()
    if preferred_channel:
        lines.append(f"- Preferred channel: {preferred_channel}")

    language = (getattr(user, "language", "") or "").strip()
    if language and language != "en":
        lines.append(f"- Language: {language}")

    city = (getattr(user, "location_city", "") or "").strip()
    if city:
        lines.append(f"- Location: {city}")

    if not lines:
        return ""
    return "\n".join(lines)


_PRIVACY_PLACEHOLDERS_BODY = (
    "Your workspace files and tool results may contain anonymized placeholders like "
    "`[PERSON_1]`, `[EMAIL_ADDRESS_1]`, `[PHONE_NUMBER_1]`, `[LOCATION_1]`. A platform "
    "restoration layer converts them back to real values before the user sees your reply.\n"
    "\n"
    "- **Preserve placeholders exactly as written.** Never guess, invent, or substitute "
    "a real name / email / phone / location — even when context makes it obvious.\n"
    "- **Never combine placeholders with name fragments from other fields.** Each "
    "placeholder is a complete value.\n"
    "- **Treat placeholders as the real values when reasoning** — `[PERSON_1]` *is* the "
    "person; refer to them as `[PERSON_1]` in your reply.\n"
    "- **Don't ask the user** what the real value is, and **don't mention or explain** "
    "the placeholders — the restoration is invisible to them.\n"
    "\n"
    'Example — DO: "You got an email from [PERSON_1] about the demo." '
    'DON\'T: "You got an email from Ryota about the demo."'
)


@register_section(
    key="privacy_placeholders",
    heading="## Privacy Placeholders",
    enabled=lambda t: bool(getattr(t, "pii_entity_map", None)),
    # No own refresh trigger — the entity_map is updated via
    # ``memory_sync.py``'s ``filter().update()`` which bypasses post_save
    # anyway, so subscribing to ``Tenant`` saves wouldn't help. The section
    # picks up on the next USER.md push triggered by any other registered
    # contributor (Profile via User, Goals/Tasks via Document, Fuel via
    # Workout, etc.) — every tenant activity drains a refresh.
    refresh_on=(),
    order=70,
)
def render_privacy_placeholders(tenant: Tenant) -> str:
    """Auto-injected rule block when the tenant has redacted entity state.

    The full reference is in ``templates/openclaw/docs/privacy-redaction.md``.
    This block lives in USER.md so the rule is in the agent's always-on
    context for tenants where placeholders are actively in play — keeps
    AGENTS.md slim while making the rule visible exactly when it matters.

    When entries in ``pii_entity_map`` carry ``relationship`` or ``notes``
    metadata (the new dict shape from the entity registry), an *Identity
    context* sub-section is appended listing those entries so the agent
    can disambiguate pronouns ("she", "they") against user-curated
    identity — without ever seeing the real name. Legacy string-only
    entries contribute nothing to this sub-section but still benefit
    from the always-on preservation rule above.
    """
    body = _PRIVACY_PLACEHOLDERS_BODY
    identity = _render_identity_context(tenant)
    if identity:
        body = f"{body}\n\n{identity}"
    return body


def _render_identity_context(tenant: Tenant) -> str:
    """Build the *Identity context* sub-section from ``pii_entity_map``
    entries that have either ``relationship`` or ``notes`` populated.

    Returns empty string when nothing user-curated exists — keeps the
    privacy block tight on tenants who haven't filled in any metadata.
    """
    from apps.pii.entity_registry import get_metadata, iter_normalized

    entity_map = getattr(tenant, "pii_entity_map", None)
    if not entity_map:
        return ""

    lines: list[str] = []
    # Sort by placeholder for stable rendering (deterministic envelope diffs).
    sorted_entries = sorted(iter_normalized(entity_map), key=lambda kv: kv[0])
    for placeholder, entry in sorted_entries:
        meta = get_metadata(entry)
        relationship = (meta.get("relationship") or "").strip()
        notes = (meta.get("notes") or "").strip()
        if not relationship and not notes:
            continue
        if relationship and notes:
            descriptor = f"{relationship} — {notes}"
        else:
            descriptor = relationship or notes
        lines.append(f"- `{placeholder}` — {descriptor}")

    if not lines:
        return ""

    header = (
        "### Identity context\n"
        "\n"
        "The following placeholders refer to specific people in the user's "
        "life. Use this metadata to disambiguate pronouns and references "
        '("she", "they", "my daughter") — but still emit the '
        "`[PERSON_X]` placeholder verbatim in your reply, never the "
        "metadata below.\n"
    )
    return header + "\n" + "\n".join(lines)
