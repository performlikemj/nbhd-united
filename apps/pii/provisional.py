"""Locked lifecycle transitions for provisional PII bindings."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.pii.entity_registry import canonical_key, get_name, is_denied, is_retired, to_storage_value

MAX_SEEN_ITEMS = 8


@dataclass(frozen=True)
class PiiIngress:
    channel: str
    provider_event_id: str | None
    occurred_at: datetime


def provisional_creation_enabled(tenant) -> bool:
    from django.conf import settings

    return str(tenant.pk) in settings.PII_PROVISIONAL_TENANT_IDS


def should_mint_provisional(tenant, entity_type: str, original: str, ingress: PiiIngress | None) -> bool:
    return bool(
        ingress is not None
        and provisional_creation_enabled(tenant)
        and entity_type in {"PERSON", "LOCATION"}
        and len(original.split()) == 1
    )


def _local_date(tenant, occurred_at: datetime) -> str:
    timezone_name = getattr(getattr(tenant, "user", None), "timezone", None) or "UTC"
    try:
        local_tz = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        local_tz = ZoneInfo("UTC")
    return occurred_at.astimezone(local_tz).date().isoformat()


def _event_digest(tenant, ingress: PiiIngress) -> str | None:
    if ingress.provider_event_id is None:
        return None
    material = f"{tenant.pk}:{ingress.channel}:{ingress.provider_event_id}".encode()
    return sha256(material).hexdigest()[:32]


def record_provisional_sightings(tenant, raw_owner_text: str, ingress: PiiIngress) -> list[TransitionResult]:
    """Record one raw provider event against every matching provisional value."""
    digest = _event_digest(tenant, ingress)
    if not digest or not raw_owner_text:
        return []

    from apps.pii.redactor import known_value_matches

    grouped: dict[str, list[tuple[str, Any]]] = {}
    for placeholder, raw in (getattr(tenant, "pii_entity_map", None) or {}).items():
        name = get_name(raw)
        key = canonical_key(name)
        if not key or not known_value_matches(raw_owner_text, name):
            continue
        if isinstance(raw, dict) and (raw.get("provisional") or raw.get("retired_reason") == "provisional-expired"):
            grouped.setdefault(key, []).append((placeholder, raw))

    targets: list[str] = []
    for entries in grouped.values():
        active = [(placeholder, raw) for placeholder, raw in entries if not is_retired(raw)]
        if active:
            targets.extend(placeholder for placeholder, raw in active if raw.get("provisional"))
            continue
        expired = [item for item in entries if item[1].get("retired_reason") == "provisional-expired"]
        if expired:
            # Newest last sighting wins; placeholder makes equal timestamps deterministic.
            targets.append(max(expired, key=lambda item: (item[1].get("last_seen_at") or "", item[0]))[0])

    results = []
    local_date = _local_date(tenant, ingress.occurred_at)
    for placeholder in targets:
        results.append(
            transition_binding(
                tenant,
                placeholder,
                "count",
                now=ingress.occurred_at,
                event_digest=digest,
                local_date=local_date,
            )
        )
    return results


@dataclass(frozen=True)
class TransitionResult:
    changed: bool
    entry: dict[str, Any] | None
    entity_map: dict[str, Any]
    outcome: str


def _is_globally_blocked(placeholder: str, name: str) -> bool:
    """Apply the existing global stoplist/junk rules to lifecycle revival."""
    from apps.pii.hygiene import is_junk_span
    from apps.pii.redactor import is_never_a_name

    entity_type = placeholder.removeprefix("[").split("_", 1)[0]
    junk, _reason = is_junk_span(name, entity_type)
    return junk or is_never_a_name(name)


def transition_binding(
    tenant,
    placeholder: str,
    action: str,
    *,
    now: datetime | None = None,
    event_digest: str | None = None,
    local_date: str | None = None,
    promoted_by: str | None = None,
    expires_before: datetime | None = None,
) -> TransitionResult:
    """Re-read and transition one binding under the tenant row lock.

    Precedence is denylist > owner retirement > global stoplist/junk >
    promoted/keep > reactivate. Every successful transition installs the locked
    snapshot on the caller's tenant instance so subsequent redaction is current.
    """
    if action not in {"count", "promote", "expire", "reactivate", "keep", "stop-hiding"}:
        raise ValueError(f"unknown provisional transition: {action}")
    now = now or timezone.now()
    now_iso = now.isoformat()

    with transaction.atomic():
        locked = type(tenant).objects.select_for_update().filter(pk=tenant.pk).first()
        entity_map = dict((locked.pii_entity_map if locked else None) or {})
        denylist = dict((locked.pii_denylist if locked else None) or {})
        raw = entity_map.get(placeholder)
        if raw is None:
            result = TransitionResult(False, None, entity_map, "missing")
        else:
            name = get_name(raw)
            entry = to_storage_value(name, existing=raw)
            owner_retired = entry.get("retired_reason") == "owner"
            blocked = is_denied(denylist, name) or owner_retired or _is_globally_blocked(placeholder, name)
            promoted = bool(entry.get("promoted_at") or entry.get("promoted_by"))
            changed = False
            outcome = "noop"

            if action == "stop-hiding":
                entry = to_storage_value(
                    name,
                    existing=entry,
                    retired=True,
                    retired_at=now_iso,
                    retired_reason="owner",
                )
                changed, outcome = True, "owner-retired"
            elif action == "keep" and not entry.get("provisional") and not blocked:
                entry = to_storage_value(name, existing=entry, reviewed_at=now_iso)
                changed, outcome = entry != raw, "kept"
            elif action in {"keep", "promote"} and not blocked:
                entry = to_storage_value(
                    name,
                    existing=entry,
                    provisional=False,
                    promoted_at=entry.get("promoted_at") or now_iso,
                    promoted_by=entry.get("promoted_by")
                    or promoted_by
                    or ("owner" if action == "keep" else "recurrence"),
                    reviewed_at=now_iso if action == "keep" else entry.get("reviewed_at"),
                    retired=None,
                    retired_at=None,
                    retired_reason=None,
                )
                changed, outcome = entry != raw, "promoted"
            elif action == "expire" and not owner_retired and entry.get("provisional") and not promoted:
                last_seen = parse_datetime(str(entry.get("last_seen_at") or ""))
                if expires_before is not None and (last_seen is None or last_seen >= expires_before):
                    outcome = "not-expired"
                else:
                    entry = to_storage_value(
                        name,
                        existing=entry,
                        retired=True,
                        retired_at=now_iso,
                        retired_reason="provisional-expired",
                    )
                    changed, outcome = entry != raw, "expired"
            elif action in {"reactivate", "count"} and not blocked:
                if entry.get("retired_reason") == "provisional-expired":
                    entry = to_storage_value(
                        name,
                        existing=entry,
                        provisional=True,
                        first_seen_at=now_iso,
                        last_seen_at=now_iso,
                        seen_events=[],
                        seen_dates=[],
                        retired=None,
                        retired_at=None,
                        retired_reason=None,
                    )
                    changed, outcome = True, "reactivated"
                if action == "count" and entry.get("provisional") and event_digest and local_date:
                    events = list(entry.get("seen_events") or [])
                    dates = list(entry.get("seen_dates") or [])
                    if event_digest not in events:
                        events = (events + [event_digest])[-MAX_SEEN_ITEMS:]
                        if local_date not in dates:
                            dates = (dates + [local_date])[-MAX_SEEN_ITEMS:]
                        entry = to_storage_value(
                            name,
                            existing=entry,
                            last_seen_at=now_iso,
                            seen_events=events,
                            seen_dates=dates,
                        )
                        changed, outcome = True, "counted"
                        if len(events) >= 3 and len(dates) >= 2:
                            entry = to_storage_value(
                                name,
                                existing=entry,
                                provisional=False,
                                promoted_at=now_iso,
                                promoted_by="recurrence",
                                retired=None,
                                retired_at=None,
                                retired_reason=None,
                            )
                            outcome = "promoted"
            elif blocked:
                outcome = "blocked"
            elif promoted:
                outcome = "promoted"

            if changed:
                entity_map[placeholder] = entry
                type(tenant).objects.filter(pk=tenant.pk).update(pii_entity_map=entity_map)
            result = TransitionResult(changed, entry, entity_map, outcome)

    tenant.pii_entity_map = result.entity_map
    return result
