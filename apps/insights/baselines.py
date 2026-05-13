"""Rolling-window baselines over PillarSnapshot history.

Pure statistics — no judgment. The assistant calls ``compute_baseline()`` as an
input to its reasoning ("is this anomalous?", "is this a trend?"); the
intelligence lives in the LLM, not here.

Phase 2 supports Gravity's payload-level totals (``debt``, ``savings``,
``minimum_payments``). Category-level topics (``dining``, ``subscriptions``,
etc.) require category data on ``FinanceTransaction``, which is Phase 1.5
work — those topics return ``sample_size=0`` here and the assistant should
fall back to the raw history or skip.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from django.utils import timezone

from .models import PillarSnapshot
from .pillars import Pillar


def _to_float(value: Any) -> float | None:
    """Coerce strings/Decimals/numbers to float. Return None on failure."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(Decimal(str(value)))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _gravity_total(key: str) -> Callable[[dict], float | None]:
    return lambda payload: _to_float((payload or {}).get("totals", {}).get(key))


# Per-pillar map: topic slug → function(payload) → numeric or None.
# A topic that's missing from this map gets ``sample_size=0`` — the assistant
# can detect that and fall back to the raw history.
TOPIC_EXTRACTORS: dict[str, dict[str, Callable[[dict], float | None]]] = {
    Pillar.GRAVITY.value: {
        "debt": _gravity_total("debt"),
        "savings": _gravity_total("savings"),
        "minimum_payments": _gravity_total("minimum_payments"),
    },
}


def _slope(values: list[float]) -> float:
    """Least-squares slope (per-index unit). 0 when N<2 or all xs identical."""
    n = len(values)
    if n < 2:
        return 0.0
    mean_x = (n - 1) / 2
    mean_y = sum(values) / n
    num = sum((i - mean_x) * (y - mean_y) for i, y in enumerate(values))
    den = sum((i - mean_x) ** 2 for i in range(n))
    return num / den if den else 0.0


def _stdev(values: list[float], mean: float) -> float:
    """Sample stdev (N-1 denom). 0 when N<2."""
    n = len(values)
    if n < 2:
        return 0.0
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return math.sqrt(var)


def compute_baseline(
    *,
    tenant,
    pillar: str,
    topic_slug: str,
    window_weeks: int = 12,
    granularity: str = PillarSnapshot.Granularity.WEEKLY,
) -> dict:
    """Return rolling baseline stats for a (tenant, pillar, topic) over a window.

    Output shape::

        {
            "pillar": "gravity",
            "topic": "dining",
            "granularity": "weekly",
            "window_weeks": 12,
            "sample_size": 8,
            "mean": 312.40,           # float, in the topic's native unit
            "stdev": 41.20,
            "latest": 398.00,
            "latest_z": 2.08,         # (latest - mean) / stdev; 0 when stdev=0
            "trend": 6.4,             # least-squares slope per snapshot
            "freshness_days": 2,      # age of the latest snapshot
            "supported": True,        # False = topic has no extractor in this pillar
        }
    """
    extractors = TOPIC_EXTRACTORS.get(pillar, {})
    extractor = extractors.get(topic_slug)

    base = {
        "pillar": pillar,
        "topic": topic_slug,
        "granularity": granularity,
        "window_weeks": window_weeks,
        "sample_size": 0,
        "mean": None,
        "stdev": None,
        "latest": None,
        "latest_z": None,
        "trend": None,
        "freshness_days": None,
        "supported": extractor is not None,
    }
    if extractor is None:
        return base

    since = timezone.now() - timezone.timedelta(days=window_weeks * 7)
    snapshots = list(
        PillarSnapshot.objects.filter(
            tenant=tenant,
            pillar=pillar,
            granularity=granularity,
            ts__gte=since,
        ).order_by("ts")
    )

    values_with_ts: list[tuple[datetime, float]] = []
    for snap in snapshots:
        value = extractor(snap.payload or {})
        if value is not None:
            values_with_ts.append((snap.ts, value))

    if not values_with_ts:
        return base

    values = [v for _, v in values_with_ts]
    latest_ts, latest_value = values_with_ts[-1]
    mean = sum(values) / len(values)
    stdev = _stdev(values, mean)
    freshness = max(0, (timezone.now() - latest_ts).days)

    base.update(
        {
            "sample_size": len(values),
            "mean": round(mean, 4),
            "stdev": round(stdev, 4),
            "latest": round(latest_value, 4),
            "latest_z": round((latest_value - mean) / stdev, 4) if stdev > 0 else 0.0,
            "trend": round(_slope(values), 4),
            "freshness_days": freshness,
        }
    )
    return base
