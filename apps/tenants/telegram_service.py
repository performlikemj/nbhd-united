"""
Telegram Link Service — QR code / deep link onboarding for NBHD United.

Handles:
- Generating one-time link tokens
- Building QR codes and deep links
- Processing /start commands to complete account linking
- Unlinking Telegram accounts
"""
from __future__ import annotations

import base64
import logging
import secrets
from datetime import timedelta
from io import BytesIO

from django.conf import settings
from django.utils import timezone

from .models import User
from .telegram_models import TelegramLinkToken

logger = logging.getLogger(__name__)

# Bot username — override via TELEGRAM_BOT_USERNAME in settings
BOT_USERNAME = getattr(settings, "TELEGRAM_BOT_USERNAME", "NbhdUnitedBot")

# Token TTL
TOKEN_EXPIRY_MINUTES = 15


def generate_link_token(user: User) -> TelegramLinkToken:
    """Generate a one-time linking token for a user."""
    token_value = secrets.token_urlsafe(32)
    expires_at = timezone.now() + timedelta(minutes=TOKEN_EXPIRY_MINUTES)

    return TelegramLinkToken.objects.create(
        user=user,
        token=token_value,
        expires_at=expires_at,
    )


def get_deep_link(token: str) -> str:
    """Build t.me deep link: https://t.me/BotName?start=TOKEN."""
    return f"https://t.me/{BOT_USERNAME}?start={token}"


def get_qr_code_data_url(token: str) -> str:
    """Generate QR code as base64 data URL for the deep link."""
    import qrcode  # lazy import — only needed here

    deep_link = get_deep_link(token)

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(deep_link)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)

    b64 = base64.b64encode(buffer.getvalue()).decode("utf-8")
    return f"data:image/png;base64,{b64}"


def process_start_token(
    telegram_user_id: int,
    telegram_chat_id: int,
    telegram_username: str,
    telegram_first_name: str,
    token: str,
) -> tuple[bool, str]:
    """
    Process /start TOKEN from Telegram.

    Validates token, links Telegram account to the user.

    Returns:
        (success: bool, message: str)
    """
    # Find the token
    try:
        link_token = TelegramLinkToken.objects.select_related("user").get(token=token)
    except TelegramLinkToken.DoesNotExist:
        return False, "Invalid or expired link. Please generate a new one from the dashboard."

    if not link_token.is_valid:
        return False, "This link has expired. Please generate a new one from the dashboard."

    user = link_token.user

    # Check if this Telegram user is already linked to another account
    existing = User.objects.filter(telegram_user_id=telegram_user_id).exclude(id=user.id).first()
    if existing:
        return False, "This Telegram account is already linked to another user."

    # Link it
    user.telegram_user_id = telegram_user_id
    user.telegram_chat_id = telegram_chat_id
    user.telegram_username = telegram_username or ""
    if telegram_first_name and user.display_name == "Friend":
        user.display_name = telegram_first_name
    user.save(update_fields=[
        "telegram_user_id", "telegram_chat_id", "telegram_username", "display_name",
    ])

    # Mark token as used
    link_token.used = True
    link_token.save(update_fields=["used"])

    logger.info("Linked Telegram user %s to NBHD user %s", telegram_user_id, user.id)

    # If tenant has an active container, push updated config with the new chat_id
    _trigger_config_update_if_active(user)

    return True, f"✅ Welcome, {user.display_name}! Your Telegram is now connected."


def _trigger_config_update_if_active(user: User) -> None:
    """If the user's tenant has an active container, trigger a config update."""
    from apps.tenants.models import Tenant

    try:
        tenant = Tenant.objects.get(user=user)
    except Tenant.DoesNotExist:
        return

    if tenant.status == Tenant.Status.ACTIVE and tenant.container_id:
        from apps.cron.publish import publish_task
        publish_task("update_tenant_config", str(tenant.id))


def unlink_telegram(user: User) -> bool:
    """Remove Telegram link from a user. Returns True if was linked."""
    if not user.telegram_user_id:
        return False

    user.telegram_user_id = None
    user.telegram_chat_id = None
    user.telegram_username = ""
    user.save(update_fields=["telegram_user_id", "telegram_chat_id", "telegram_username"])

    logger.info("Unlinked Telegram for user %s", user.id)
    return True


def get_telegram_status(user: User) -> dict:
    """Return linking status for the API."""
    if user.telegram_user_id:
        return {
            "linked": True,
            "telegram_username": user.telegram_username,
            "telegram_chat_id": user.telegram_chat_id,
        }
    return {"linked": False}
