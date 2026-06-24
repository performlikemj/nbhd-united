"""Fetch real infrastructure costs from Azure Cost Management API.

Runs daily via QStash cron. Stores per-tenant cost snapshots in
InfraCostSnapshot so the transparency endpoint never hits Azure at
request time.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, date, datetime, time
from decimal import Decimal

import sentry_sdk
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

# Fallback estimates used when Azure data is unavailable
ESTIMATE_CONTAINER = Decimal("4.00")
ESTIMATE_STORAGE = Decimal("0.25")
ESTIMATE_DATABASE = Decimal("0.50")


def _is_mock() -> bool:
    return os.environ.get("AZURE_MOCK", "false").lower() == "true"


def _alert_cost_degradation(reason: str, *, exc: BaseException | None = None, **context) -> None:
    """Make a degraded infra-cost refresh loud instead of silent.

    The cron deliberately swallows Azure failures and falls back to flat
    estimates so the transparency endpoint keeps serving — but a swallowed
    failure is invisible, and one hid here for months. This emits a WARNING
    log (→ container logs + Sentry Logs stream) plus a single Sentry issue
    tagged ``infra_cost_degraded=<reason>`` so the fallback is greppable and
    alertable. No-ops cleanly when Sentry is uninitialised (local/CI/no DSN).
    """
    detail = " ".join(f"{key}={value}" for key, value in context.items())
    logger.warning(
        "infra_cost_degraded reason=%s — refresh fell back to estimates %s",
        reason,
        detail,
        exc_info=exc,
    )
    try:
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("infra_cost_degraded", reason)
            for key, value in context.items():
                scope.set_extra(key, value)
            if exc is not None:
                sentry_sdk.capture_exception(exc)
            else:
                sentry_sdk.capture_message(
                    f"Infra cost refresh degraded to estimates: {reason}",
                    level="warning",
                )
    except Exception:  # pragma: no cover - alerting must never break the cron
        logger.debug("sentry capture failed during infra_cost_degraded alert", exc_info=True)


def _get_cost_management_client():
    from azure.mgmt.costmanagement import CostManagementClient

    from apps.orchestrator.azure_client import _get_provisioner_credential

    return CostManagementClient(_get_provisioner_credential())


def _query_resource_costs(month_start: date, month_end: date) -> dict[str, Decimal]:
    """Query Azure Cost Management for all resources in rg-nbhd-prod.

    Returns {resource_name_lower: cost_decimal}.
    """
    client = _get_cost_management_client()
    resource_group = getattr(settings, "AZURE_RESOURCE_GROUP", "rg-nbhd-prod")
    subscription_id = settings.AZURE_SUBSCRIPTION_ID
    scope = f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"

    # Azure's QueryTimePeriod.from/to are ISO-8601 *datetime* fields. The SDK
    # validates the request dict through a strict parser that rejects bare
    # dates ("2026-06-01" → "Invalid datetime string"), so we must include a
    # time + offset. From = start of the month, to = end of `month_end` (so
    # today's partial-day spend is included).
    period_from = datetime.combine(month_start, time.min, tzinfo=UTC)
    period_to = datetime.combine(month_end, time(23, 59, 59), tzinfo=UTC)

    query_body = {
        "type": "Usage",
        "timeframe": "Custom",
        "time_period": {
            "from_property": period_from.isoformat(),
            "to": period_to.isoformat(),
        },
        "dataset": {
            "granularity": "None",
            "aggregation": {
                "totalCost": {"name": "Cost", "function": "Sum"},
            },
            "grouping": [
                {"type": "Dimension", "name": "ResourceId"},
            ],
        },
    }

    result = client.query.usage(scope=scope, parameters=query_body)
    costs: dict[str, Decimal] = {}

    if not result.rows:
        return costs

    # Columns: [Cost, ResourceId]
    for row in result.rows:
        cost_val = Decimal(str(row[0]))
        resource_id = str(row[1])
        # Extract resource name from full resource ID
        resource_name = resource_id.rsplit("/", 1)[-1].lower()
        costs[resource_name] = costs.get(resource_name, Decimal("0")) + cost_val

    return costs


def fetch_all_container_costs(resource_costs: dict[str, Decimal]) -> dict[str, Decimal]:
    """Filter resource costs to oc-* container apps."""
    return {name: cost for name, cost in resource_costs.items() if name.startswith("oc-")}


def fetch_all_storage_costs(resource_costs: dict[str, Decimal]) -> dict[str, Decimal]:
    """Filter resource costs to ws-* file shares.

    Note: Azure may report at storage account level. If individual shares
    aren't in the results, we fall back to estimate per tenant.
    """
    return {name: cost for name, cost in resource_costs.items() if name.startswith("ws-")}


def calculate_database_share(active_tenant_count: int) -> Decimal:
    """Even split of Supabase monthly cost across active tenants."""
    supabase_cost = Decimal(str(getattr(settings, "SUPABASE_MONTHLY_COST", 25.0)))
    if active_tenant_count <= 0:
        return supabase_cost
    return (supabase_cost / active_tenant_count).quantize(Decimal("0.0001"))


def _write_estimate_snapshots(active_tenants, month_start: date, db_share: Decimal) -> int:
    """Upsert flat-estimate snapshots for every active tenant.

    Used by the AZURE_MOCK path and as the fallback when Azure billing data is
    unavailable. Returns the number of tenants written.
    """
    from apps.billing.models import InfraCostSnapshot

    total = ESTIMATE_CONTAINER + ESTIMATE_STORAGE + db_share
    count = 0
    for tenant in active_tenants:
        InfraCostSnapshot.objects.update_or_create(
            tenant=tenant,
            month=month_start,
            defaults={
                "container_cost": ESTIMATE_CONTAINER,
                "storage_cost": ESTIMATE_STORAGE,
                "database_share": db_share,
                "total_cost": total,
                "source": "estimate",
            },
        )
        count += 1
    return count


def refresh_infra_costs() -> dict:
    """Main entry point: fetch Azure costs and upsert InfraCostSnapshot rows.

    Returns summary dict for cron logging. ``degraded`` is True whenever every
    active tenant ended up on flat estimates instead of real Azure data — see
    ``_alert_cost_degradation`` for how that surfaces.
    """
    from apps.billing.models import InfraCostSnapshot
    from apps.tenants.models import Tenant

    today = timezone.now().date()
    month_start = today.replace(day=1)

    active_tenants = list(
        Tenant.objects.filter(
            status="active",
            container_id__isnull=False,
        ).exclude(container_id="")
    )

    active_count = len(active_tenants)
    db_share = calculate_database_share(active_count)

    if _is_mock():
        logger.info("AZURE_MOCK=true — using estimate fallback for all tenants")
        updated = _write_estimate_snapshots(active_tenants, month_start, db_share)
        return {"updated": updated, "source": "estimate", "degraded": False, "reason": "mock"}

    # Fetch all resource costs in one API call
    try:
        resource_costs = _query_resource_costs(month_start, today)
    except Exception as exc:
        _alert_cost_degradation("azure_query_failed", exc=exc, tenants=active_count)
        updated = _write_estimate_snapshots(active_tenants, month_start, db_share)
        return {
            "updated": updated,
            "source": "estimate",
            "degraded": True,
            "reason": "azure_query_failed",
        }

    container_costs = fetch_all_container_costs(resource_costs)
    storage_costs = fetch_all_storage_costs(resource_costs)

    updated = 0
    estimated = 0
    for tenant in active_tenants:
        container_name = (tenant.container_id or "").lower()
        # Derive storage share name from container name: oc-xxx → ws-xxx
        storage_name = f"ws-{container_name[3:]}" if container_name.startswith("oc-") else ""

        container_cost = container_costs.get(container_name, ESTIMATE_CONTAINER)
        storage_cost = storage_costs.get(storage_name, ESTIMATE_STORAGE)

        # Source is "azure" only if we got real data for at least the container
        source = "azure" if container_name in container_costs else "estimate"
        if source == "estimate":
            estimated += 1

        total = container_cost + storage_cost + db_share

        InfraCostSnapshot.objects.update_or_create(
            tenant=tenant,
            month=month_start,
            defaults={
                "container_cost": container_cost,
                "storage_cost": storage_cost,
                "database_share": db_share,
                "total_cost": total,
                "source": source,
            },
        )
        updated += 1

    azure_count = updated - estimated

    # The query "succeeded" (no exception) yet not a single active tenant got
    # real Azure data — the quiet degradation that hid for months. Alert with a
    # precise reason so the next break is actionable, not invisible.
    degraded = active_count > 0 and azure_count == 0
    reason = ""
    if degraded:
        if not resource_costs:
            reason = "azure_returned_empty"
        elif not container_costs:
            reason = "azure_no_container_resources"
        else:
            reason = "azure_no_tenant_match"
        _alert_cost_degradation(
            reason,
            tenants=active_count,
            resources_seen=len(resource_costs),
            containers_seen=len(container_costs),
        )

    logger.info(
        "Refreshed infra costs for %d tenants (azure: %d, estimate fallback: %d; %d containers, %d shares found)",
        updated,
        azure_count,
        estimated,
        len(container_costs),
        len(storage_costs),
    )

    return {
        "updated": updated,
        "source": "azure",
        "degraded": degraded,
        "reason": reason,
        "containers_found": len(container_costs),
        "shares_found": len(storage_costs),
        "tenants_on_estimate": estimated,
    }
