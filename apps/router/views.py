"""Telegram webhook endpoint — routes to correct OpenClaw instance."""
import asyncio
import hmac
import json
import logging

from django.conf import settings
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .services import (
    extract_chat_id,
    forward_to_openclaw,
    handle_start_command,
    is_rate_limited,
    resolve_container,
    send_onboarding_link,
    send_temporary_error,
)

logger = logging.getLogger(__name__)


@csrf_exempt
@require_POST
def telegram_webhook(request):
    """Receive Telegram updates and route to the correct OpenClaw instance.

    This is the single webhook endpoint for the shared Telegram bot.
    It looks up chat_id → container and forwards the update.
    """
    # Verify webhook secret
    configured_secret = (settings.TELEGRAM_WEBHOOK_SECRET or "").strip()
    if not configured_secret:
        logger.error("TELEGRAM_WEBHOOK_SECRET is not configured")
        return HttpResponse("Webhook secret not configured", status=503)

    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(secret, configured_secret):
        return HttpResponseForbidden("Invalid secret")

    try:
        update = json.loads(request.body)
    except json.JSONDecodeError:
        return HttpResponseBadRequest("Invalid JSON")

    # Handle /start TOKEN for account linking (before routing)
    link_response = handle_start_command(update)
    if link_response:
        return JsonResponse(link_response)

    chat_id = extract_chat_id(update)
    if not chat_id:
        return HttpResponse("ok")

    if is_rate_limited(chat_id):
        logger.warning("Rate limited chat_id %s", chat_id)
        return HttpResponse("Too many requests", status=429)

    # Look up container for this chat_id
    container_fqdn = resolve_container(chat_id)

    if not container_fqdn:
        # Unknown user — send onboarding link
        response_data = send_onboarding_link(chat_id)
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
    # Forwarding failed (timeout or error) — tell the user to retry
    return JsonResponse(send_temporary_error(chat_id))
