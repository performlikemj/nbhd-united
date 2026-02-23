"""Smart container update logic for tenant OpenClaw containers.

Checks if a tenant's container is running an outdated image and either
auto-updates (if idle 2+ hours) or asks the user with inline buttons.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.utils import timezone

from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

# How long since last message before we consider the user "idle" enough
# for a silent update.
SILENT_UPDATE_IDLE_THRESHOLD = timedelta(hours=2)

# Cooldown after a failed update attempt or user declining
UPDATE_COOLDOWN = timedelta(hours=1)

# ACR image path template
ACR_IMAGE_TEMPLATE = "nbhdunited.azurecr.io/nbhd-openclaw:{tag}"

# In-memory cooldown tracker: tenant_id → datetime when cooldown expires
_update_cooldowns: dict[int, Any] = {}


def get_latest_image_tag() -> str:
    """Return the latest OpenClaw image tag from Django settings (set by CI)."""
    return getattr(settings, "OPENCLAW_IMAGE_TAG", "latest") or "latest"


def is_container_outdated(tenant: Tenant) -> bool:
    """Check if a tenant's container is running an older image than latest."""
    latest = get_latest_image_tag()
    if latest == "latest":
        # Can't compare — CI hasn't set a SHA yet
        return False

    current = tenant.container_image_tag or ""
    if not current or current == "latest":
        # Unknown current tag — assume outdated
        return True

    return current != latest


def is_idle_enough_for_silent_update(tenant: Tenant) -> bool:
    """Check if the user has been idle long enough for a silent update."""
    if not tenant.last_message_at:
        return True  # Never messaged — safe to update

    idle_since = timezone.now() - tenant.last_message_at
    return idle_since >= SILENT_UPDATE_IDLE_THRESHOLD


def update_container(tenant: Tenant) -> bool:
    """Update a tenant's container to the latest image.

    Returns True if update was initiated successfully, False otherwise.
    """
    from apps.orchestrator.azure_client import update_container_image

    if not tenant.container_id:
        logger.warning("No container_id for tenant %s — can't update", tenant.id)
        return False

    latest_tag = get_latest_image_tag()
    image = ACR_IMAGE_TEMPLATE.format(tag=latest_tag)

    try:
        update_container_image(tenant.container_id, image)
        tenant.container_image_tag = latest_tag
        tenant.save(update_fields=["container_image_tag", "updated_at"])
        logger.info(
            "Updated container %s to image tag %s",
            tenant.container_id, latest_tag,
        )
        return True
    except Exception:
        logger.exception("Failed to update container %s", tenant.container_id)
        return False


def build_update_prompt(lang: str = "en") -> dict[str, Any]:
    """Build the update prompt message with inline buttons.

    Returns dict with 'text' and 'reply_markup' for Telegram.
    """
    messages = {
        "en": "🔄 There's an update available with new features! Quick restart takes about 15 seconds. OK to update now?",
        "ja": "🔄 新しい機能のアップデートがあります！再起動は約15秒です。今すぐ更新してもよろしいですか？",
        "es": "🔄 ¡Hay una actualización disponible con nuevas funciones! El reinicio toma unos 15 segundos. ¿Actualizar ahora?",
    }

    buttons = {
        "en": [
            {"text": "✅ Update now", "callback_data": "container_update:yes"},
            {"text": "⏳ Later", "callback_data": "container_update:no"},
        ],
        "ja": [
            {"text": "✅ 今すぐ更新", "callback_data": "container_update:yes"},
            {"text": "⏳ あとで", "callback_data": "container_update:no"},
        ],
        "es": [
            {"text": "✅ Actualizar ahora", "callback_data": "container_update:yes"},
            {"text": "⏳ Más tarde", "callback_data": "container_update:no"},
        ],
    }

    lang_key = lang[:2].lower() if lang else "en"
    if lang_key not in messages:
        lang_key = "en"

    return {
        "text": messages[lang_key],
        "reply_markup": {
            "inline_keyboard": [buttons[lang_key]],
        },
    }


def _is_on_cooldown(tenant: Tenant) -> bool:
    """Check if this tenant is on update cooldown."""
    expires = _update_cooldowns.get(tenant.id)
    if expires and timezone.now() < expires:
        return True
    # Expired — clean up
    _update_cooldowns.pop(tenant.id, None)
    return False


def _set_cooldown(tenant: Tenant) -> None:
    """Set update cooldown for this tenant."""
    _update_cooldowns[tenant.id] = timezone.now() + UPDATE_COOLDOWN


def check_and_maybe_update(tenant: Tenant) -> dict[str, Any] | None:
    """Main entry point: check if update needed and decide action.

    Returns:
        None — no update needed
        {"action": "silent_update"} — update happening silently
        {"action": "ask_user", "text": ..., "reply_markup": ...} — ask user first
    """
    if not is_container_outdated(tenant):
        return None

    if _is_on_cooldown(tenant):
        return None  # Don't spam — wait for cooldown to expire

    if is_idle_enough_for_silent_update(tenant):
        success = update_container(tenant)
        if success:
            return {"action": "silent_update"}
        _set_cooldown(tenant)  # Failed — back off
        return None

    # User was recently active — ask them (and set cooldown so we don't re-ask on next message)
    _set_cooldown(tenant)
    lang = getattr(tenant.user, "language", "en") or "en"
    prompt = build_update_prompt(lang)
    return {
        "action": "ask_user",
        "text": prompt["text"],
        "reply_markup": prompt["reply_markup"],
    }


def handle_update_callback(tenant: Tenant, callback_data: str) -> str | None:
    """Handle user's response to the update prompt.

    Returns a text response to send back, or None.
    """
    if callback_data == "container_update:yes":
        success = update_container(tenant)
        if success:
            lang = getattr(tenant.user, "language", "en") or "en"
            msgs = {
                "en": "✅ Updating now! I'll be back in about 15 seconds...",
                "ja": "✅ 更新中です！約15秒でお戻りします...",
                "es": "✅ ¡Actualizando! Volveré en unos 15 segundos...",
            }
            lang_key = lang[:2].lower() if lang in msgs else "en"
            return msgs.get(lang_key, msgs["en"])
        else:
            _set_cooldown(tenant)
            return "Sorry, the update failed. I'll try again later."

    elif callback_data == "container_update:no":
        _set_cooldown(tenant)
        lang = getattr(tenant.user, "language", "en") or "en"
        msgs = {
            "en": "👍 No problem! I'll ask again later.",
            "ja": "👍 了解です！また後で聞きますね。",
            "es": "👍 ¡Sin problema! Preguntaré más tarde.",
        }
        lang_key = lang[:2].lower() if lang in msgs else "en"
        return msgs.get(lang_key, msgs["en"])

    return None
