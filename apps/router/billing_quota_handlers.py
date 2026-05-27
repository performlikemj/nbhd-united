"""Quota-email handlers — per-tenant cost-cap notifications (PR #1.8).

Two events trigger an email to the tenant's user.email:

  ``send_cost_approaching_email``  fires when ``estimated_cost_this_month``
      first crosses 90% of the tenant's effective cap. Gentle heads-up,
      no CTA — just lets them know so the eventual chat pause isn't a
      surprise.

  ``send_cost_exhausted_email``    fires when the tenant hits the full
      cap (either via the reconcile cron truing-up against OpenRouter
      truth, or via the in-flight 402 detector in the chat-completion
      drain path). Explains when chat resumes; mirrors the in-channel
      Telegram/LINE budget-exhausted message but lands in inbox where
      a paused tenant can actually see it.

Both are idempotent via per-tenant timestamp markers on the Tenant row
(``cost_warn_sent_at`` / ``cost_exhausted_email_sent_at``). Cleared
monthly by ``reset_monthly_counters`` so the chain re-arms each cycle.

The pattern mirrors ``apps.router.line_quota_handlers`` for the LINE
Push monthly-quota emails, with one key difference: line_quota is a
platform-wide singleton, this is per-tenant fan-out.
"""

from __future__ import annotations

import calendar
import logging
from datetime import date
from decimal import Decimal

from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone

from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)


def _next_month_first(today: date | None = None) -> date:
    """Return the date of the 1st of next month relative to ``today``."""
    today = today or date.today()
    if today.month == 12:
        return date(today.year + 1, 1, 1)
    return date(today.year, today.month + 1, 1)


def _format_reset_date(today: date | None = None) -> str:
    """Render the reset date as ``"June 1, 2026"``. Used in subject + body."""
    d = _next_month_first(today)
    return f"{calendar.month_name[d.month]} {d.day}, {d.year}"


def _format_dollars(value) -> str:
    """Coerce a Decimal / float / str to a ``$X.XX`` string."""
    try:
        dec = Decimal(str(value)).quantize(Decimal("0.01"))
    except Exception:
        return f"${value}"
    return f"${dec:.2f}"


def _should_email(tenant: Tenant) -> tuple[bool, str]:
    """Return ``(ok, reason)`` for whether ``tenant`` should receive a quota
    notification right now.

    Skips:
      - Tenant has no ``user.email`` (nothing to send to)
      - Tenant is ``is_budget_exempt`` (no cap → no reason to notify)
      - Tenant is in a non-ACTIVE state (deprovisioned / suspended)
    """
    user = getattr(tenant, "user", None)
    email = (getattr(user, "email", "") or "").strip()
    if not email:
        return False, "no_email"
    if getattr(tenant, "is_budget_exempt", False):
        return False, "budget_exempt"
    if tenant.status not in (Tenant.Status.ACTIVE, Tenant.Status.SUSPENDED):
        return False, f"status_{tenant.status}"
    return True, ""


def _build_context(tenant: Tenant) -> dict:
    """Common Django template context for both quota emails."""
    cap = Decimal(str(tenant.effective_cost_budget))
    used = Decimal(str(tenant.estimated_cost_this_month or 0))
    pct = int((used / cap * 100).quantize(Decimal("1"))) if cap > 0 else 0
    return {
        "display_name": (getattr(tenant.user, "display_name", "") or "").strip() or "there",
        "used_dollars": _format_dollars(used),
        "cap_dollars": _format_dollars(cap),
        "used_pct": pct,
        "reset_date": _format_reset_date(),
    }


def _send_html_email(*, to_email: str, subject: str, txt_body: str, html_body: str) -> bool:
    """Send a multipart text+html email. Returns True on success."""
    try:
        msg = EmailMultiAlternatives(
            subject=subject,
            body=txt_body,
            from_email=None,  # uses DEFAULT_FROM_EMAIL
            to=[to_email],
        )
        msg.attach_alternative(html_body, "text/html")
        msg.send(fail_silently=False)
        return True
    except Exception:
        logger.exception("billing_quota: email send failed to %s", to_email)
        return False


# ─────────────────────────────────────────────────────────────────────
# 90% approaching warning
# ─────────────────────────────────────────────────────────────────────


def send_cost_approaching_email(tenant: Tenant) -> bool:
    """Fire the 90%-of-cap warning email. Idempotent.

    Returns True iff a fresh email was sent. False on:
      - tenant already received the warning this month
      - tenant skipped per ``_should_email`` (no email / budget exempt / wrong status)
      - email send failed (logged via ``send_mail`` exception path)
    """
    if tenant.cost_warn_sent_at is not None:
        return False

    ok, reason = _should_email(tenant)
    if not ok:
        logger.info("billing_quota: approaching email skipped tenant=%s reason=%s", str(tenant.id)[:8], reason)
        return False

    ctx = _build_context(tenant)
    subject = render_to_string("email/billing_quota/cost_approaching_subject.txt", ctx).strip()
    txt_body = render_to_string("email/billing_quota/cost_approaching_body.txt", ctx)
    html_body = render_to_string("email/billing_quota/cost_approaching_body.html", ctx)

    if not _send_html_email(
        to_email=tenant.user.email,
        subject=subject,
        txt_body=txt_body,
        html_body=html_body,
    ):
        return False

    Tenant.objects.filter(id=tenant.id).update(cost_warn_sent_at=timezone.now())
    logger.info(
        "billing_quota: approaching email sent tenant=%s used_pct=%s",
        str(tenant.id)[:8],
        ctx["used_pct"],
    )
    return True


# ─────────────────────────────────────────────────────────────────────
# 100% exhausted notification
# ─────────────────────────────────────────────────────────────────────


def send_cost_exhausted_email(tenant: Tenant) -> bool:
    """Fire the cap-exhausted notification. Idempotent.

    Returns True iff a fresh email was sent. Skip conditions match
    ``send_cost_approaching_email``.
    """
    if tenant.cost_exhausted_email_sent_at is not None:
        return False

    ok, reason = _should_email(tenant)
    if not ok:
        logger.info("billing_quota: exhausted email skipped tenant=%s reason=%s", str(tenant.id)[:8], reason)
        return False

    ctx = _build_context(tenant)
    subject = render_to_string("email/billing_quota/cost_exhausted_subject.txt", ctx).strip()
    txt_body = render_to_string("email/billing_quota/cost_exhausted_body.txt", ctx)
    html_body = render_to_string("email/billing_quota/cost_exhausted_body.html", ctx)

    if not _send_html_email(
        to_email=tenant.user.email,
        subject=subject,
        txt_body=txt_body,
        html_body=html_body,
    ):
        return False

    Tenant.objects.filter(id=tenant.id).update(cost_exhausted_email_sent_at=timezone.now())
    logger.info(
        "billing_quota: exhausted email sent tenant=%s used_dollars=%s",
        str(tenant.id)[:8],
        ctx["used_dollars"],
    )
    return True


# ─────────────────────────────────────────────────────────────────────
# Threshold detector — shared by the reconcile cron + chat-completion drain
# ─────────────────────────────────────────────────────────────────────


def fire_threshold_emails_if_crossed(tenant: Tenant, *, before: Decimal, after: Decimal) -> dict:
    """Compare a tenant's pre/post ``estimated_cost_this_month`` against
    90% + 100% of their effective cap and fire the corresponding email
    when a threshold is crossed *upward* this update.

    Designed to be called from any code path that mutates the counter
    (the reconcile cron, the in-flight 402 detector). Idempotency is
    fully delegated to the underlying email helpers — repeat calls in
    the same cycle are no-ops.

    Returns a small audit dict ``{"warn": bool, "exhausted": bool}``.
    """
    try:
        cap = Decimal(str(tenant.effective_cost_budget))
    except Exception:
        return {"warn": False, "exhausted": False}
    if cap <= 0:
        return {"warn": False, "exhausted": False}

    warn_threshold = cap * Decimal("0.90")
    warn_fired = False
    exhausted_fired = False

    if before < warn_threshold <= after:
        warn_fired = send_cost_approaching_email(tenant)

    if before < cap <= after:
        exhausted_fired = send_cost_exhausted_email(tenant)

    return {"warn": warn_fired, "exhausted": exhausted_fired}
