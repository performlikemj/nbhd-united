"""Stripe webhook handler and billing views."""
import json
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
        )
        return Response({"url": session.url})


class StripeCheckoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
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
        )
        return Response({"url": session.url})
