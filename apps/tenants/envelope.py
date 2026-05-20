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
    """
    return _PRIVACY_PLACEHOLDERS_BODY
