"""The sole write path for structured, decaying tenant situation signals."""

from __future__ import annotations

import logging
import unicodedata
from datetime import timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import transaction
from django.utils import timezone

from apps.tenants.models import Tenant, UserSituation

logger = logging.getLogger(__name__)

OBSERVATION_WRITE_THROTTLE = timedelta(minutes=10)
_MARKDOWN_SIGNIFICANT = frozenset("#*`[]()>|_~")


def _capture_enabled(tenant: Tenant) -> bool:
    return bool(getattr(tenant, "situational_context_enabled", False)) and not getattr(tenant, "is_eval_sink", False)


def clean_place_label(label: object) -> str:
    if not isinstance(label, str):
        return ""
    clean = label.strip()
    if not clean or len(clean) > 64:
        return ""
    if len(clean.splitlines()) != 1:
        return ""
    if any(ch in _MARKDOWN_SIGNIFICANT or unicodedata.category(ch) == "Cc" for ch in clean):
        return ""
    return clean


def _is_throttled(last_observed_at, observed_at) -> bool:
    return bool(last_observed_at and observed_at - last_observed_at < OBSERVATION_WRITE_THROTTLE)


def record_place_observation(
    tenant: Tenant,
    label: object,
    source: str,
    observed_at=None,
) -> bool:
    """Record one labeled place observation; return whether the label changed."""
    if not _capture_enabled(tenant):
        return False

    clean_label = clean_place_label(label)
    if not clean_label:
        return False

    observed_at = observed_at or timezone.now()
    clean_source = str(source or "").strip()[:16]

    with transaction.atomic():
        situation, _ = UserSituation.objects.select_for_update().get_or_create(tenant=tenant)
        changed = situation.current_place_label != clean_label
        if not changed and _is_throttled(situation.current_place_last_observed_at, observed_at):
            return False

        update_fields = ["current_place_last_observed_at", "updated_at"]
        situation.current_place_last_observed_at = observed_at
        if changed:
            situation.current_place_label = clean_label
            situation.current_place_source = clean_source
            situation.current_place_since = observed_at
            update_fields.extend(
                [
                    "current_place_label",
                    "current_place_source",
                    "current_place_since",
                ]
            )
        situation.save(update_fields=update_fields)

    home = str(getattr(getattr(tenant, "user", None), "location_city", "") or "").strip()
    differs_home = clean_label.casefold() != home.casefold()
    logger.info(
        "situation_updated tenant=%s source=%s labeled=1 changed=%d differs_home=%d",
        tenant.id,
        clean_source,
        int(changed),
        int(differs_home),
    )
    return changed


def record_device_tz(
    tenant: Tenant,
    tz_name: object,
    source_device: str,
    observed_at=None,
) -> bool:
    """Record a validated IANA device timezone; return whether its value changed."""
    if not _capture_enabled(tenant):
        return False

    if not isinstance(tz_name, str):
        return False
    clean_tz = tz_name.strip()
    if not clean_tz or len(clean_tz) > 64:
        logger.debug("situation_device_tz_invalid tenant=%s", tenant.id)
        return False
    try:
        ZoneInfo(clean_tz)
    except (ZoneInfoNotFoundError, ValueError):
        logger.debug("situation_device_tz_invalid tenant=%s", tenant.id)
        return False

    observed_at = observed_at or timezone.now()
    clean_source_device = str(source_device or "").strip()[:64]

    with transaction.atomic():
        situation, _ = UserSituation.objects.select_for_update().get_or_create(tenant=tenant)
        if situation.device_tz_last_observed_at is not None and observed_at < situation.device_tz_last_observed_at:
            return False

        changed = situation.device_tz != clean_tz
        if not changed and _is_throttled(situation.device_tz_last_observed_at, observed_at):
            return False

        update_fields = ["device_tz_last_observed_at", "updated_at"]
        situation.device_tz_last_observed_at = observed_at
        if changed:
            situation.device_tz = clean_tz
            situation.device_tz_since = observed_at
            situation.device_tz_source_device = clean_source_device
            update_fields.extend(
                [
                    "device_tz",
                    "device_tz_since",
                    "device_tz_source_device",
                ]
            )
        situation.save(update_fields=update_fields)

    return changed
