"""Seed the platform-authored welcome shown in a tenant's first app session."""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

FIRST_SESSION_WELCOME_KEY = "first_session"
FIRST_SESSION_WELCOME_JOB_NAME = "_first_session_welcome"
FIRST_SESSION_WELCOME_CLIENT_MSG_ID = "_first_session_welcome"
FIRST_SESSION_WELCOME_QUICK_REPLIES = ["Tell you about me"]
FIRST_SESSION_DISPLAY_NAME_MAX_LENGTH = 80

FIRST_SESSION_WELCOME_TEMPLATE = """Hey {display_name} — welcome to the neighborhood. I'm your assistant, and this is where we talk.

Your private space is already set up: journal, goals, workouts, money, and more — all yours, all private. The fastest way for me to be genuinely useful is to know you a little. When you have a minute, tell me: what should I call you, and what's the one thing you're most hoping I can help with?

Reply whenever you're ready. I don't rush."""


def sanitize_display_name(display_name: object) -> str:
    """Return a short, single-line name that cannot mimic a PII placeholder."""
    cleaned = " ".join(str(display_name or "").split())
    cleaned = cleaned.replace("[", "").replace("]", "")
    return cleaned[:FIRST_SESSION_DISPLAY_NAME_MAX_LENGTH].strip()


def compose_first_session_welcome(display_name: object) -> str:
    """Interpolate the orchestrator-authored greeting without rewriting it."""
    cleaned_name = sanitize_display_name(display_name)
    if cleaned_name:
        return FIRST_SESSION_WELCOME_TEMPLATE.format(display_name=cleaned_name)

    unnamed = FIRST_SESSION_WELCOME_TEMPLATE.format(display_name="").removeprefix("Hey  — ")
    return unnamed.replace("welcome to the neighborhood.", "Welcome to the neighborhood.", 1)


def seed_first_session_welcome(tenant):
    """Stamp once under lock, then persist the app-feed greeting.

    The stamp deliberately precedes the row write. If persistence fails after
    the stamp, later provisioning retries accept the loss instead of risking a
    duplicate greeting.
    """
    marks = dict(tenant.welcomes_sent or {})
    if FIRST_SESSION_WELCOME_KEY in marks:
        return None

    from apps.tenants.models import Tenant

    with transaction.atomic():
        locked_tenant = Tenant.objects.select_for_update().only("welcomes_sent").get(pk=tenant.pk)
        marks = dict(locked_tenant.welcomes_sent or {})
        if FIRST_SESSION_WELCOME_KEY in marks:
            tenant.welcomes_sent = marks
            return None
        marks[FIRST_SESSION_WELCOME_KEY] = timezone.now().isoformat()
        locked_tenant.welcomes_sent = marks
        locked_tenant.save(update_fields=["welcomes_sent"])
    tenant.welcomes_sent = marks

    from apps.router.chat_views import create_delivered_app_assistant_message
    from apps.router.proactive_context import record_proactive_outbound

    message_text = compose_first_session_welcome(getattr(tenant.user, "display_name", ""))
    create_delivered_app_assistant_message(
        tenant=tenant,
        user=tenant.user,
        message_text=message_text,
        client_msg_id=FIRST_SESSION_WELCOME_CLIENT_MSG_ID,
        quick_replies=FIRST_SESSION_WELCOME_QUICK_REPLIES,
    )

    return record_proactive_outbound(
        tenant=tenant,
        channel="app",
        channel_user_id=str(tenant.user_id),
        message_text=message_text,
        job_name=FIRST_SESSION_WELCOME_JOB_NAME,
        quick_replies=FIRST_SESSION_WELCOME_QUICK_REPLIES,
    )
