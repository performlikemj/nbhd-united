"""Telegram onboarding flow for new subscribers.

Hybrid approach (Option C):
- Steps 0-2: Code-driven, structured questions with parsed responses
- Step 3: Free-form, forwarded to the agent for natural conversation

Guarantees capture of name + timezone before handing off to the agent.
"""
from __future__ import annotations

import logging
import re

from django.utils import timezone as dj_timezone

from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

# Structured onboarding steps.
# Each step: (question_text, field_to_update, parser_function)
ONBOARDING_STEPS = [
    {
        "question": (
            "Hey there! 👋 Welcome to Neighborhood United.\n\n"
            "I'm your personal AI assistant. Before we get started, "
            "I'd love to learn a little about you so I can be more helpful.\n\n"
            "First — what should I call you?"
        ),
        "field": "name",
    },
    {
        "question": "Nice to meet you, {name}! 🎉\n\nWhat timezone are you in? "
        "(e.g. \"EST\", \"Pacific\", \"JST\", \"UTC+2\", or a city like \"Tokyo\" or \"New York\")",
        "field": "timezone",
    },
    {
        "question": "Got it! Last question from me — what are you most hoping your assistant can help with? "
        "(work stuff, personal organization, creative projects, just someone to chat with... anything goes!)",
        "field": "interests",
    },
]

# Common timezone aliases → IANA
TIMEZONE_ALIASES: dict[str, str] = {
    "est": "America/New_York",
    "eastern": "America/New_York",
    "cst": "America/Chicago",
    "central": "America/Chicago",
    "mst": "America/Denver",
    "mountain": "America/Denver",
    "pst": "America/Los_Angeles",
    "pacific": "America/Los_Angeles",
    "jst": "Asia/Tokyo",
    "gmt": "Europe/London",
    "bst": "Europe/London",
    "cet": "Europe/Berlin",
    "ist": "Asia/Kolkata",
    "aest": "Australia/Sydney",
    "aedt": "Australia/Sydney",
    "nzst": "Pacific/Auckland",
    "hst": "Pacific/Honolulu",
    "akst": "America/Anchorage",
    "ast": "America/Puerto_Rico",
    # Cities
    "new york": "America/New_York",
    "nyc": "America/New_York",
    "los angeles": "America/Los_Angeles",
    "la": "America/Los_Angeles",
    "chicago": "America/Chicago",
    "denver": "America/Denver",
    "london": "Europe/London",
    "paris": "Europe/Paris",
    "berlin": "Europe/Berlin",
    "tokyo": "Asia/Tokyo",
    "osaka": "Asia/Tokyo",
    "seoul": "Asia/Seoul",
    "beijing": "Asia/Shanghai",
    "shanghai": "Asia/Shanghai",
    "mumbai": "Asia/Kolkata",
    "delhi": "Asia/Kolkata",
    "sydney": "Australia/Sydney",
    "melbourne": "Australia/Melbourne",
    "dubai": "Asia/Dubai",
    "singapore": "Asia/Singapore",
    "hong kong": "Asia/Hong_Kong",
    "toronto": "America/Toronto",
    "vancouver": "America/Vancouver",
    "sao paulo": "America/Sao_Paulo",
    "mexico city": "America/Mexico_City",
}

# UTC offset pattern: UTC+5, UTC-3:30, GMT+9, etc.
UTC_OFFSET_RE = re.compile(
    r"(?:utc|gmt)\s*([+-])\s*(\d{1,2})(?::(\d{2}))?", re.IGNORECASE
)


def parse_timezone(text: str) -> str:
    """Best-effort timezone parsing. Returns IANA string or 'UTC' as fallback."""
    cleaned = text.strip().lower()

    # Direct alias match
    if cleaned in TIMEZONE_ALIASES:
        return TIMEZONE_ALIASES[cleaned]

    # UTC offset
    m = UTC_OFFSET_RE.search(cleaned)
    if m:
        sign, hours, minutes = m.group(1), int(m.group(2)), int(m.group(3) or 0)
        # Etc/GMT uses inverted signs
        offset = hours
        if sign == "+":
            return f"Etc/GMT-{offset}" if offset != 0 else "UTC"
        else:
            return f"Etc/GMT+{offset}" if offset != 0 else "UTC"

    # Check if any alias key is a substring
    for alias, tz in TIMEZONE_ALIASES.items():
        if alias in cleaned:
            return tz

    return "UTC"


def parse_name(text: str) -> str:
    """Extract a name from user response. Just clean it up."""
    name = text.strip()
    # Remove common prefixes
    for prefix in ("my name is", "i'm", "im", "call me", "it's", "its", "i am"):
        if name.lower().startswith(prefix):
            name = name[len(prefix):].strip()
    # Remove trailing punctuation
    name = name.rstrip("!.,")
    # Capitalize
    if name:
        name = name.strip()
        # Title case if all lower
        if name == name.lower():
            name = name.title()
    return name or "Friend"


def get_onboarding_response(
    tenant: Tenant, message_text: str
) -> str | None:
    """Process an onboarding message and return the response.

    Returns:
        str: The next question or completion message (handle in poller)
        None: Onboarding is complete, forward to agent normally
    """
    step = tenant.onboarding_step

    # Step 0: First ever message — send welcome + first question
    if step == 0:
        tenant.onboarding_step = 1
        tenant.save(update_fields=["onboarding_step", "updated_at"])
        return ONBOARDING_STEPS[0]["question"]

    # Steps 1-3: Process the answer to the previous question, ask the next
    if step == 1:
        # They answered the name question
        name = parse_name(message_text)
        tenant.user.display_name = name
        tenant.user.save(update_fields=["display_name"])
        logger.info("Onboarding [%s]: name=%s", tenant.id, name)

        tenant.onboarding_step = 2
        tenant.save(update_fields=["onboarding_step", "updated_at"])
        return ONBOARDING_STEPS[1]["question"].format(name=name)

    if step == 2:
        # They answered the timezone question
        tz = parse_timezone(message_text)
        tenant.user.timezone = tz
        tenant.user.save(update_fields=["timezone"])
        logger.info("Onboarding [%s]: timezone=%s (from: %s)", tenant.id, tz, message_text.strip())

        tenant.onboarding_step = 3
        tenant.save(update_fields=["onboarding_step", "updated_at"])
        return ONBOARDING_STEPS[2]["question"]

    if step == 3:
        # They answered the interests question — complete onboarding
        interests = message_text.strip()
        logger.info("Onboarding [%s]: interests=%s", tenant.id, interests)

        # Store interests in user preferences
        prefs = tenant.user.preferences or {}
        prefs["onboarding_interests"] = interests
        tenant.user.preferences = prefs
        tenant.user.save(update_fields=["preferences"])

        # Write USER.md to file share
        _write_user_md(tenant, interests)

        # Mark complete
        tenant.onboarding_complete = True
        tenant.onboarding_step = 4
        tenant.save(update_fields=["onboarding_complete", "onboarding_step", "updated_at"])

        name = tenant.user.display_name or "Friend"
        return (
            f"Thanks, {name}! I've got everything I need. 🎉\n\n"
            f"Your assistant is all set up and ready to go. "
            f"From here on out, you're chatting directly with your personal AI.\n\n"
            f"Go ahead — say hi, ask a question, or tell it what you need help with!"
        )

    # Step 4+: Already complete, should not reach here
    return None


def _write_user_md(tenant: Tenant, interests: str) -> None:
    """Write USER.md to the tenant's file share with onboarding data."""
    try:
        from apps.orchestrator.azure_client import upload_workspace_file  # noqa: F811

        name = tenant.user.display_name or "Friend"
        tz = tenant.user.timezone or "UTC"

        content = f"""# About You

- **Name:** {name}
- **Timezone:** {tz}

## What you're looking for

{interests}

---
*This file was created during onboarding. Your assistant will update it as it learns more about you.*
"""
        upload_workspace_file(str(tenant.id), "workspace/USER.md", content)
        logger.info("Wrote USER.md for tenant %s", tenant.id)
    except Exception:
        logger.exception("Failed to write USER.md for tenant %s", tenant.id)
        # Non-fatal — onboarding still completes
