from __future__ import annotations

import logging
from typing import Literal

import requests
from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger(__name__)

DeliveryClassification = Literal[
    "delivered",
    "timeout",
    "transient",
    "undeliverable",
]


def _send_telegram(subject: str, text: str) -> DeliveryClassification:
    token = getattr(settings, "STEWARD_TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = getattr(settings, "STEWARD_TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return "undeliverable"

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": f"{subject}\n\n{text}",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
    except requests.Timeout:
        logger.warning("Steward Telegram urgent timed out")
        return "timeout"
    except requests.ConnectionError:
        logger.warning("Steward Telegram urgent connection failed")
        return "transient"
    except requests.RequestException as exc:
        logger.warning(
            "Steward Telegram urgent request failed error_class=%s",
            type(exc).__name__,
        )
        return "transient"

    if response.status_code == 200:
        try:
            if response.json().get("ok") is True:
                return "delivered"
        except ValueError:
            pass
        return "transient"
    if 400 <= response.status_code < 500:
        logger.error("Steward Telegram urgent rejected with HTTP %d", response.status_code)
        return "undeliverable"
    if response.status_code >= 500:
        logger.warning("Steward Telegram urgent returned HTTP %d", response.status_code)
        return "timeout"
    return "transient"


def _send_mailgun_fallback(subject: str, text: str) -> DeliveryClassification:
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
            "Steward Mailgun fallback failed error_class=%s",
            type(exc).__name__,
        )
        return "transient"
    if sent:
        return "delivered"
    logger.error("Steward Mailgun fallback reported zero deliveries")
    return "undeliverable"


def send_urgent(subject: str, text: str, fingerprint: str) -> str:
    """Deliver an urgent directly, with no dependency on an agent or gateway."""
    try:
        telegram_status = _send_telegram(subject, text)
    except Exception as exc:
        logger.error(
            "Steward Telegram urgent failed unexpectedly error_class=%s",
            type(exc).__name__,
        )
        telegram_status = "transient"
    if telegram_status == "delivered":
        logger.info("Steward urgent delivered fingerprint=%s", fingerprint)
        return telegram_status

    fallback_status = _send_mailgun_fallback(subject, text)
    if fallback_status == "delivered":
        logger.info(
            "Steward urgent delivered by fallback fingerprint=%s primary=%s",
            fingerprint,
            telegram_status,
        )
        return fallback_status

    token = getattr(settings, "STEWARD_TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = getattr(settings, "STEWARD_TELEGRAM_CHAT_ID", "").strip()
    recipient = getattr(settings, "STEWARD_ALERT_EMAIL", "").strip()
    if not (token and chat_id) and not recipient:
        logger.error(
            "Steward urgent undeliverable: no direct channel configured fingerprint=%s",
            fingerprint,
        )
        return "undeliverable"

    logger.error(
        "Steward urgent delivery failed fingerprint=%s telegram=%s fallback=%s",
        fingerprint,
        telegram_status,
        fallback_status,
    )
    if fallback_status == "transient":
        return "transient"
    return telegram_status
