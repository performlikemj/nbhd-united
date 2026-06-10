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

from .constants import CREDIT_PACKS
from .credits import credits_state, handle_credit_refund, handle_credit_topup_completed
from .services import (
    handle_checkout_completed,
    handle_invoice_payment_failed,
    handle_subscription_deleted,
)

logger = logging.getLogger(__name__)


def _billing_is_enabled() -> bool:
    """Whether paid billing flows should be allowed."""
    return bool(getattr(settings, "STRIPE_PRICE_ID", ""))


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


def _is_missing_customer_error(exc: "stripe.error.StripeError") -> bool:
    """True when Stripe rejected the ``customer`` param as non-existent.

    Fires on a stale ``stripe_customer_id`` — a customer that belongs to a
    different Stripe account or a different mode (test vs live) than the key we
    call with. The canonical case: a tenant carries a customer/subscription from
    an OLD account or from test mode, then the platform moves to a NEW live
    account (object-id suffixes encode the account, so the ids are literally
    cross-account). Stripe returns ``invalid_request_error`` /
    ``resource_missing`` with ``param='customer'`` (message "No such customer").
    Match on code+param first, fall back to the message so a future stripe-py
    that drops the structured fields still self-heals.
    """
    if getattr(exc, "code", "") == "resource_missing" and getattr(exc, "param", "") == "customer":
        return True
    return "No such customer" in str(exc)


def _clear_stale_customer(tenant: Tenant) -> None:
    """Drop a stale ``stripe_customer_id`` so the next checkout creates a fresh
    customer in the CURRENT account/mode (the credit-topup webhook backfills the
    new id via the ``stripe_customer_id=""`` guard). Clears in-memory too so the
    caller's retry takes the ``customer_email`` branch. Best-effort: a failed
    write must not mask the original Stripe error path.
    """
    try:
        Tenant.objects.filter(id=tenant.id).update(stripe_customer_id="")
        tenant.stripe_customer_id = ""
        logger.warning(
            "billing: cleared stale stripe_customer_id for tenant %s (cross-account/mode customer) — "
            "next checkout creates a fresh customer",
            tenant.id,
        )
    except Exception:
        logger.exception("billing: failed to clear stale stripe_customer_id for tenant %s", tenant.id)


def _stripe_object_to_plain_dict(obj):
    """Convert a Stripe ``StripeObject`` to a plain dict.

    Required because stripe-py 15.x's ``StripeObject`` (returned by
    ``stripe.Webhook.construct_event`` for ``event["data"]["object"]``)
    is no longer a ``dict`` subclass, no longer a ``Mapping``, has no
    ``.keys``, ``.items``, or ``.get`` methods, and its ``__getattr__``
    intercepts any attribute lookup that misses the class definition.
    Result: ``session_data.get("metadata")`` in our handlers raises
    ``AttributeError: get`` — every real Stripe webhook to our endpoint
    would crash with HTTP 500.

    What 15.x's ``StripeObject`` *does* expose is a documented
    ``to_dict()`` method that returns a fully-coerced plain dict
    (nested ``StripeObject``s become plain dicts too). That's the
    boundary we use here. It works on 14.x too — both versions ship
    the same public method.

    Observed via webhook signature test 2026-05-13 and confirmed by
    PR #539 CI failures iterating against stripe-py 15.0.1.
    """
    # `to_dict` is a real method on the StripeObject class hierarchy
    # (defined, not synthesised via __getattr__), so attribute lookup
    # resolves it via the normal MRO — no shadowing risk like .get.
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return obj


@csrf_exempt
@require_POST
def stripe_webhook(request):
    """Handle Stripe webhook events."""
    payload = request.body
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(
            payload,
            sig_header,
            settings.DJSTRIPE_WEBHOOK_SECRET,
        )
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        logger.warning("Stripe webhook verification failed: %s", e)
        return HttpResponseBadRequest("Invalid signature")

    # Stripe webhooks are unauthenticated (no JWT) so TenantContextMiddleware
    # does not fire.  Grant service-role access for the cross-tenant DB writes.
    from apps.tenants.middleware import set_rls_context

    set_rls_context(service_role=True)

    event_type = event["type"]
    # Real Stripe events always carry an id (the credit idempotency key). Read
    # defensively so a malformed/synthetic event can't 500 the endpoint — a
    # missing id only neutralises the credit-grant path (grant_credit refuses an
    # empty event id), which non-credit events don't use.
    try:
        event_id = event["id"]
    except (KeyError, TypeError):
        event_id = ""
    data = _stripe_object_to_plain_dict(event["data"]["object"])
    logger.info("Stripe webhook: %s", event_type)

    match event_type:
        case "checkout.session.completed" | "checkout.session.async_payment_succeeded":
            # Branch one-time credit top-ups off the subscription flow FIRST: a
            # mis-route into handle_checkout_completed would flip the tenant's
            # tier + reprovision. Credit grant is idempotent on event_id.
            meta = data.get("metadata") or {}
            if data.get("mode") == "payment" and meta.get("kind") == "credit_topup":
                handle_credit_topup_completed(event_id, data)
            elif event_type == "checkout.session.completed":
                handle_checkout_completed(data)
            else:
                logger.info("async_payment_succeeded for non-credit session %s", data.get("id"))
        case "charge.refunded":
            handle_credit_refund(event_id, data)
        case "charge.dispute.created":
            logger.warning("Stripe dispute opened (charge=%s) — manual review", data.get("id"))
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
        if not _billing_is_enabled():
            return Response(
                {"detail": "Billing is temporarily disabled. Enjoy your free trial."},
                status=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            )

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

        try:
            session = stripe.billing_portal.Session.create(
                customer=tenant.stripe_customer_id,
                return_url=f"{settings.FRONTEND_URL}/billing",
                api_key=api_key,
            )
        except stripe.error.StripeError as exc:
            # Stale customer (different account/mode after a Stripe migration): the
            # portal genuinely needs a live customer, so we can't transparently
            # retry. Clear the dead id and tell the user to relink — a new
            # subscription or top-up backfills a fresh customer in THIS account.
            if _is_missing_customer_error(exc):
                logger.warning("Stripe portal: stale customer for tenant %s — clearing + asking to relink", tenant.id)
                _clear_stale_customer(tenant)
                return Response(
                    {
                        "detail": "Your billing profile needs to be relinked. Start a subscription or credit top-up to reconnect."
                    },
                    status=http_status.HTTP_409_CONFLICT,
                )
            logger.error("Stripe portal error for tenant %s: %s", tenant.id, exc)
            return Response(
                {"detail": "Unable to open the billing portal right now. Please try again later."},
                status=http_status.HTTP_502_BAD_GATEWAY,
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

        if not _billing_is_enabled():
            return Response(
                {"detail": "Billing is temporarily disabled. Enjoy your free trial."},
                status=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        price_id = settings.STRIPE_PRICE_ID
        if not price_id:
            return Response(
                {"detail": "Stripe pricing is not configured."},
                status=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        user = request.user
        customer_email = user.email

        metadata = {"user_id": str(user.id), "tier": "starter"}
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


class CreditCheckoutView(APIView):
    """Start a one-time prepaid-credit top-up Checkout Session (mode=payment).

    The client may only pick a server-defined pack by id; the amount + granted
    credit are looked up from CREDIT_PACKS here AND re-derived in the webhook —
    never trusted from the client. Credit is granted only by the webhook, never
    on the success_url redirect.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        api_key = _require_stripe_api_key()
        if not api_key:
            return Response(
                {"detail": "Stripe is not configured."},
                status=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        # NOTE: gate on CREDIT_PACKS, NOT _billing_is_enabled() — the latter keys
        # off the subscription price; top-ups are purchasable independently.
        if not CREDIT_PACKS:
            return Response(
                {"detail": "Credit top-ups are not available."},
                status=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        try:
            tenant = request.user.tenant
        except Tenant.DoesNotExist:
            return Response({"detail": "No tenant found."}, status=http_status.HTTP_404_NOT_FOUND)

        pack_id = (request.data.get("pack_id") or "").strip()
        pack = CREDIT_PACKS.get(pack_id)
        if not pack:
            return Response({"detail": "Unknown credit pack."}, status=http_status.HTTP_400_BAD_REQUEST)

        # Stamp metadata on BOTH the session and the PaymentIntent: refund/dispute
        # events carry the PaymentIntent (not the session), so the webhook needs
        # it there to match the clawback back to the grant.
        meta = {
            "kind": "credit_topup",
            "pack_id": pack_id,
            "tenant_id": str(tenant.id),
            "user_id": str(request.user.id),
        }
        line_items = [
            {
                "price_data": {
                    "currency": "usd",
                    "unit_amount": pack["price_cents"],
                    "product_data": {"name": pack["label"]},
                },
                "quantity": 1,
            }
        ]

        def _create_session(customer):
            return stripe.checkout.Session.create(
                mode="payment",
                customer=customer,
                customer_email=None if customer else request.user.email,
                client_reference_id=str(tenant.id),
                line_items=line_items,
                metadata=meta,
                payment_intent_data={"metadata": meta},
                success_url=f"{settings.FRONTEND_URL}/settings/billing?topup=success",
                cancel_url=f"{settings.FRONTEND_URL}/settings/billing?topup=cancelled",
                api_key=api_key,
            )

        customer = tenant.stripe_customer_id or None
        try:
            session = _create_session(customer)
        except stripe.error.StripeError as exc:
            # A stale customer (different Stripe account/mode — e.g. an old test
            # customer after a live-account migration) self-heals: drop the id and
            # retry with the email so Checkout mints a fresh customer in THIS
            # account. The credit-topup webhook backfills the new id.
            if customer and _is_missing_customer_error(exc):
                logger.warning(
                    "Stripe credit checkout: stale customer %s for tenant %s — retrying without it",
                    customer,
                    tenant.id,
                )
                _clear_stale_customer(tenant)
                try:
                    session = _create_session(None)
                except stripe.error.StripeError as retry_exc:
                    logger.error("Stripe credit checkout retry error for tenant %s: %s", tenant.id, retry_exc)
                    return Response(
                        {"detail": "Unable to start checkout right now. Please try again later."},
                        status=http_status.HTTP_502_BAD_GATEWAY,
                    )
            else:
                logger.error("Stripe credit checkout error for tenant %s: %s", tenant.id, exc)
                return Response(
                    {"detail": "Unable to start checkout right now. Please try again later."},
                    status=http_status.HTTP_502_BAD_GATEWAY,
                )
        return Response({"url": session.url})


class CreditBalanceView(APIView):
    """Read the tenant's prepaid-credit balance, included-allowance usage,
    available packs, and recent ledger entries (for the Credits UI)."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            tenant = request.user.tenant
        except Tenant.DoesNotExist:
            return Response({"detail": "No tenant found."}, status=http_status.HTTP_404_NOT_FOUND)
        return Response(credits_state(tenant))
