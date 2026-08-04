"""USER.md ``Profile`` section — pulled from ``tenant.user`` fields.

Always-on (no feature flag), comes first in the rendered region (order=10)
because it grounds every later section in who the user actually is.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from apps.common.tenant_tz import tenant_tz
from apps.orchestrator.envelope_registry import register_section
from apps.tenants.models import Tenant, User, UserSituation

logger = logging.getLogger(__name__)

PLACE_DECAY = timedelta(hours=48)
DEVICE_TZ_TTL = timedelta(days=7)
AWAY_NUDGE_AFTER = timedelta(days=5)
SITUATION_MAX_CHARS = 400

# Agent-facing labels for the resolved delivery channel. Mirrors the marker
# labels in apps/router/services.py (_CHANNEL_LABELS) so the Profile block and
# the per-turn "[chat via …]" stamp name the same surface the same way.
_DELIVERY_CHANNEL_LABELS = {
    "app": "NBHD app",
    "telegram": "Telegram",
    "line": "LINE",
}


def _resolve_delivery_channel_label(user) -> str:
    """Human label for the channel outbound messages actually go to, or ``""``.

    Fail-open: any resolver error degrades to omitting the line rather than
    risking a wrong channel in the agent's context (the whole point of this
    change) or breaking the USER.md render.
    """
    try:
        from apps.router.cron_delivery import resolve_user_channel

        channel = resolve_user_channel(user)
    except Exception:
        return ""
    return _DELIVERY_CHANNEL_LABELS.get(channel or "", "")


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

    The channel line renders the RESOLVED delivery channel, never the raw
    ``preferred_channel`` column. ``preferred_channel`` is the untouched schema
    default (``"telegram"``) on essentially every row, so rendering it asserted
    "Preferred channel: telegram" into the agent-visible context of iOS-only
    tenants who never linked Telegram — the exact falsehood the channel-identity
    fix exists to kill. We delegate to ``resolve_user_channel`` (the single
    source of truth the outbound senders route on) rather than duplicating the
    rule, so this line automatically inherits any correction to that resolver.
    When it returns None (no delivery surface at all) the line is omitted —
    printing nothing beats printing a falsehood.

    The ``apps.router`` import is function-local: ``apps.router.cron_delivery``
    imports ``apps.tenants.models`` at module scope, and this module is imported
    during envelope-registry setup. A local import matches the established
    tenants→router pattern (see ``apps/tenants/emails.py``, ``line_views.py``).
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

    delivery_channel = _resolve_delivery_channel_label(user)
    if delivery_channel:
        lines.append(f"- Delivery channel: {delivery_channel}")

    language = (getattr(user, "language", "") or "").strip()
    if language and language != "en":
        lines.append(f"- Language: {language}")

    city = (getattr(user, "location_city", "") or "").strip()
    if city:
        lines.append(f"- Home location: {city}")

    if not lines:
        return ""
    return "\n".join(lines)


def _situation_enabled(tenant: Tenant) -> bool:
    return bool(getattr(tenant, "situational_context_enabled", False)) and not getattr(tenant, "is_eval_sink", False)


def _observation_age_seconds(now, observed_at) -> int:
    if observed_at is None:
        return 0
    return max(0, int((now - observed_at).total_seconds()))


def _format_place_observed_at(tenant: Tenant, observed_at, now) -> str:
    tz = tenant_tz(tenant)
    local_observed = observed_at.astimezone(tz)
    local_now = now.astimezone(tz)
    if local_observed.date() == local_now.date():
        return f"{local_observed:%H:%M} today"
    return local_observed.strftime("%b %d").replace(" 0", " ")


def _cap_situation_lines(place_line: str, tz_line: str, nudge_line: str) -> str:
    lines = [line for line in (place_line, tz_line, nudge_line) if line]
    body = "\n".join(lines)
    if len(body) <= SITUATION_MAX_CHARS:
        return body

    if nudge_line and nudge_line in lines:
        lines.remove(nudge_line)
        body = "\n".join(lines)
    if len(body) > SITUATION_MAX_CHARS and tz_line and tz_line in lines:
        lines.remove(tz_line)
        body = "\n".join(lines)
    if len(body) > SITUATION_MAX_CHARS:
        body = body[: SITUATION_MAX_CHARS - 1].rstrip() + "…"
    return body


@register_section(
    key="right_now",
    heading="## Right now",
    enabled=_situation_enabled,
    refresh_on=(UserSituation,),
    order=13,
)
def render_situation(tenant: Tenant) -> str:
    """Render fresh place/timezone signals and omit decayed observations."""
    try:
        situation = tenant.situation
    except UserSituation.DoesNotExist:
        return ""

    now = timezone.now()
    user = getattr(tenant, "user", None)
    home = str(getattr(user, "location_city", "") or "").strip()
    profile_tz = str(getattr(user, "timezone", "") or "UTC").strip() or "UTC"

    place_label = (situation.current_place_label or "").strip()
    place_age_s = _observation_age_seconds(now, situation.current_place_last_observed_at)
    place_fresh = bool(
        place_label
        and situation.current_place_last_observed_at
        and now - situation.current_place_last_observed_at <= PLACE_DECAY
    )
    traveling = bool(place_fresh and place_label.casefold() != home.casefold())

    if place_label and not place_fresh:
        logger.info(
            "situation_decayed tenant=%s age_s=%d",
            tenant.id,
            place_age_s,
        )

    place_line = ""
    if place_fresh:
        as_of = _format_place_observed_at(tenant, situation.current_place_last_observed_at, now)
        home_clause = f"; home base {home or 'not set'}" if traveling else ""
        place_line = f"Current location: {place_label} (as of {as_of}{home_clause})."

    device_tz = (situation.device_tz or "").strip()
    device_tz_fresh = bool(
        device_tz
        and situation.device_tz_last_observed_at
        and now - situation.device_tz_last_observed_at <= DEVICE_TZ_TTL
    )
    tz_line = ""
    if device_tz_fresh and device_tz != profile_tz:
        tz_line = f"Device timezone: {device_tz} (profile: {profile_tz})."

    nudge_line = ""
    if traveling and situation.current_place_since and now - situation.current_place_since >= AWAY_NUDGE_AFTER:
        nudge_line = "(Away 5+ days — consider asking whether to update the home base or shift the schedule.)"

    body = _cap_situation_lines(place_line, tz_line, nudge_line)
    if body:
        logger.info(
            "situation_rendered tenant=%s fresh=%d traveling=%d age_s=%d",
            tenant.id,
            int(place_fresh),
            int(traveling),
            place_age_s,
        )
    return body


_PRIVACY_PLACEHOLDERS_BODY = (
    "Your workspace files and tool results may contain anonymized placeholders like "
    "`[PERSON_1]`, `[EMAIL_ADDRESS_1]`, `[PHONE_NUMBER_1]`, `[LOCATION_1]`. A platform "
    "restoration layer converts them back to real values before the user sees your reply.\n"
    "\n"
    "- **Preserve placeholders exactly as written.** Never guess, invent, or substitute "
    "a real name / email / phone / location — even when context makes it obvious.\n"
    "- **Never combine placeholders with name fragments from other fields.** Each "
    "placeholder is a complete value.\n"
    "- An annotation such as `[PERSON_1|coworker]` is relationship context; reason "
    "with that relationship while preserving the whole token.\n"
    "- If a token says `|unresolved`, say plainly that the name is redacted on your "
    "side with no relationship on file and ask who they are. Never claim familiarity, "
    "deny knowledge, or base a decision on an unresolved token.\n"
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
    # order=12 — directly after Profile (10), so the placeholder legend
    # sits near the TOP of USER.md and survives any future budget pressure.
    # Until 2026-05-22 this was order=70 (near the bottom), and when
    # USER.md exceeded OpenClaw's 12 KB bootstrap budget the placeholder
    # legend was silently truncated — the agent saw `[[USER_PAUL_3]]` etc.
    # with no dictionary mapping them back to real names, surfacing as
    # "funky" / "slightly off" replies. The dictionary is load-bearing:
    # if it ever does get cut by some future bloat, that's a worse
    # outcome than losing stale journal entries, so promote it.
    order=12,
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
        '("she", "they", "my daughter"). In replies preserve the complete '
        "placeholder token exactly as it appears in the current context; never "
        "emit the metadata as a raw name.\n"
    )
    return header + "\n" + "\n".join(lines)
