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
