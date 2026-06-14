"""Limited-time free-model offer: resolution + health state helpers.

The promo: while OpenRouter keeps ``NEMOTRON_FREE_MODEL`` free *and* reachable,
it becomes the default chat model for every tenant on the rolling default
(``preferred_model == ""``). The moment it stops being free or goes dark, the
``model_health_check`` cron flips the offer off, reverts those tenants to the
configured fallback (DeepSeek V4 Pro), and notifies them.

This module is the single source of truth both surfaces read:
  - per-tenant chat config (apps/orchestrator/config_generator.py), and
  - the platform's own OpenRouter calls / API surface.

It does pure DB reads/writes only — no HTTP, no notifications, no config
bumps. The cron task (apps/billing/tasks.py:model_health_check_task) owns the
probing + transition side effects.
"""

from __future__ import annotations

from typing import Any

from django.utils import timezone

from apps.billing.constants import (
    NEMOTRON_FREE_DISPLAY,
    display_name_for_model,
)

# Number of consecutive failed communication checks before we yank a *reachable*
# offer. A pricing change (free → paid) deactivates immediately; transient
# unreachability has to persist across this many cron ticks first, so a single
# blip doesn't flap the whole fleet (per-turn outages are already absorbed by
# OpenClaw's runWithModelFallback).
OFFER_FAILURE_THRESHOLD = 3

# Number of consecutive failed reachability checks before we auto-disable an offer
# that is *enabled but has never activated*. Distinct from (and far larger than)
# OFFER_FAILURE_THRESHOLD: that one yanks a live offer on a short outage, whereas
# this one only fires when the offer never went live at all — i.e. the configured
# slug is simply gone (OpenRouter retired/renamed it → a permanent 404). At the
# 30-minute cron cadence, 48 checks ≈ 24h of continuous failure, which a real
# transient blip won't reach. When tripped we flip the `enabled` kill-switch off so
# we stop pinging a dead slug forever; relaunching means setting a valid model_id
# and re-enabling.
INACTIVE_OFFER_DISABLE_THRESHOLD = 48


def _offer():
    # Late import — this module is imported from config_generator during
    # Django setup, before the app registry is fully populated.
    from apps.billing.models import FreeModelOffer

    return FreeModelOffer.load()


def offer_is_active() -> bool:
    """True when the free promo is currently the advertised default."""
    o = _offer()
    return bool(o.enabled and o.is_active and o.model_id)


def offer_model_id() -> str:
    return _offer().model_id


def offer_fallback_model_id() -> str:
    return _offer().fallback_model_id


def resolve_default_primary_model(base_primary: str) -> str:
    """Resolve the effective default primary model for a tenant on the rolling
    default. Returns the free-offer model while the promo is live, otherwise
    ``base_primary`` (the tier's configured primary, e.g. DeepSeek V4 Pro)."""
    o = _offer()
    if o.enabled and o.is_active and o.model_id:
        return o.model_id
    return base_primary


def offer_model_entry() -> dict[str, dict[str, Any]]:
    """Allowlist fragment (model_id → {"alias": ...}) to merge into a tier's
    model entries while the promo is active, so the offer model is a valid
    primary and a selectable picker option. Empty when the promo is off."""
    if not offer_is_active():
        return {}
    return {offer_model_id(): {"alias": "nemotron"}}


# ── Health-state writers (used by the cron + the fallback wrapper) ──────────


def record_model_success(model_id: str, *, pricing: dict | None = None) -> None:
    """Mark a model as reachable and reset its failure counter."""
    from apps.billing.models import ModelHealth

    now = timezone.now()
    defaults = {
        "is_reachable": True,
        "consecutive_failures": 0,
        "last_checked_at": now,
        "last_ok_at": now,
        "last_error": "",
    }
    if pricing is not None:
        defaults["pricing"] = pricing
        defaults["is_free"] = _pricing_is_free(pricing)
    ModelHealth.objects.update_or_create(model_id=model_id, defaults=defaults)


def record_model_failure(model_id: str, error: str) -> int:
    """Mark a model as having failed a call. Returns the new failure count."""
    from apps.billing.models import ModelHealth

    now = timezone.now()
    row, _ = ModelHealth.objects.get_or_create(model_id=model_id)
    row.consecutive_failures = (row.consecutive_failures or 0) + 1
    row.is_reachable = False
    row.last_checked_at = now
    row.last_error = (error or "")[:500]
    row.save(update_fields=["consecutive_failures", "is_reachable", "last_checked_at", "last_error", "updated_at"])
    return row.consecutive_failures


def record_model_pricing(model_id: str, pricing: dict | None) -> bool:
    """Persist the latest pricing block and free flag from a /models read.
    Returns the resolved is_free value."""
    from apps.billing.models import ModelHealth

    is_free = _pricing_is_free(pricing or {})
    ModelHealth.objects.update_or_create(
        model_id=model_id,
        defaults={
            "pricing": pricing or {},
            "is_free": is_free,
            "last_checked_at": timezone.now(),
        },
    )
    return is_free


def _pricing_is_free(pricing: dict) -> bool:
    """OpenRouter reports per-token prices as strings ("0", "0.0000004").
    A model is free when both prompt and completion are zero."""

    def _zero(v: Any) -> bool:
        try:
            return float(v) == 0.0
        except (TypeError, ValueError):
            return False

    if not pricing:
        return False
    return _zero(pricing.get("prompt")) and _zero(pricing.get("completion"))


# ── API/surface read ───────────────────────────────────────────────────────


def offer_state() -> dict[str, Any]:
    """Serializable snapshot of the offer + its health, for the settings API."""
    from apps.billing.models import ModelHealth

    o = _offer()
    health = ModelHealth.objects.filter(model_id=o.model_id).first()
    return {
        "active": bool(o.enabled and o.is_active and o.model_id),
        "model_id": o.model_id,
        "display_name": NEMOTRON_FREE_DISPLAY if o.model_id else "",
        "fallback_model_id": o.fallback_model_id,
        "fallback_display_name": display_name_for_model(o.fallback_model_id),
        "activated_at": o.activated_at.isoformat() if o.activated_at else None,
        "last_transition_reason": o.last_transition_reason,
        "health": {
            "is_reachable": bool(health.is_reachable) if health else None,
            "is_free": bool(health.is_free) if health else None,
            "last_checked_at": health.last_checked_at.isoformat() if (health and health.last_checked_at) else None,
        },
    }
