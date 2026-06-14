"""Free-model offer health probing + transition orchestration.

Runs on a 30-minute cron (``model_health_check_task``). Each tick it:

  1. reads OpenRouter ``/models`` and records pricing/free-status for the
     monitored models (the "communication check" for *free-ness*);
  2. sends a 1-token completion to the offer model (the communication check
     for *reachability*);
  3. flips the ``FreeModelOffer`` singleton ONLY on a genuine transition, and
     on each flip bumps affected tenant configs + notifies users.

Guardrails against flapping the whole fleet on a blip:
  - a pricing read failure leaves free-status *unknown* and never deactivates;
  - reachability has to fail ``OFFER_FAILURE_THRESHOLD`` consecutive ticks
    before an *active* offer is yanked (per-turn outages are already absorbed by
    OpenClaw's runWithModelFallback);
  - activation requires a definite free + reachable signal this tick.
"""

from __future__ import annotations

import logging
from typing import Any

import requests
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from apps.billing.constants import (
    DEEPSEEK_FLASH_MODEL,
    DEEPSEEK_MODEL,
    GEMMA_MODEL,
    NEMOTRON_FREE_DISPLAY,
    NEMOTRON_FREE_MODEL,
    display_name_for_model,
)
from apps.billing.model_offers import (
    INACTIVE_OFFER_DISABLE_THRESHOLD,
    OFFER_FAILURE_THRESHOLD,
    record_model_failure,
    record_model_pricing,
)
from apps.common.openrouter import chat_completion, normalize_model_id

logger = logging.getLogger(__name__)

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
PING_TIMEOUT_SECONDS = 20

# Models whose pricing we refresh each tick. The offer model also gets an active
# reachability ping; the rest self-report reachability through real traffic via
# the control-plane fallback wrapper.
MONITORED_MODELS = [NEMOTRON_FREE_MODEL, DEEPSEEK_MODEL, DEEPSEEK_FLASH_MODEL, GEMMA_MODEL]

# How many tenants we'll proactively notify on a single transition before we
# stop (and log) — a backstop so a future large fleet can't blast thousands of
# channel sends from one cron tick. Config bumps still apply to everyone.
NOTIFY_CAP = 500


def model_health_check() -> dict[str, Any]:
    """Probe + reconcile the free-model offer. Returns a summary dict."""
    from apps.billing.models import FreeModelOffer

    offer = FreeModelOffer.load()
    result: dict[str, Any] = {
        "offer_model": offer.model_id,
        "was_active": offer.is_active,
        "enabled": offer.enabled,
        "transition": "none",
    }

    if not offer.enabled:
        if offer.is_active:
            _transition(offer, new_active=False, reason="disabled")
            result["transition"] = "deactivated"
        return result

    pricing_map = _fetch_pricing()
    for model_id in MONITORED_MODELS:
        pricing = pricing_map.get(normalize_model_id(model_id))
        if pricing is not None:
            record_model_pricing(model_id, pricing)

    offer_free = _offer_free_status(pricing_map, offer.model_id)  # True / False / None(unknown)
    reachable, fail_count = _ping(offer.model_id)
    result.update(offer_free=offer_free, reachable=reachable, failures=fail_count)

    if not offer.is_active:
        if offer_free is True and reachable:
            _transition(offer, new_active=True, reason="available")
            result["transition"] = "activated"
        elif _slug_retired(pricing_map, offer.model_id) and fail_count >= INACTIVE_OFFER_DISABLE_THRESHOLD:
            # Enabled but never activated, AND we have positive evidence the slug is
            # gone: a SUCCESSFUL /models read that doesn't list it (OpenRouter retired
            # /renamed it → permanent 404), confirmed by a long run of failed pings.
            # Requiring the successful-read signal means a transient OpenRouter outage
            # (which leaves pricing_map empty → _slug_retired False) can NOT kill a
            # valid pre-launch promo; only a real retire does. Flip the kill-switch off
            # so we stop pinging a dead slug every tick (the `not offer.enabled`
            # early-return above short-circuits future ticks). Relaunch = set a valid
            # model_id + re-enable.
            offer.enabled = False
            offer.last_transition_reason = "auto_disabled_retired_slug"
            offer.save(update_fields=["enabled", "last_transition_reason", "updated_at"])
            logger.warning(
                "model_health_check: auto-disabled free offer — model %s absent from a "
                "successful /models read and unreachable for %d consecutive checks; never "
                "activated. Set a valid model_id and re-enable to relaunch.",
                offer.model_id,
                fail_count,
            )
            result["transition"] = "auto_disabled"
        else:
            result["transition"] = "held (not yet available)"
    else:
        if offer_free is False:
            _transition(offer, new_active=False, reason="no_longer_free")
            result["transition"] = "deactivated"
        elif not reachable and fail_count >= OFFER_FAILURE_THRESHOLD:
            _transition(offer, new_active=False, reason="unreachable")
            result["transition"] = "deactivated"
        else:
            result["transition"] = "held (healthy or below failure threshold)"

    logger.info("model_health_check: %s", result)
    return result


# ── Probes ─────────────────────────────────────────────────────────────────


def _fetch_pricing() -> dict[str, dict]:
    """Return {bare_slug: pricing_block} from OpenRouter ``/models``. Empty on
    any failure (caller treats a missing entry as 'unknown', never 'not free')."""
    key = getattr(settings, "OPENROUTER_API_KEY", "")
    if not key:
        logger.warning("model_health_check: OPENROUTER_API_KEY not configured; skipping pricing read")
        return {}
    try:
        resp = requests.get(
            OPENROUTER_MODELS_URL, headers={"Authorization": f"Bearer {key}"}, timeout=PING_TIMEOUT_SECONDS
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        logger.warning("model_health_check: /models fetch failed", exc_info=True)
        return {}
    out: dict[str, dict] = {}
    for entry in data.get("data", []) or []:
        mid = entry.get("id")
        if mid:
            out[mid] = entry.get("pricing") or {}
    return out


def _offer_free_status(pricing_map: dict[str, dict], model_id: str) -> bool | None:
    from apps.billing.model_offers import _pricing_is_free

    slug = normalize_model_id(model_id)
    if slug not in pricing_map:
        return None
    return _pricing_is_free(pricing_map[slug])


def _slug_retired(pricing_map: dict[str, dict], model_id: str) -> bool:
    """True only when a SUCCESSFUL /models read does NOT list the slug — positive
    evidence OpenRouter retired/renamed it (permanent 404). A failed/empty read
    (pricing_map == {}) returns False: an outage is not evidence the slug is gone."""
    return bool(pricing_map) and normalize_model_id(model_id) not in pricing_map


def _ping(model_id: str) -> tuple[bool, int]:
    """1-token completion to confirm the model actually responds. Returns
    (reachable, consecutive_failures). chat_completion records health itself."""
    try:
        chat_completion(
            [model_id],
            [{"role": "user", "content": "ping"}],
            max_tokens=1,
            timeout=PING_TIMEOUT_SECONDS,
        )
        return True, 0
    except Exception as exc:  # noqa: BLE001 — probe failure is expected signal
        # chat_completion already incremented the failure counter; read it back.
        from apps.billing.models import ModelHealth

        row = ModelHealth.objects.filter(model_id=model_id).first()
        count = row.consecutive_failures if row else record_model_failure(model_id, repr(exc))
        logger.warning("model_health_check: offer model ping failed (%d consecutive): %s", count, exc)
        return False, count


# ── Transition ───────────────────────────────────────────────────────────────


def _transition(offer, *, new_active: bool, reason: str) -> None:
    now = timezone.now()
    offer.is_active = new_active
    offer.last_transition_reason = reason
    if new_active:
        offer.activated_at = now
    else:
        offer.deactivated_at = now
    offer.save(update_fields=["is_active", "last_transition_reason", "activated_at", "deactivated_at", "updated_at"])
    logger.info("FreeModelOffer transition → active=%s reason=%s", new_active, reason)
    _apply_to_tenants(offer, new_active=new_active, reason=reason)


def _apply_to_tenants(offer, *, new_active: bool, reason: str) -> None:
    """Re-render + notify every tenant whose effective model changes: those on
    the rolling default (preferred_model == "") and those who explicitly picked
    the offer model. On deactivation, an explicit pick of the now-dead offer
    model is reset to the rolling default so the user isn't stranded on it."""
    from apps.cron.publish import publish_task
    from apps.router.system_notify import send_system_notification
    from apps.tenants.models import Tenant

    model = offer.model_id
    affected = list(
        Tenant.objects.filter(status=Tenant.Status.ACTIVE, hibernated_at__isnull=True).filter(
            Q(preferred_model="") | Q(preferred_model=model)
        )
    )
    message = _transition_message(offer, new_active=new_active, reason=reason)

    bumped = 0
    notified = 0
    for tenant in affected:
        if not new_active and tenant.preferred_model == model:
            tenant.preferred_model = ""
            tenant.save(update_fields=["preferred_model"])
        tenant.bump_pending_config()
        try:
            publish_task(
                "apply_single_tenant_config",
                str(tenant.id),
                idempotency_key=f"apply-config-{tenant.id}",
            )
        except Exception:
            logger.warning("model_health_check: enqueue apply failed for %s", str(tenant.id)[:8], exc_info=True)
        bumped += 1
        if notified < NOTIFY_CAP:
            if send_system_notification(tenant, message):
                notified += 1

    if len(affected) > NOTIFY_CAP:
        logger.warning(
            "model_health_check: %d tenants affected but capped notifications at %d (configs still bumped)",
            len(affected),
            NOTIFY_CAP,
        )
    logger.info("model_health_check: transition applied — bumped=%d notified=%d", bumped, notified)


def _transition_message(offer, *, new_active: bool, reason: str) -> str:
    fallback = display_name_for_model(offer.fallback_model_id)
    if new_active:
        return (
            f"✨ Good news — for a limited time, your assistant now runs on "
            f"{NEMOTRON_FREE_DISPLAY}, free of charge. It's a frontier model with a "
            f"1M-token memory. If it ever becomes unavailable we'll switch you back to "
            f"{fallback} automatically — nothing for you to do. Prefer a different model? "
            f"Settings → AI Provider."
        )
    if reason == "no_longer_free":
        return (
            f"The free {NEMOTRON_FREE_DISPLAY} promotion has ended, so your assistant has "
            f"been switched back to {fallback}. You can change models anytime in "
            f"Settings → AI Provider."
        )
    # unreachable / disabled
    return (
        f"{NEMOTRON_FREE_DISPLAY} is temporarily unavailable, so your assistant has been "
        f"switched to {fallback} to stay responsive. We'll move you back automatically if "
        f"it returns. You can also choose a model in Settings → AI Provider."
    )
