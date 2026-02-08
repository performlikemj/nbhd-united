"""Telegram webhook endpoint — routes to correct OpenClaw instance."""
import asyncio
import json
import logging

from django.conf import settings
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .services import extract_chat_id, forward_to_openclaw, resolve_container, send_onboarding_link

logger = logging.getLogger(__name__)


@csrf_exempt
@require_POST
def telegram_webhook(request):
    """Receive Telegram updates and route to the correct OpenClaw instance.

    This is the single webhook endpoint for the shared Telegram bot.
    It looks up chat_id → container and forwards the update.
    """
    # Verify webhook secret
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if secret != settings.TELEGRAM_WEBHOOK_SECRET:
        return HttpResponseForbidden("Invalid secret")

    try:
        update = json.loads(request.body)
    except json.JSONDecodeError:
        return HttpResponseBadRequest("Invalid JSON")

    chat_id = extract_chat_id(update)
    if not chat_id:
        return HttpResponse("ok")

    # Look up container for this chat_id
    container_fqdn = resolve_container(chat_id)

    if not container_fqdn:
        # Unknown user — send onboarding link
        response_data = send_onboarding_link(chat_id)
        # We could call Telegram API directly here, or just return
        # For now, log it
        logger.info("Unknown chat_id %s, sending onboarding link", chat_id)
        return JsonResponse(response_data)

    # Forward to the correct OpenClaw instance
    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(forward_to_openclaw(container_fqdn, update))
    finally:
        loop.close()

    if result:
        return JsonResponse(result)
    return HttpResponse("ok")
