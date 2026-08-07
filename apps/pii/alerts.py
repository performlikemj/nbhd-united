"""Metadata-only rate alerts for Layer-1 placeholder authoring."""

from __future__ import annotations

import hashlib
import logging
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

RATE_MIN_ATTEMPTS = 20
RATE_THRESHOLD_PERCENT = 1
RATE_WINDOW = timedelta(hours=24)
RATE_COOLDOWN = timedelta(hours=24)


def rate_exceeded(*, attempts: int, count: int) -> bool:
    """Return whether ``count`` is above 1% of a sufficiently large sample."""
    return attempts >= RATE_MIN_ATTEMPTS and count * 100 > attempts * RATE_THRESHOLD_PERCENT


def send_rate_alert(
    tenant,
    *,
    attempts: int,
    count: int,
    kind: str,
    fingerprint_scope: str | None,
    window: str,
    counters: tuple[tuple[str, object], ...],
) -> bool:
    """Send one steward-gated rate alert without field values or text content."""
    if not rate_exceeded(attempts=attempts, count=count):
        return False

    from apps.transcripts.alerts import _send_alert

    counter_lines = "".join(f"{label}: {value}\n" for label, value in counters)
    scope_suffix = f":{fingerprint_scope}" if fingerprint_scope else ""
    return _send_alert(
        fingerprint=f"pii-authoring-{kind}-rate:{tenant.id}{scope_suffix}",
        cooldown=RATE_COOLDOWN,
        subject=f"[PII] Layer-1 {kind} rate above 1%",
        body=f"Tenant ID: {tenant.id}\nWindow: {window}\n{counter_lines}",
    )


def record_live_write_outcome(tenant, *, seam: str, writer: str, is_error: bool) -> bool:
    """Record a checked live write and alert on a >1% fixed-24h error rate.

    Counters are scoped by tenant, seam, and writer class. Cache failures are
    deliberately non-blocking: alert telemetry must never turn a primary-store
    authoring operation into a failed write.
    """
    now = timezone.now()
    window_seconds = int(RATE_WINDOW.total_seconds())
    bucket = int(now.timestamp()) // window_seconds
    seam_key = hashlib.sha256(seam.encode("utf-8")).hexdigest()[:20]
    base = f"pii:live-write:{tenant.id}:{seam_key}:{writer}:{bucket}"
    timeout = window_seconds * 2

    try:
        attempts_key = f"{base}:attempts"
        cache.add(attempts_key, 0, timeout=timeout)
        attempts = cache.incr(attempts_key)

        errors_key = f"{base}:errors"
        cache.add(errors_key, 0, timeout=timeout)
        errors = cache.incr(errors_key) if is_error else int(cache.get(errors_key, 0) or 0)
    except Exception:
        logger.exception(
            "pii_live_write_alert_counter_error tenant=%s seam=%s writer=%s",
            getattr(tenant, "id", "?"),
            seam,
            writer,
        )
        return False

    return send_rate_alert(
        tenant,
        attempts=attempts,
        count=errors,
        kind="error",
        fingerprint_scope=f"live:{seam_key}:{writer}:{bucket}",
        window="current fixed 24h live-write window",
        counters=(
            ("Seam", seam),
            ("Writer class", writer),
            ("Writer attempts", attempts),
            ("Writer errors", errors),
        ),
    )
