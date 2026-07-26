from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass

import stripe
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.mail import EmailMultiAlternatives
from django.db import IntegrityError, transaction
from django.template.loader import render_to_string
from django.utils import timezone

from apps.tenants.models import Tenant

from .models import License, LicenseActivation

logger = logging.getLogger(__name__)

LICENSE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
LICENSE_BODY_LENGTH = 12
LICENSE_SEAT_LIMIT = 3
LICENSE_KEY_GENERATION_ATTEMPTS = 5


class CheckoutSessionRejected(Exception):
    """The Checkout Session is not a paid YardTalk purchase."""


@dataclass(frozen=True)
class VerifiedCheckoutSession:
    session_id: str
    purchaser_email: str
    customer_id: str
    payment_intent_id: str


def generate_license_key() -> str:
    body = "".join(secrets.choice(LICENSE_ALPHABET) for _ in range(LICENSE_BODY_LENGTH))
    return f"YT-{body[:4]}-{body[4:8]}-{body[8:]}"


def license_receipt(license_key: str, device_id: str) -> str:
    secret = settings.YARDTALK_LICENSE_RECEIPT_SECRET
    if not secret:
        raise ImproperlyConfigured("YARDTALK_LICENSE_RECEIPT_SECRET is not configured")
    message = f"{license_key}:{device_id}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def activate_license(license_key: str, device_id: str) -> dict:
    with transaction.atomic():
        try:
            license_obj = License.objects.select_for_update().get(key=license_key)
        except License.DoesNotExist:
            return {"valid": False, "reason": "unknown_key"}

        if license_obj.status == License.Status.REVOKED:
            return {"valid": False, "reason": "revoked"}

        activation = LicenseActivation.objects.filter(license=license_obj, device_id=device_id).first()
        activated_device_count = LicenseActivation.objects.filter(license=license_obj).count()
        if activation is None:
            if activated_device_count >= LICENSE_SEAT_LIMIT:
                return {"valid": False, "reason": "seat_limit"}
            LicenseActivation.objects.create(license=license_obj, device_id=device_id)
            activated_device_count += 1

    return {
        "valid": True,
        "receipt": license_receipt(license_obj.key, device_id),
        "seats_remaining": LICENSE_SEAT_LIMIT - activated_device_count,
    }


def user_has_active_subscription(user) -> bool:
    try:
        tenant = user.tenant
    except Tenant.DoesNotExist:
        return False
    return bool(tenant.status == Tenant.Status.ACTIVE and tenant.stripe_subscription_id and not tenant.is_trial)


def _stripe_api_key() -> str:
    if settings.STRIPE_LIVE_MODE:
        return settings.STRIPE_LIVE_SECRET_KEY
    return settings.STRIPE_TEST_SECRET_KEY


def _plain_dict(value):
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return value


def _stripe_id(value) -> str:
    value = _plain_dict(value)
    if isinstance(value, dict):
        return str(value.get("id") or "")
    return str(value or "")


def verify_checkout_session(session_id: str) -> VerifiedCheckoutSession:
    price_id = (settings.YARDTALK_STRIPE_PRICE_ID or "").strip()
    api_key = (_stripe_api_key() or "").strip()
    if not price_id or not api_key:
        raise ImproperlyConfigured("YardTalk Stripe checkout is not configured")

    raw_session = stripe.checkout.Session.retrieve(
        session_id,
        expand=["line_items.data.price"],
        api_key=api_key,
    )
    session = _plain_dict(raw_session)
    line_items = ((_plain_dict(session.get("line_items")) or {}).get("data")) or []
    purchased_prices = {_stripe_id((_plain_dict(item) or {}).get("price")) for item in line_items}

    if (
        session.get("id") != session_id
        or session.get("mode") != "payment"
        or session.get("payment_status") != "paid"
        or price_id not in purchased_prices
    ):
        raise CheckoutSessionRejected

    customer_details = _plain_dict(session.get("customer_details")) or {}
    purchaser_email = (customer_details.get("email") or session.get("customer_email") or "").strip()
    if not purchaser_email:
        raise CheckoutSessionRejected

    return VerifiedCheckoutSession(
        session_id=session_id,
        purchaser_email=purchaser_email,
        customer_id=_stripe_id(session.get("customer")),
        payment_intent_id=_stripe_id(session.get("payment_intent")),
    )


def issue_license(checkout: VerifiedCheckoutSession) -> tuple[License, bool]:
    existing = License.objects.filter(stripe_session_id=checkout.session_id).first()
    if existing is not None:
        return existing, False

    for _ in range(LICENSE_KEY_GENERATION_ATTEMPTS):
        try:
            with transaction.atomic():
                license_obj = License.objects.create(
                    key=generate_license_key(),
                    purchaser_email=checkout.purchaser_email,
                    stripe_session_id=checkout.session_id,
                    stripe_customer_id=checkout.customer_id,
                    stripe_payment_intent_id=checkout.payment_intent_id,
                )
            return license_obj, True
        except IntegrityError:
            existing = License.objects.filter(stripe_session_id=checkout.session_id).first()
            if existing is not None:
                return existing, False

    raise RuntimeError("Unable to generate a unique YardTalk license key")


def send_license_email(license_obj: License) -> bool:
    context = {"license_key": license_obj.key, "seat_limit": LICENSE_SEAT_LIMIT}
    try:
        subject = render_to_string("email/yardtalk/license_key_subject.txt", context).strip()
        text_body = render_to_string("email/yardtalk/license_key_body.txt", context)
        html_body = render_to_string("email/yardtalk/license_key_body.html", context)
        message = EmailMultiAlternatives(
            subject=subject,
            body=text_body,
            to=[license_obj.purchaser_email],
        )
        message.attach_alternative(html_body, "text/html")
        message.send(fail_silently=False)
    except Exception:
        logger.exception("YardTalk license email failed for Checkout Session %s", license_obj.stripe_session_id)
        return False

    sent_at = timezone.now()
    License.objects.filter(pk=license_obj.pk).update(key_email_sent_at=sent_at)
    license_obj.key_email_sent_at = sent_at
    return True


def fulfill_checkout_session(session_id: str) -> License:
    checkout = verify_checkout_session(session_id)
    license_obj, created = issue_license(checkout)
    if created:
        send_license_email(license_obj)
    return license_obj


def handle_yardtalk_checkout_completed(session_data: dict) -> None:
    session_id = str(session_data.get("id") or "")
    if not session_id:
        logger.warning("YardTalk Checkout Session webhook had no session id")
        return
    try:
        fulfill_checkout_session(session_id)
    except CheckoutSessionRejected:
        logger.info("Checkout Session %s is not a paid YardTalk purchase", session_id)
