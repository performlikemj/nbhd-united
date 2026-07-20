"""YardTalk licensing core: key generation, normalization, the entitlement
predicate, and the Stripe webhook mint + license-key email.

Platform-level (no tenant FK) — a YardTalk buyer need not be an nbhd subscriber.
The webhook mint is idempotent on the Checkout Session id; the key email fires
once per license (dunning pattern: try/except log-never-raise).
"""

from __future__ import annotations

import logging
import re
import secrets

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.billing.models import YardTalkLicense
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

# Crockford base32 minus I/L/O/U — unambiguous when read aloud or typed.
KEY_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
KEY_BODY_LEN = 12
DEVICE_SEAT_LIMIT = 3
LICENSE_RECEIPT_SALT = "nbhd.yardtalk.license.v1"

_STRIP_RE = re.compile(r"[\s\-]")


def normalize_license_key(raw: str) -> str:
    """Collapse a user-entered key to its compact comparison form: strip all
    whitespace and hyphens, uppercase. e.g. ``yt-abcd-2345-efgh`` -> ``YTABCD2345EFGH``.
    Also the basis for the per-key throttle bucket.
    """
    return _STRIP_RE.sub("", raw or "").upper()


def canonical_license_key(raw: str) -> str:
    """Return the canonical stored form ``YT-XXXX-XXXX-XXXX`` for a user-entered
    key, or the compact normalized string if it isn't a well-formed YT key (which
    then simply won't match any stored key -> ``unknown_key``).
    """
    compact = normalize_license_key(raw)
    if len(compact) == (2 + KEY_BODY_LEN) and compact.startswith("YT"):
        body = compact[2:]
        return f"YT-{body[0:4]}-{body[4:8]}-{body[8:12]}"
    return compact


def _generate_license_key() -> str:
    body = "".join(secrets.choice(KEY_ALPHABET) for _ in range(KEY_BODY_LEN))
    return f"YT-{body[0:4]}-{body[4:8]}-{body[8:12]}"


def is_yardtalk_entitled(tenant: Tenant | None) -> bool:
    """Does this tenant's paid nbhd subscription entitle free YardTalk?

    True when the tenant is ACTIVE and either budget-exempt (canary/internal) or
    carries a live subscription that is not a trial. This is deliberately NOT
    ``Tenant.has_entitlement`` (which counts trials as billing-entitled).
    """
    if tenant is None:
        return False
    if tenant.status != Tenant.Status.ACTIVE:
        return False
    if tenant.is_budget_exempt:
        return True
    return bool(tenant.stripe_subscription_id) and not tenant.is_trial


def send_license_key_email(license_obj: YardTalkLicense) -> bool:
    """Render + send the license-key email, stamping ``key_email_sent_at`` on a
    confirmed send. Dunning pattern: any failure is logged, never raised, so a
    mail hiccup can't 500 the webhook or roll back the mint. Returns True iff a
    fresh email went out.
    """
    from django.core.mail import EmailMultiAlternatives
    from django.template.loader import render_to_string

    email = (license_obj.email or "").strip()
    if not email:
        logger.error("yardtalk license: no email on license %s — cannot send key", license_obj.id)
        return False

    ctx = {"license_key": license_obj.key, "seat_limit": DEVICE_SEAT_LIMIT}
    try:
        subject = render_to_string("email/yardtalk/license_key_subject.txt", ctx).strip()
        text_body = render_to_string("email/yardtalk/license_key_body.txt", ctx)
        html_body = render_to_string("email/yardtalk/license_key_body.html", ctx)
        msg = EmailMultiAlternatives(subject=subject, body=text_body, from_email=None, to=[email])
        msg.attach_alternative(html_body, "text/html")
        msg.send(fail_silently=False)
    except Exception:
        logger.exception("yardtalk license: key email send failed (session=%s)", license_obj.stripe_session_id)
        return False

    stamped = timezone.now()
    YardTalkLicense.objects.filter(id=license_obj.id).update(key_email_sent_at=stamped)
    license_obj.key_email_sent_at = stamped
    logger.info("yardtalk license: key email sent (session=%s)", license_obj.stripe_session_id)
    return True


def _purchaser_email(session_data: dict) -> str:
    details = session_data.get("customer_details") or {}
    email = details.get("email") or session_data.get("customer_email") or ""
    return (email or "").strip()


def handle_yardtalk_license_completed(event_id: str, session_data: dict) -> None:
    """Mint a YardTalk license for a paid $20 Checkout Session and email the key.

    Idempotent on ``stripe_session_id``: a replayed event neither double-mints
    nor re-emails. Returns silently (caller returns HTTP 200) on unprocessable
    events so Stripe stops retrying permanently-bad ones.
    """
    session_id = session_data.get("id") or ""
    if not session_id:
        logger.error("yardtalk license: missing session id (event=%s) — not minting", event_id)
        return

    email = _purchaser_email(session_data)
    if not email:
        logger.error("yardtalk license: no purchaser email (session=%s) — not minting", session_id)
        return

    # Already minted (replay / concurrent redelivery) — do not re-mint or re-email.
    if YardTalkLicense.objects.filter(stripe_session_id=session_id).exists():
        logger.info("yardtalk license: already minted for session %s", session_id)
        return

    payment_intent = session_data.get("payment_intent") or ""
    license_obj = None
    for _ in range(5):
        candidate = _generate_license_key()
        try:
            with transaction.atomic():
                license_obj = YardTalkLicense.objects.create(
                    key=candidate,
                    email=email,
                    stripe_session_id=session_id,
                    stripe_payment_intent_id=payment_intent,
                )
            break
        except IntegrityError:
            # Either the session id collided (a concurrent mint won the race) or
            # the generated key collided. Session-id collision => already minted,
            # so stop and DON'T re-email; otherwise retry with a fresh key.
            if YardTalkLicense.objects.filter(stripe_session_id=session_id).exists():
                logger.info("yardtalk license: concurrent mint for session %s — skipping", session_id)
                return
            continue

    if license_obj is None:
        logger.error("yardtalk license: exhausted key-generation retries (session=%s)", session_id)
        return

    send_license_key_email(license_obj)
