"""Tenant onboarding emails (welcome + future Day-N follow-ups).

Lives in apps.tenants because the templates and the Tenant model both
do. Other apps (orchestrator, billing) call into here at the right
moment in their flows.
"""

import logging

from django.conf import settings as django_settings
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils import timezone

from .models import Tenant

logger = logging.getLogger(__name__)


def _line_quota_available() -> bool:
    """Return True if the fleet-wide LINE quota has room for new users.

    Failing closed: if we can't read the quota state for any reason, we
    skip the LINE P.S. rather than offer a channel that may 429. The
    user can still discover LINE on the dashboard later.
    """
    try:
        from apps.router.models import LineQuotaState

        return not LineQuotaState.get().is_exhausted
    except Exception:
        logger.warning("welcome_email.line_quota_lookup_failed", exc_info=True)
        return False


def send_welcome_email(tenant: Tenant) -> bool:
    """Send the Day-0 welcome email to ``tenant.user.email``.

    Idempotent via ``tenant.welcome_email_sent_at`` — a second call after
    a successful send is a no-op. Returns True if the email went out on
    this call, False if skipped (already sent, no recipient address, or
    send failed).

    Non-critical: callers in the provisioning hot path should not let
    failures here roll back tenant state. Exceptions are caught and
    logged.
    """
    if tenant.welcome_email_sent_at:
        logger.info("welcome_email.already_sent tenant_id=%s", tenant.id)
        return False

    user = tenant.user
    recipient = (user.email or "").strip()
    if not recipient:
        logger.info("welcome_email.no_recipient tenant_id=%s user_id=%s", tenant.id, user.id)
        return False

    frontend_url = getattr(django_settings, "FRONTEND_URL", "https://neighborhoodunited.org").rstrip("/")
    settings_url = f"{frontend_url}/settings/integrations"

    video_url = getattr(django_settings, "WELCOME_VIDEO_URL", "") or ""

    context = {
        "display_name": user.display_name or "there",
        "telegram_connected": bool(user.telegram_chat_id),
        "settings_url": settings_url,
        "video_url": video_url,
        "line_quota_available": _line_quota_available(),
    }

    subject = render_to_string("email/welcome_subject.txt", context).strip()
    text_body = render_to_string("email/welcome_body.txt", context)
    html_body = render_to_string("email/welcome_body.html", context)

    try:
        send_mail(
            subject=subject,
            message=text_body,
            from_email=None,  # uses DEFAULT_FROM_EMAIL
            recipient_list=[recipient],
            html_message=html_body,
            fail_silently=False,
        )
    except Exception:
        logger.exception("welcome_email.send_failed tenant_id=%s", tenant.id)
        return False

    # Stamp idempotency marker only on a confirmed send. ``update_fields``
    # narrows the write so we don't race with concurrent saves elsewhere
    # in the provisioning flow.
    tenant.welcome_email_sent_at = timezone.now()
    tenant.save(update_fields=["welcome_email_sent_at", "updated_at"])
    logger.info(
        "welcome_email.sent tenant_id=%s recipient=%s telegram_connected=%s",
        tenant.id,
        recipient,
        context["telegram_connected"],
    )
    return True
