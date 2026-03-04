"""
LINE Account Linking Service — deep link onboarding for NBHD United.

Mirrors the Telegram linking flow in apps/tenants/telegram_service.py.

Handles:
- Generating one-time link tokens for LINE
- Building LINE deep links
- Processing link tokens sent via LINE message
- Unlinking LINE accounts
"""
from __future__ import annotations

import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.tenants.models import User
from apps.tenants.line_models import LineLinkToken

logger = logging.getLogger(__name__)

# Bot basic ID — override via LINE_BOT_ID in settings (e.g. "@nbhd-united")
# Maps to LINE_BOT_ID env var in settings.py

# Token TTL
TOKEN_EXPIRY_MINUTES = 15


def generate_link_token(user: User) -> LineLinkToken:
    """Generate a one-time linking token for a user."""
    token_value = secrets.token_urlsafe(32)
    expires_at = timezone.now() + timedelta(minutes=TOKEN_EXPIRY_MINUTES)

    return LineLinkToken.objects.create(
        user=user,
        token=token_value,
        expires_at=expires_at,
    )


def get_deep_link(token: str) -> str:
    """Build LINE deep link for account linking.

    Format: https://line.me/R/oaMessage/{bot_id}/?link_{token}
    The user taps this, LINE opens, and the bot receives the token as a message.
    """
    bot_id = getattr(settings, "LINE_BOT_ID", "")
    return f"https://line.me/R/oaMessage/{bot_id}/?link_{token}"


def get_qr_code_data_url(token: str) -> str:
    """Generate QR code as base64 data URL for the LINE deep link."""
    import base64
    from io import BytesIO

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


def process_line_link_token(
    line_user_id: str,
    line_display_name: str,
    token: str,
) -> tuple[bool, str]:
    """
    Process a link token received from LINE.

    User sends a message starting with "link_TOKEN" in LINE.
    Validates token, links LINE account to the NBHD user.

    Returns:
        (success: bool, message: str)
    """
    # Find the token
    try:
        link_token = LineLinkToken.objects.select_related("user").get(token=token)
    except LineLinkToken.DoesNotExist:
        return False, "Invalid or expired link. Please generate a new one from the dashboard."

    if not link_token.is_valid:
        return False, "This link has expired. Please generate a new one from the dashboard."

    user = link_token.user

    # Check if this LINE user is already linked to another account
    existing = User.objects.filter(line_user_id=line_user_id).exclude(id=user.id).first()
    if existing:
        return False, "This LINE account is already linked to another user."

    # Link it
    user.line_user_id = line_user_id
    user.line_display_name = line_display_name or ""
    if line_display_name and user.display_name == "Friend":
        user.display_name = line_display_name
    user.save(update_fields=[
        "line_user_id", "line_display_name", "display_name",
    ])

    # Mark token as used
    link_token.used = True
    link_token.save(update_fields=["used"])

    logger.info("Linked LINE user %s to NBHD user %s", line_user_id, user.id)

    # If tenant has an active container, push updated config
    _trigger_config_update_if_active(user)

    return True, f"✅ Welcome, {user.display_name}! Your LINE account is now connected."


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


def unlink_line(user: User) -> bool:
    """Remove LINE link from a user. Returns True if was linked."""
    if not user.line_user_id:
        return False

    user.line_user_id = None
    user.line_display_name = ""
    user.save(update_fields=["line_user_id", "line_display_name"])

    # If user's preferred channel was LINE, switch back to telegram
    if user.preferred_channel == "line":
        user.preferred_channel = "telegram"
        user.save(update_fields=["preferred_channel"])

    logger.info("Unlinked LINE for user %s", user.id)
    return True


def get_line_status(user: User) -> dict:
    """Return LINE linking status for the API."""
    if user.line_user_id:
        return {
            "linked": True,
            "line_display_name": user.line_display_name,
        }
    return {"linked": False}
