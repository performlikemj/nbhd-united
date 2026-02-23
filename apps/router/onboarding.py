"""Telegram onboarding flow for new subscribers.

Hybrid approach (Option C):
- Steps 0-3: Code-driven, structured questions with parsed responses
- Step 4: Free-form, forwarded to the agent for natural conversation

Guarantees capture of name, language, timezone before handing off to the agent.

Also handles re-introduction for existing users who were backfilled
(onboarding_complete=True but onboarding_step=4 with no real data).
"""
from __future__ import annotations

import logging
import re

from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Language mappings
# ---------------------------------------------------------------------------
LANGUAGE_ALIASES: dict[str, tuple[str, str]] = {
    # input → (language_code, display_name)
    "english": ("en", "English"),
    "en": ("en", "English"),
    "spanish": ("es", "Spanish"),
    "español": ("es", "Spanish"),
    "es": ("es", "Spanish"),
    "french": ("fr", "French"),
    "français": ("fr", "French"),
    "fr": ("fr", "French"),
    "german": ("de", "German"),
    "deutsch": ("de", "German"),
    "de": ("de", "German"),
    "portuguese": ("pt", "Portuguese"),
    "português": ("pt", "Portuguese"),
    "pt": ("pt", "Portuguese"),
    "japanese": ("ja", "Japanese"),
    "日本語": ("ja", "Japanese"),
    "ja": ("ja", "Japanese"),
    "chinese": ("zh", "Chinese"),
    "中文": ("zh", "Chinese"),
    "zh": ("zh", "Chinese"),
    "korean": ("ko", "Korean"),
    "한국어": ("ko", "Korean"),
    "ko": ("ko", "Korean"),
    "italian": ("it", "Italian"),
    "italiano": ("it", "Italian"),
    "it": ("it", "Italian"),
    "dutch": ("nl", "Dutch"),
    "nederlands": ("nl", "Dutch"),
    "nl": ("nl", "Dutch"),
    "russian": ("ru", "Russian"),
    "русский": ("ru", "Russian"),
    "ru": ("ru", "Russian"),
    "arabic": ("ar", "Arabic"),
    "العربية": ("ar", "Arabic"),
    "ar": ("ar", "Arabic"),
    "hindi": ("hi", "Hindi"),
    "हिन्दी": ("hi", "Hindi"),
    "hi": ("hi", "Hindi"),
    "turkish": ("tr", "Turkish"),
    "türkçe": ("tr", "Turkish"),
    "tr": ("tr", "Turkish"),
    "thai": ("th", "Thai"),
    "ไทย": ("th", "Thai"),
    "th": ("th", "Thai"),
    "vietnamese": ("vi", "Vietnamese"),
    "tiếng việt": ("vi", "Vietnamese"),
    "vi": ("vi", "Vietnamese"),
    "polish": ("pl", "Polish"),
    "polski": ("pl", "Polish"),
    "pl": ("pl", "Polish"),
    "indonesian": ("id", "Indonesian"),
    "bahasa indonesia": ("id", "Indonesian"),
    "id": ("id", "Indonesian"),
    "malay": ("ms", "Malay"),
    "bahasa melayu": ("ms", "Malay"),
    "ms": ("ms", "Malay"),
    "tagalog": ("tl", "Tagalog"),
    "filipino": ("tl", "Tagalog"),
    "tl": ("tl", "Tagalog"),
    "swahili": ("sw", "Swahili"),
    "kiswahili": ("sw", "Swahili"),
    "sw": ("sw", "Swahili"),
}


def parse_language(text: str) -> tuple[str, str]:
    """Parse language from user input. Returns (code, display_name)."""
    cleaned = text.strip().lower()

    # Direct match
    if cleaned in LANGUAGE_ALIASES:
        return LANGUAGE_ALIASES[cleaned]

    # Substring match
    for alias, result in LANGUAGE_ALIASES.items():
        if alias in cleaned:
            return result

    # Default to English
    return ("en", "English")


# ---------------------------------------------------------------------------
# Timezone mappings
# ---------------------------------------------------------------------------
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
    "bangkok": "Asia/Bangkok",
    "jakarta": "Asia/Jakarta",
    "manila": "Asia/Manila",
    "kuala lumpur": "Asia/Kuala_Lumpur",
    "cairo": "Africa/Cairo",
    "nairobi": "Africa/Nairobi",
    "lagos": "Africa/Lagos",
    "johannesburg": "Africa/Johannesburg",
    "kingston": "America/Jamaica",
    "jamaica": "America/Jamaica",
}

UTC_OFFSET_RE = re.compile(
    r"(?:utc|gmt)\s*([+-])\s*(\d{1,2})(?::(\d{2}))?", re.IGNORECASE
)


def parse_timezone(text: str) -> str:
    """Best-effort timezone parsing. Returns IANA string or 'UTC' as fallback."""
    cleaned = text.strip().lower()

    if cleaned in TIMEZONE_ALIASES:
        return TIMEZONE_ALIASES[cleaned]

    m = UTC_OFFSET_RE.search(cleaned)
    if m:
        sign, hours = m.group(1), int(m.group(2))
        if sign == "+":
            return f"Etc/GMT-{hours}" if hours != 0 else "UTC"
        else:
            return f"Etc/GMT+{hours}" if hours != 0 else "UTC"

    for alias, tz in TIMEZONE_ALIASES.items():
        if alias in cleaned:
            return tz

    return "UTC"


# ---------------------------------------------------------------------------
# Name parsing
# ---------------------------------------------------------------------------
def parse_name(text: str) -> str:
    """Extract a name from user response."""
    name = text.strip()
    for prefix in ("my name is", "i'm", "im", "call me", "it's", "its", "i am"):
        if name.lower().startswith(prefix):
            name = name[len(prefix):].strip()
    name = name.rstrip("!.,")
    if name:
        name = name.strip()
        if name == name.lower():
            name = name.title()
    return name or "Friend"


# ---------------------------------------------------------------------------
# Onboarding flow
# ---------------------------------------------------------------------------

# Step 0: send welcome (triggered by first message)
# Step 1: user answered name → ask language
# Step 2: user answered language → ask timezone
# Step 3: user answered timezone → ask interests
# Step 4: user answered interests → complete

WELCOME_MESSAGE = (
    "Hey there! 👋 Welcome to Neighborhood United.\n\n"
    "I'm your personal AI assistant. Before we get started, "
    "I'd love to learn a little about you so I can be more helpful.\n\n"
    "First — what should I call you?"
)

REINTRO_MESSAGE = (
    "Hey! 👋 I realize I never properly introduced myself or got to know you.\n\n"
    "I'm your personal AI assistant, and I'd love to set things up properly "
    "so I can help you better.\n\n"
    "Let's start — what should I call you?"
)


def get_onboarding_response(
    tenant: Tenant, message_text: str
) -> str | None:
    """Process an onboarding message and return the response.

    Returns:
        str: The next question or completion message
        None: Onboarding is complete, forward to agent normally
    """
    step = tenant.onboarding_step

    # Step 0: First message → send welcome + first question
    if step == 0:
        # Check if this is a re-introduction (backfilled user)
        is_reintro = tenant.onboarding_complete  # was True from backfill
        tenant.onboarding_complete = False  # reset for flow
        tenant.onboarding_step = 1
        tenant.save(update_fields=["onboarding_complete", "onboarding_step", "updated_at"])
        return REINTRO_MESSAGE if is_reintro else WELCOME_MESSAGE

    name = tenant.user.display_name or "Friend"

    # Step 1: They answered the name question → ask language
    if step == 1:
        name = parse_name(message_text)
        tenant.user.display_name = name
        tenant.user.save(update_fields=["display_name"])
        logger.info("Onboarding [%s]: name=%s", tenant.id, name)

        tenant.onboarding_step = 2
        tenant.save(update_fields=["onboarding_step", "updated_at"])
        return (
            f"Nice to meet you, {name}! 🎉\n\n"
            f"What language would you like me to talk to you in?\n"
            f"(e.g. English, Spanish, Japanese, French...)"
        )

    # Step 2: They answered the language question → ask timezone
    if step == 2:
        lang_code, lang_name = parse_language(message_text)
        tenant.user.language = lang_code
        tenant.user.save(update_fields=["language"])
        logger.info("Onboarding [%s]: language=%s (%s)", tenant.id, lang_code, lang_name)

        tenant.onboarding_step = 3
        tenant.save(update_fields=["onboarding_step", "updated_at"])
        return (
            f"Great, I'll communicate in {lang_name}! 🗣️\n\n"
            f"What timezone are you in?\n"
            f"(e.g. \"EST\", \"Pacific\", \"JST\", \"UTC+2\", or a city like \"Tokyo\" or \"New York\")"
        )

    # Step 3: They answered the timezone question → ask interests
    if step == 3:
        tz = parse_timezone(message_text)
        tenant.user.timezone = tz
        tenant.user.save(update_fields=["timezone"])
        logger.info("Onboarding [%s]: timezone=%s (from: %s)", tenant.id, tz, message_text.strip())

        tenant.onboarding_step = 4
        tenant.save(update_fields=["onboarding_step", "updated_at"])
        return (
            "Got it! Last question from me — what are you most hoping your assistant can help with?\n"
            "(work stuff, personal organization, creative projects, just someone to chat with... anything goes!)"
        )

    # Step 4: They answered the interests question → complete
    if step == 4:
        interests = message_text.strip()
        logger.info("Onboarding [%s]: interests=%s", tenant.id, interests)

        prefs = tenant.user.preferences or {}
        prefs["onboarding_interests"] = interests
        tenant.user.preferences = prefs
        tenant.user.save(update_fields=["preferences"])

        _write_user_md(tenant, interests)

        tenant.onboarding_complete = True
        tenant.onboarding_step = 5
        tenant.save(update_fields=["onboarding_complete", "onboarding_step", "updated_at"])

        return (
            f"Thanks, {name}! I've got everything I need. 🎉\n\n"
            f"Your assistant is all set up and ready to go. "
            f"From here on out, you're chatting directly with your personal AI.\n\n"
            f"Go ahead — say hi, ask a question, or tell it what you need help with!"
        )

    # Step 5+: Already complete
    return None


def needs_reintroduction(tenant: Tenant) -> bool:
    """Check if an existing user should go through re-introduction.

    Returns True for backfilled users who have default/empty profile data.
    """
    if not tenant.onboarding_complete:
        return False  # Already in onboarding flow

    user = tenant.user
    has_default_name = user.display_name in ("Friend", "")
    has_default_tz = user.timezone in ("UTC", "")
    has_default_lang = user.language in ("en", "")
    has_no_interests = not (user.preferences or {}).get("onboarding_interests")

    # If most fields are defaults, they were likely backfilled
    defaults_count = sum([has_default_name, has_default_tz, has_default_lang, has_no_interests])
    return defaults_count >= 3


def _write_user_md(tenant: Tenant, interests: str) -> None:
    """Write USER.md to the tenant's file share with onboarding data."""
    try:
        from apps.orchestrator.azure_client import upload_workspace_file  # noqa: F811

        name = tenant.user.display_name or "Friend"
        tz = tenant.user.timezone or "UTC"
        lang = tenant.user.language or "en"

        content = f"""# About You

- **Name:** {name}
- **Language:** {lang}
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
