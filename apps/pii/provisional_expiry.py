"""Bounded oldest-first expiry for provisional PII bindings."""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.pii.provisional import transition_binding

logger = logging.getLogger(__name__)
DEFAULT_MAX_ENTRIES = 500


def _candidates(entity_map: dict, cutoff, max_entries: int) -> list[str]:
    eligible = []
    for placeholder, entry in entity_map.items():
        if not isinstance(entry, dict):
            continue
        if not entry.get("provisional") or entry.get("retired"):
            continue
        if entry.get("promoted_at") or entry.get("promoted_by"):
            continue
        last_seen = parse_datetime(str(entry.get("last_seen_at") or ""))
        if last_seen is not None and last_seen < cutoff:
            eligible.append((last_seen, placeholder))
    eligible.sort(key=lambda item: (item[0], item[1]))
    return [placeholder for _seen, placeholder in eligible[:max_entries]]


def sweep_tenant(tenant, *, dry_run: bool = False, max_entries: int = DEFAULT_MAX_ENTRIES, now=None) -> dict[str, int]:
    now = now or timezone.now()
    cutoff = now - timedelta(hours=settings.PII_PROVISIONAL_TTL_HOURS)
    placeholders = _candidates(getattr(tenant, "pii_entity_map", None) or {}, cutoff, max_entries)
    expired = 0
    if not dry_run:
        for placeholder in placeholders:
            result = transition_binding(
                tenant,
                placeholder,
                "expire",
                now=now,
                expires_before=cutoff,
            )
            expired += int(result.outcome == "expired")
            logger.info("pii_policy_expire tenant=%s outcome=%s", tenant.pk, result.outcome)
    return {"examined": len(placeholders), "eligible": len(placeholders), "expired": expired}


def sweep_all_tenants(*, dry_run: bool = False, max_entries: int = DEFAULT_MAX_ENTRIES) -> dict[str, int]:
    from apps.tenants.models import Tenant

    totals = {"tenants_seen": 0, "examined": 0, "eligible": 0, "expired": 0, "errors": 0}
    for tenant in (
        Tenant.objects.filter(status=Tenant.Status.ACTIVE)
        .only("id", "pii_entity_map", "pii_denylist", "user__timezone")
        .select_related("user")
        .order_by("id")
    ):
        totals["tenants_seen"] += 1
        try:
            result = sweep_tenant(tenant, dry_run=dry_run, max_entries=max_entries)
        except Exception:
            totals["errors"] += 1
            logger.exception("pii provisional expiry failed tenant=%s", tenant.pk)
            continue
        for key in ("examined", "eligible", "expired"):
            totals[key] += result[key]
    return totals


def expire_provisional_bindings_task() -> dict[str, int]:
    if not settings.PII_PROVISIONAL_SWEEP_ENABLED:
        return {"disabled": 1}
    return sweep_all_tenants()
