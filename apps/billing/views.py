"""Stripe webhook handler."""
import json
import logging

import stripe
from django.conf import settings
from django.http import HttpResponse, HttpResponseBadRequest
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .services import handle_checkout_completed, handle_subscription_deleted

logger = logging.getLogger(__name__)


@csrf_exempt
@require_POST
def stripe_webhook(request):
    """Handle Stripe webhook events."""
    payload = request.body
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.DJSTRIPE_WEBHOOK_SECRET,
        )
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        logger.warning("Stripe webhook verification failed: %s", e)
        return HttpResponseBadRequest("Invalid signature")

    event_type = event["type"]
    data = event["data"]["object"]
    logger.info("Stripe webhook: %s", event_type)

    match event_type:
        case "checkout.session.completed":
            handle_checkout_completed(data)
        case "customer.subscription.deleted":
            handle_subscription_deleted(data)
        case "customer.subscription.updated":
            # Future: handle tier changes
            logger.info("Subscription updated: %s", data.get("id"))
        case _:
            logger.debug("Unhandled Stripe event: %s", event_type)

    return HttpResponse(status=200)
