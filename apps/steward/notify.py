from __future__ import annotations

import logging
from typing import Literal

from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger(__name__)

DeliveryClassification = Literal[
    "delivered",
    "timeout",
    "transient",
    "undeliverable",
]


def _send_email(subject: str, text: str) -> DeliveryClassification:
    recipient = getattr(settings, "STEWARD_ALERT_EMAIL", "").strip()
    if not recipient:
        return "undeliverable"
    try:
        sent = send_mail(
            subject=f"[Steward] {subject}",
            message=text,
            from_email=None,
            recipient_list=[recipient],
            fail_silently=False,
        )
    except Exception as exc:
        logger.error(
            "Steward email alert failed error_class=%s",
            type(exc).__name__,
        )
        return "transient"
    if sent:
        return "delivered"
    logger.error("Steward email alert reported zero deliveries")
    return "undeliverable"


def send_urgent(subject: str, text: str, fingerprint: str) -> str:
    """Deliver an urgent by email and record only confirmed delivery."""
    delivery = _send_email(subject, text)
    if delivery == "delivered":
        from apps.steward.gate import record_sent

        record_sent(fingerprint)
        logger.info("Steward urgent delivered fingerprint=%s", fingerprint)
        return delivery

    if not getattr(settings, "STEWARD_ALERT_EMAIL", "").strip():
        logger.error(
            "Steward urgent undeliverable: no email configured fingerprint=%s",
            fingerprint,
        )
        return "undeliverable"

    logger.error(
        "Steward urgent delivery failed fingerprint=%s classification=%s",
        fingerprint,
        delivery,
    )
    return delivery
