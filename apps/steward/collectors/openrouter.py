from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.steward.collectors.status import (
    acquire_collector_lease,
    collector_failed,
    collector_succeeded,
    release_collector_lease,
    set_persistence_timeouts,
)
from apps.steward.models import (
    CollectorStatus,
    EvidenceEvent,
    EvidenceSource,
    OpenRouterModelDaily,
)
from apps.steward.services import EvidenceIngestInput, ingest_evidence_batch
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

OPENROUTER_TIMEOUT_SECONDS = 15.0
OPENROUTER_QUERY_LIMIT = 10_000
TRAILING_BASELINE_DAYS = 7
MIN_BASELINE_DAYS = 3
TOOL_SHARE_DROP_THRESHOLD_PTS = 10.0
NULL_RATE_THRESHOLD_PCT = 0.5
SEVERE_TOOL_SHARE_DROP_PTS = 25.0
SEVERE_NULL_RATE_PCT = 2.0

_EMPTY_RESULT = {"queries": 0, "rows": 0, "evidence": 0}


class OpenRouterAnalyticsError(ValueError):
    """The beta analytics API returned an unusable response."""


def _utc_day_bounds(collected_at: datetime) -> tuple[date, datetime, datetime]:
    utc_date = collected_at.astimezone(UTC).date()
    end = datetime.combine(utc_date, time.min, tzinfo=UTC)
    start = end - timedelta(days=1)
    return start.date(), start, end


def _iso_z(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _number(value: object, *, field: str, integer: bool = False) -> int | float:
    if isinstance(value, bool) or value is None:
        raise OpenRouterAnalyticsError(f"OpenRouter {field} is not numeric.")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise OpenRouterAnalyticsError(f"OpenRouter {field} is not numeric.") from exc
    if not parsed.is_finite() or parsed < 0:
        raise OpenRouterAnalyticsError(f"OpenRouter {field} is outside the accepted range.")
    if integer:
        if parsed != parsed.to_integral_value():
            raise OpenRouterAnalyticsError(f"OpenRouter {field} is not an integer.")
        return int(parsed)
    return float(parsed)


def _response_rows(response: httpx.Response, *, dimension: str) -> list[dict[str, Any]]:
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise OpenRouterAnalyticsError("OpenRouter analytics response has an invalid envelope.")
    envelope = payload["data"]
    rows = envelope.get("data")
    metadata = envelope.get("metadata")
    if not isinstance(rows, list) or not isinstance(metadata, dict):
        raise OpenRouterAnalyticsError("OpenRouter analytics response has an invalid data shape.")
    if metadata.get("truncated") is not False:
        raise OpenRouterAnalyticsError("OpenRouter analytics response is truncated or unmarked.")
    if any(not isinstance(row, dict) for row in rows):
        raise OpenRouterAnalyticsError("OpenRouter analytics response contains a non-object row.")
    if dimension not in {"model", "provider"}:
        raise ValueError("unsupported OpenRouter analytics dimension")
    return rows


def _query_body(
    *,
    dimensions: list[str],
    start: datetime,
    end: datetime,
    key_hash: str | None = None,
) -> dict[str, object]:
    body: dict[str, object] = {
        "metrics": ["request_count", "avg_latency"],
        "dimensions": dimensions,
        "time_range": {"start": _iso_z(start), "end": _iso_z(end)},
        "limit": OPENROUTER_QUERY_LIMIT,
    }
    if key_hash is not None:
        body["filters"] = [
            {
                "field": "api_key_id",
                "operator": "eq",
                "value": key_hash,
            }
        ]
    return body


def _query_daily(
    client: httpx.Client,
    *,
    url: str,
    target_date: date,
    scope: str,
    dimension: str,
    start: datetime,
    end: datetime,
    key_hash: str | None = None,
) -> list[OpenRouterModelDaily]:
    response = client.post(
        url,
        json=_query_body(
            dimensions=[dimension, "finish_reason"],
            start=start,
            end=end,
            key_hash=key_hash,
        ),
    )
    rows = _response_rows(response, dimension=dimension)
    parsed: list[OpenRouterModelDaily] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        raw_name = row.get(dimension)
        if not isinstance(raw_name, str) or not raw_name.strip() or len(raw_name.strip()) > 200:
            raise OpenRouterAnalyticsError(f"OpenRouter analytics row has an invalid {dimension}.")
        name = raw_name.strip()
        raw_finish_reason = row.get("finish_reason")
        if raw_finish_reason is None:
            finish_reason = "null"
        elif isinstance(raw_finish_reason, str) and raw_finish_reason.strip():
            finish_reason = raw_finish_reason.strip().lower()
        else:
            raise OpenRouterAnalyticsError("OpenRouter analytics row has an invalid finish_reason.")
        if len(finish_reason) > 64:
            raise OpenRouterAnalyticsError("OpenRouter analytics finish_reason is too long.")
        unique_dimension = (name, finish_reason)
        if unique_dimension in seen:
            raise OpenRouterAnalyticsError("OpenRouter analytics response contains duplicate dimensions.")
        seen.add(unique_dimension)

        request_count = _number(row.get("request_count"), field="request_count", integer=True)
        raw_latency = row.get("avg_latency")
        avg_latency_ms = None if raw_latency is None else _number(raw_latency, field="avg_latency", integer=False)
        parsed.append(
            OpenRouterModelDaily(
                date=target_date,
                scope=scope,
                model=name,
                finish_reason=finish_reason,
                request_count=request_count,
                avg_latency_ms=avg_latency_ms,
            )
        )
    return parsed


def _canary_key_hash() -> tuple[str | None, str]:
    canary_id = str(getattr(settings, "STEWARD_OPENROUTER_CANARY_TENANT_ID", "") or "").strip()
    if not canary_id:
        return None, "canary=tenant_id_unset"
    try:
        key_hash = Tenant.objects.filter(id=canary_id).values_list("openrouter_key_hash", flat=True).first()
    except (TypeError, ValueError, ValidationError):
        return None, "canary=tenant_id_invalid"
    if key_hash is None:
        return None, "canary=tenant_missing"
    key_hash = str(key_hash).strip()
    if not key_hash:
        return None, "canary=key_hash_unset"
    return key_hash, "canary=collected"


def _rates_for_rows(rows: list[tuple[date, str, int]]) -> dict[date, tuple[float, float]]:
    counts: dict[date, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for day, finish_reason, request_count in rows:
        counts[day][finish_reason] += request_count
    rates: dict[date, tuple[float, float]] = {}
    for day, reasons in counts.items():
        total = sum(reasons.values())
        if total <= 0:
            continue
        rates[day] = (
            reasons.get("tool_calls", 0) * 100.0 / total,
            reasons.get("null", 0) * 100.0 / total,
        )
    return rates


def _model_rates(*, scope: str, model: str, target_date: date) -> tuple[tuple[float, float] | None, list]:
    current_rows = list(
        OpenRouterModelDaily.objects.filter(
            date=target_date,
            scope=scope,
            model=model,
        ).values_list("date", "finish_reason", "request_count")
    )
    current = _rates_for_rows(current_rows).get(target_date)
    history_rows = list(
        OpenRouterModelDaily.objects.filter(
            date__gte=target_date - timedelta(days=TRAILING_BASELINE_DAYS),
            date__lt=target_date,
            scope=scope,
            model=model,
        ).values_list("date", "finish_reason", "request_count")
    )
    history = list(_rates_for_rows(history_rows).values())
    return current, history


def _rounded(value: float) -> float:
    return round(value, 4)


def _event_input(
    *,
    target_date: date,
    collected_at: datetime,
    scope: str,
    name: str,
    kind: str,
    payload: dict[str, object],
) -> EvidenceIngestInput:
    identity = hashlib.sha256(f"{scope}:{name}".encode()).hexdigest()[:16]
    return EvidenceIngestInput(
        source=EvidenceSource.OPENROUTER_MODEL_HEALTH,
        subject=f"openrouter-health:{identity}",
        occurred_at=datetime.combine(target_date, time.min, tzinfo=UTC),
        payload=payload,
        fingerprint=f"openrouter-health:{target_date}:{scope}:{kind}:{identity}",
        trust=EvidenceEvent.Trust.AUTHENTICATED_API,
        provenance=EvidenceEvent.Provenance.COLLECTOR,
    )


def _model_drift_inputs(*, target_date: date, collected_at: datetime) -> list[EvidenceIngestInput]:
    inputs: list[EvidenceIngestInput] = []
    for scope in (OpenRouterModelDaily.Scope.ACCOUNT, OpenRouterModelDaily.Scope.CANARY):
        models = (
            OpenRouterModelDaily.objects.filter(date=target_date, scope=scope)
            .order_by("model")
            .values_list("model", flat=True)
            .distinct()
        )
        for model in models:
            current, history = _model_rates(scope=scope, model=model, target_date=target_date)
            if current is None or len(history) < MIN_BASELINE_DAYS:
                continue
            current_tool_share, current_null_rate = current
            baseline_tool_share = sum(day[0] for day in history) / len(history)
            share_drop = baseline_tool_share - current_tool_share
            if share_drop > TOOL_SHARE_DROP_THRESHOLD_PTS:
                inputs.append(
                    _event_input(
                        target_date=target_date,
                        collected_at=collected_at,
                        scope=scope,
                        name=model,
                        kind="tool_calls_share_drop",
                        payload={
                            "kind": "tool_calls_share_drop",
                            "date": target_date.isoformat(),
                            "scope": scope,
                            "model": model,
                            "current_pct": _rounded(current_tool_share),
                            "baseline_pct": _rounded(baseline_tool_share),
                            "drop_pts": _rounded(share_drop),
                            "baseline_days": len(history),
                            "severe": share_drop > SEVERE_TOOL_SHARE_DROP_PTS,
                        },
                    )
                )
            if current_null_rate > NULL_RATE_THRESHOLD_PCT:
                inputs.append(
                    _event_input(
                        target_date=target_date,
                        collected_at=collected_at,
                        scope=scope,
                        name=model,
                        kind="null_rate",
                        payload={
                            "kind": "null_rate",
                            "date": target_date.isoformat(),
                            "scope": scope,
                            "model": model,
                            "current_pct": _rounded(current_null_rate),
                            "baseline_days": len(history),
                            "severe": current_null_rate > SEVERE_NULL_RATE_PCT,
                        },
                    )
                )
    return inputs


def _provider_drift_inputs(*, target_date: date, collected_at: datetime) -> list[EvidenceIngestInput]:
    history_rows = list(
        OpenRouterModelDaily.objects.filter(
            date__gte=target_date - timedelta(days=TRAILING_BASELINE_DAYS),
            date__lt=target_date,
            scope=OpenRouterModelDaily.Scope.PROVIDER,
            request_count__gt=0,
        ).values_list("date", "model")
    )
    history_days = {day for day, _ in history_rows}
    if len(history_days) < MIN_BASELINE_DAYS:
        return []
    previous_providers = {provider for _, provider in history_rows}
    current_providers = set(
        OpenRouterModelDaily.objects.filter(
            date=target_date,
            scope=OpenRouterModelDaily.Scope.PROVIDER,
            request_count__gt=0,
        ).values_list("model", flat=True)
    )
    return [
        _event_input(
            target_date=target_date,
            collected_at=collected_at,
            scope=OpenRouterModelDaily.Scope.PROVIDER,
            name=provider,
            kind="new_provider",
            payload={
                "kind": "new_provider",
                "date": target_date.isoformat(),
                "scope": OpenRouterModelDaily.Scope.PROVIDER,
                "provider": provider,
                "baseline_days": len(history_days),
                "severe": False,
            },
        )
        for provider in sorted(current_providers - previous_providers)
    ]


def drift_inputs(*, target_date: date, collected_at: datetime) -> list[EvidenceIngestInput]:
    """Compute deterministic drift findings from persisted daily aggregates."""
    return [
        *_model_drift_inputs(target_date=target_date, collected_at=collected_at),
        *_provider_drift_inputs(target_date=target_date, collected_at=collected_at),
    ]


@transaction.atomic
def _persist_daily(
    *,
    rows: list[OpenRouterModelDaily],
    target_date: date,
    collected_at: datetime,
) -> tuple[int, int]:
    set_persistence_timeouts()
    if rows:
        OpenRouterModelDaily.objects.bulk_create(
            rows,
            update_conflicts=True,
            update_fields=["request_count", "avg_latency_ms"],
            unique_fields=["date", "scope", "model", "finish_reason"],
        )
    # Collector findings stay on the digest evidence rail. Steward has direct
    # urgent delivery, but no collector-facing reservation/suppression seam.
    results = ingest_evidence_batch(
        drift_inputs(target_date=target_date, collected_at=collected_at),
        now=collected_at,
    )
    return len(rows), sum(result.created for result in results)


def collect_openrouter() -> dict[str, int]:
    """Collect the previous full UTC day under an expiring single-run lease."""
    collected_at = timezone.now()
    held_until = acquire_collector_lease(
        CollectorStatus.Collector.OPENROUTER,
        now=collected_at,
    )
    if held_until is None:
        logger.info("Steward OpenRouter collector skipped: lease already held")
        return dict(_EMPTY_RESULT)
    try:
        return _collect_openrouter(collected_at=collected_at)
    finally:
        release_collector_lease(
            CollectorStatus.Collector.OPENROUTER,
            held_until,
        )


def _collect_openrouter(*, collected_at: datetime) -> dict[str, int]:
    management_key = str(getattr(settings, "STEWARD_OPENROUTER_MGMT_KEY", "") or "").strip()
    if not management_key:
        logger.info("Steward OpenRouter collector disabled: STEWARD_OPENROUTER_MGMT_KEY is unset")
        collector_failed(
            CollectorStatus.Collector.OPENROUTER,
            attempted_at=collected_at,
            error_class="not_configured",
            detail="STEWARD_OPENROUTER_MGMT_KEY unset",
        )
        return dict(_EMPTY_RESULT)

    target_date, start, end = _utc_day_bounds(collected_at)
    key_hash, canary_detail = _canary_key_hash()
    query_count = 0
    try:
        base_url = str(getattr(settings, "OPENROUTER_API_BASE", "https://openrouter.ai/api/v1")).rstrip("/")
        query_url = f"{base_url}/analytics/query"
        rows: list[OpenRouterModelDaily] = []
        with httpx.Client(
            timeout=OPENROUTER_TIMEOUT_SECONDS,
            headers={
                "Authorization": f"Bearer {management_key}",
                "Content-Type": "application/json",
            },
        ) as client:
            rows.extend(
                _query_daily(
                    client,
                    url=query_url,
                    target_date=target_date,
                    scope=OpenRouterModelDaily.Scope.ACCOUNT,
                    dimension="model",
                    start=start,
                    end=end,
                )
            )
            query_count += 1
            if key_hash is not None:
                rows.extend(
                    _query_daily(
                        client,
                        url=query_url,
                        target_date=target_date,
                        scope=OpenRouterModelDaily.Scope.CANARY,
                        dimension="model",
                        start=start,
                        end=end,
                        key_hash=key_hash,
                    )
                )
                query_count += 1
            rows.extend(
                _query_daily(
                    client,
                    url=query_url,
                    target_date=target_date,
                    scope=OpenRouterModelDaily.Scope.PROVIDER,
                    dimension="provider",
                    start=start,
                    end=end,
                )
            )
            query_count += 1
        row_count, evidence_count = _persist_daily(
            rows=rows,
            target_date=target_date,
            collected_at=collected_at,
        )
    except Exception as exc:
        logger.warning(
            "Steward OpenRouter analytics collection failed error_class=%s",
            type(exc).__name__,
        )
        collector_failed(
            CollectorStatus.Collector.OPENROUTER,
            attempted_at=collected_at,
            error_class=type(exc).__name__,
            detail="OpenRouter analytics collection failed",
        )
        return {"queries": query_count, "rows": 0, "evidence": 0}

    collector_succeeded(
        CollectorStatus.Collector.OPENROUTER,
        attempted_at=collected_at,
        detail=";".join(
            [
                f"date={target_date.isoformat()}",
                f"rows={row_count}",
                f"evidence={evidence_count}",
                canary_detail,
            ]
        ),
    )
    return {"queries": query_count, "rows": row_count, "evidence": evidence_count}
