"""Telegram webhook endpoint."""
import json
import logging

from django.conf import settings
from django.http import HttpResponse, HttpResponseForbidden
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

logger = logging.getLogger(__name__)


@csrf_exempt
@require_POST
def telegram_webhook(request):
    """Receive updates from Telegram Bot API."""
    # Verify webhook secret
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if secret != settings.TELEGRAM_WEBHOOK_SECRET:
        return HttpResponseForbidden("Invalid secret")

    try:
        update = json.loads(request.body)
        logger.info("Telegram update: %s", update.get("update_id"))
        # In production, dispatch to async handler via Celery
        # For now, just acknowledge
    except json.JSONDecodeError:
        logger.warning("Invalid JSON from Telegram webhook")

    return HttpResponse("ok")
