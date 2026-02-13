"""Stripe webhook handler and billing views."""
import logging

import stripe
from django.conf import settings
from django.http import HttpResponse, HttpResponseBadRequest
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from rest_framework import status as http_status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.tenants.models import Tenant
from .services import (
    handle_checkout_completed,
    handle_invoice_payment_failed,
    handle_subscription_deleted,
)

logger = logging.getLogger(__name__)


def _get_stripe_api_key() -> str:
    """Return the Stripe API key matching the configured mode."""
    if settings.STRIPE_LIVE_MODE:
        return settings.STRIPE_LIVE_SECRET_KEY
    return settings.STRIPE_TEST_SECRET_KEY


def _require_stripe_api_key() -> str | None:
    """Return configured Stripe API key or None if Stripe is not configured."""
    api_key = (_get_stripe_api_key() or "").strip()
    if not api_key:
        logger.error("Stripe API key missing (STRIPE_LIVE_MODE=%s)", settings.STRIPE_LIVE_MODE)
        return None
    return api_key


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
        case "invoice.payment_failed":
            handle_invoice_payment_failed(data)
        case _:
            logger.debug("Unhandled Stripe event: %s", event_type)

    return HttpResponse(status=200)


class StripePortalView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        api_key = _require_stripe_api_key()
        if not api_key:
            return Response(
                {"detail": "Stripe is not configured."},
                status=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        try:
            tenant = request.user.tenant
        except Tenant.DoesNotExist:
            return Response(
                {"detail": "No tenant found."},
                status=http_status.HTTP_404_NOT_FOUND,
            )

        if not tenant.stripe_customer_id:
            return Response(
                {"detail": "No Stripe customer linked."},
                status=http_status.HTTP_400_BAD_REQUEST,
            )

        session = stripe.billing_portal.Session.create(
            customer=tenant.stripe_customer_id,
            return_url=f"{settings.FRONTEND_URL}/billing",
            api_key=api_key,
        )
        return Response({"url": session.url})


class StripeCheckoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        api_key = _require_stripe_api_key()
        if not api_key:
            return Response(
                {"detail": "Stripe is not configured."},
                status=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        tier = request.data.get("tier", "basic")
        price_id = settings.STRIPE_PRICE_IDS.get(tier)

        if not price_id:
            return Response(
                {"detail": f"Unknown tier: {tier}"},
                status=http_status.HTTP_400_BAD_REQUEST,
            )

        user = request.user
        customer_email = user.email

        metadata = {"user_id": str(user.id), "tier": tier}
        try:
            tenant = user.tenant
            metadata["tenant_id"] = str(tenant.id)
        except Tenant.DoesNotExist:
            pass

        session = stripe.checkout.Session.create(
            customer_email=customer_email,
            line_items=[{"price": price_id, "quantity": 1}],
            mode="subscription",
            success_url=f"{settings.FRONTEND_URL}/onboarding?checkout=success",
            cancel_url=f"{settings.FRONTEND_URL}/billing?checkout=cancelled",
            metadata=metadata,
            consent_collection={"terms_of_service": "required"},
            custom_text={
                "terms_of_service_acceptance": {
                    "message": f"I agree to the [Terms of Service]({settings.FRONTEND_URL}/legal/terms)"
                }
            },
            api_key=api_key,
        )
        return Response({"url": session.url})
