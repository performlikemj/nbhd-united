"""One-click email-unsubscribe HTTP surface.

``GET  /api/v1/tenants/unsubscribe/<token>/`` renders a minimal branded
confirmation page with a single "Unsubscribe" button that POSTs back to
the same URL. GET is deliberately **read-only** — it does NOT set the
opt-out flag. Mail-security scanners (Microsoft SafeLinks, Proofpoint,
etc.) fetch every link in a message with a GET; mutating on GET would
silently unsubscribe users who never clicked.

``POST /api/v1/tenants/unsubscribe/<token>/`` performs the actual opt-out.
It serves two callers: the confirmation page's button, and the RFC 8058
one-click flow named by the ``List-Unsubscribe`` / ``List-Unsubscribe-Post``
headers the campaign send sets (the mail client POSTs it automatically).

The token carries an opt-out **category** (see ``unsubscribe_signing.py``):
``marketing`` (the default; every pre-category token parses as marketing)
sets ``User.email_opt_out``; ``service`` sets ``User.service_email_opt_out``
for operational notices. Auth-critical mail (password reset) honors neither.

Both verbs are unauthenticated by design — clicking from an inbox can't
carry a session — with authorization carried entirely by the per-user
HMAC token (signed in ``unsubscribe_signing.py``). POST is idempotent: a
second POST for an already-opted-out user succeeds without changing the
original opt-out timestamp. An invalid or tampered token returns 404 on
either verb, with no information about whether the user exists.
"""

from __future__ import annotations

import logging

from django.http import Http404
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from apps.tenants.models import User
from apps.tenants.unsubscribe_signing import CATEGORY_SERVICE, parse_unsubscribe_token

logger = logging.getLogger(__name__)


def _resolve_user(token: str) -> tuple[User, str]:
    """Verify the token and return ``(user, category)`` WITHOUT mutating.

    Raises ``Http404`` on any verification failure or unknown user so the
    caller returns 404 without leaking whether the token maps to a real
    account. Safe to call on GET — no side effects.
    """
    parsed = parse_unsubscribe_token(token)
    if parsed is None:
        raise Http404("Invalid unsubscribe token")
    user_id, category = parsed

    try:
        return User.objects.get(id=user_id), category
    except (User.DoesNotExist, ValueError):
        # ValueError catches a malformed UUID from a tampered token that
        # somehow survived signature verification (shouldn't happen).
        raise Http404("Invalid unsubscribe token")


def _set_opt_out(user: User, category: str) -> None:
    """Set the category's opt-out flag idempotently. Only called on POST."""
    if category == CATEGORY_SERVICE:
        if not user.service_email_opt_out:
            user.service_email_opt_out = True
            user.service_email_opt_out_at = timezone.now()
            user.save(update_fields=["service_email_opt_out", "service_email_opt_out_at"])
            logger.info("Service-notice unsubscribe applied for user %s", user.id)
        return
    if not user.email_opt_out:
        user.email_opt_out = True
        user.email_opt_out_at = timezone.now()
        user.save(update_fields=["email_opt_out", "email_opt_out_at"])
        logger.info("Email unsubscribe applied for user %s", user.id)


@csrf_exempt
@require_http_methods(["GET", "POST"])
def unsubscribe(request, token: str):
    """Confirm (GET, read-only) or apply (POST) an email opt-out."""
    user, category = _resolve_user(token)
    context = {
        "display_name": getattr(user, "display_name", None) or "there",
        "is_service": category == CATEGORY_SERVICE,
    }

    if request.method == "POST":
        _set_opt_out(user, category)
        context["confirmed"] = True
        return render(request, "tenants/unsubscribe_confirm.html", context)

    # GET: render the confirmation page with a one-button POST form.
    # No mutation — scanners that GET this link must not opt anyone out.
    context["confirmed"] = False
    return render(request, "tenants/unsubscribe_confirm.html", context)
