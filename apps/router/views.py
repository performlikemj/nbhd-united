"""Telegram webhook endpoint — routes to correct OpenClaw instance."""

import asyncio
import hmac
import json
import logging

from django.conf import settings
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, HttpResponseNotFound, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.billing.services import check_budget, record_usage
from apps.tenants.models import Tenant

from .error_messages import error_msg
from .lesson_callbacks import handle_lesson_callback
from .services import (
    extract_chat_id,
    forward_to_openclaw,
    handle_start_command,
    is_rate_limited,
    resolve_tenant_by_chat_id,
    send_onboarding_link,
)

logger = logging.getLogger(__name__)


def _coerce_non_negative_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return 0
        try:
            parsed = int(value)
        except ValueError:
            try:
                parsed_float = float(value)
            except ValueError:
                return 0
            if parsed_float.is_integer():
                return int(parsed_float)
            return 0
        return parsed if parsed >= 0 else 0
    return 0


def _extract_usage_payload(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        return {}

    usage_payload = payload.get("usage")
    if isinstance(usage_payload, dict):
        return usage_payload

    result_payload = payload.get("result")
    if isinstance(result_payload, dict):
        nested = result_payload.get("usage")
        if isinstance(nested, dict):
            return nested

    return {}


def _record_usage_from_openclaw_result(tenant: Tenant, result: object) -> None:
    if not isinstance(result, dict):
        return

    usage = _extract_usage_payload(result)
    if not usage:
        logger.warning(
            "USAGE_MISSING tenant=%s result_keys=%s — OpenClaw response has no usage payload",
            tenant.id,
            list(result.keys()),
        )
        return

    input_tokens = _coerce_non_negative_int(usage.get("input_tokens", usage.get("input")))
    output_tokens = _coerce_non_negative_int(usage.get("output_tokens", usage.get("output")))
    model_used = ""
    if isinstance(usage.get("model_used"), str):
        model_used = usage.get("model_used") or ""
    elif isinstance(usage.get("model"), str):
        model_used = usage.get("model") or ""

    if not model_used and isinstance(result.get("model_used"), str):
        model_used = result.get("model_used") or ""

    if not (input_tokens or output_tokens):
        logger.warning(
            "USAGE_ZERO tenant=%s model=%s usage_keys=%s — usage payload present but token counts are zero",
            tenant.id,
            model_used,
            list(usage.keys()),
        )

    try:
        record_usage(
            tenant=tenant,
            event_type="message",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model_used=model_used,
        )
    except Exception:
        logger.exception("Failed to record usage for tenant=%s after OpenClaw callback", tenant.id)


def _build_budget_exhausted_message(chat_id: int, tenant: Tenant, reason: str) -> dict:
    frontend_url = getattr(settings, "FRONTEND_URL", "https://neighborhoodunited.org").rstrip("/")
    lang = tenant.user.language or "en"

    if reason == "global":
        msg_key = "budget_unavailable"
        kwargs: dict[str, str] = {}
    else:
        msg_key = "budget_exhausted_trial" if tenant.is_trial else "budget_exhausted_paid"
        kwargs = {"plus_message": "", "billing_url": f"{frontend_url}/billing"}

    return {
        "method": "sendMessage",
        "chat_id": chat_id,
        "text": error_msg(lang, msg_key, **kwargs),
    }


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

    # Telegram webhooks are unauthenticated — set service-role so
    # resolve_tenant_by_chat_id and record_usage can read/write RLS tables.
    from apps.tenants.middleware import set_rls_context

    set_rls_context(service_role=True)

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

    tenant = resolve_tenant_by_chat_id(chat_id)

    # Handle inline button callbacks (lessons, extraction)
    if "callback_query" in update and tenant is not None:
        callback_data = update["callback_query"].get("data", "")
        if callback_data.startswith("lesson:"):
            return handle_lesson_callback(update, tenant)
        if callback_data.startswith("extract:"):
            from apps.router.extraction_callbacks import handle_extraction_callback

            return handle_extraction_callback(update, tenant)

    # Unknown/inactive users are guided through onboarding.
    if not tenant:
        # Unknown user — send onboarding link
        response_data = send_onboarding_link(chat_id)
        logger.info("Unknown chat_id %s, sending onboarding link", chat_id)
        return JsonResponse(response_data)

    # Provisioning tenant — assistant is still waking up
    if tenant.status in (Tenant.Status.PENDING, Tenant.Status.PROVISIONING):
        lang = tenant.user.language or "en"
        return JsonResponse(
            {
                "method": "sendMessage",
                "chat_id": chat_id,
                "text": error_msg(lang, "waking_up"),
            }
        )

    frontend_url = getattr(settings, "FRONTEND_URL", "https://neighborhoodunited.org").rstrip("/")
    if tenant.status == Tenant.Status.SUSPENDED and not tenant.is_trial and not bool(tenant.stripe_subscription_id):
        lang = tenant.user.language or "en"
        return JsonResponse(
            {
                "method": "sendMessage",
                "chat_id": chat_id,
                "text": error_msg(
                    lang,
                    "suspended",
                    billing_url=f"{frontend_url}/settings/billing",
                ),
            }
        )

    # Hibernated tenant — buffer message and wake container
    from apps.router.wake_on_message import handle_hibernated_message

    msg_text = (update.get("message") or {}).get("text", "")
    wake_result = handle_hibernated_message(
        tenant,
        "telegram",
        update,
        msg_text,
    )
    if wake_result is True:
        lang = tenant.user.language or "en"
        return JsonResponse(
            {
                "method": "sendMessage",
                "chat_id": chat_id,
                "text": error_msg(lang, "hibernation_waking"),
            }
        )
    elif wake_result is False:
        return HttpResponse("ok")

    budget_reason = check_budget(tenant)
    if budget_reason:
        return JsonResponse(_build_budget_exhausted_message(chat_id, tenant, budget_reason))

    tenant.last_message_at = timezone.now()
    tenant.save(update_fields=["last_message_at"])

    # Forward to the correct OpenClaw instance
    loop = asyncio.new_event_loop()
    try:
        user_timezone = tenant.user.timezone or "UTC"

        # Inject current time into message text so the agent knows "now"
        from apps.router.services import build_datetime_context

        msg = update.get("message") or update.get("edited_message") or {}
        if "text" in msg:
            msg["text"] = build_datetime_context(user_timezone) + msg["text"]

        result = loop.run_until_complete(
            forward_to_openclaw(
                tenant.container_fqdn,
                update,
                user_timezone=user_timezone,
                timeout=30.0,
                max_retries=1,
                retry_delay=5.0,
            )
        )
    finally:
        loop.close()

    if result:
        _record_usage_from_openclaw_result(tenant, result)
        return JsonResponse(result)
    # Forwarding timed out — the agent likely received the message and will
    # reply asynchronously via the bot token.  Silently ack to Telegram
    # instead of sending a confusing "try again" message.
    return HttpResponse("ok")


# ── Chart image serving (for LINE image messages) ─────────────────────────


@csrf_exempt
def serve_chart_image(request, tenant_id, filename):
    """Serve a chart PNG from a tenant's Azure File Share.

    No authentication — security is provided by unguessable filenames
    (UUID-based).  LINE's servers fetch this URL to deliver image messages.
    """
    import re

    if request.method != "GET":
        return HttpResponseBadRequest("GET only")

    # Validate filename to prevent path traversal
    if not re.match(r"^[\w-]+\.png$", filename):
        return HttpResponseNotFound("Not found")

    try:
        from apps.orchestrator.azure_client import _is_mock

        if _is_mock():
            return HttpResponseNotFound("Not found (mock)")

        account_name = str(getattr(settings, "AZURE_STORAGE_ACCOUNT_NAME", "") or "").strip()
        if not account_name:
            return HttpResponseNotFound("Not found")

        from azure.storage.fileshare import ShareFileClient

        from apps.orchestrator.azure_client import get_storage_client

        storage_client = get_storage_client()
        keys = storage_client.storage_accounts.list_keys(
            settings.AZURE_RESOURCE_GROUP,
            account_name,
        )
        account_key = keys.keys[0].value
        share_name = f"ws-{str(tenant_id)[:20]}"

        file_client = ShareFileClient(
            account_url=f"https://{account_name}.file.core.windows.net",
            share_name=share_name,
            file_path=f"workspace/charts/{filename}",
            credential=account_key,
        )
        data = file_client.download_file().readall()

        response = HttpResponse(data, content_type="image/png")
        response["Cache-Control"] = "public, max-age=3600"
        return response

    except Exception:
        logger.exception("Failed to serve chart %s for tenant %s", filename, tenant_id)
        return HttpResponseNotFound("Not found")
