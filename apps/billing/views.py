import json
import logging

from django.conf import settings
from django.http import HttpResponse, HttpResponseBadRequest
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

logger = logging.getLogger(__name__)


@csrf_exempt
@require_POST
def stripe_webhook(request):
    """Handle Stripe webhook events.

    dj-stripe handles most sync automatically; this is for custom logic.
    """
    # dj-stripe's built-in webhook handling covers most cases.
    # Add custom event handling here if needed.
    try:
        event = json.loads(request.body)
        event_type = event.get("type", "")
        logger.info("Stripe webhook received: %s", event_type)
    except json.JSONDecodeError:
        return HttpResponseBadRequest("Invalid JSON")

    return HttpResponse(status=200)
