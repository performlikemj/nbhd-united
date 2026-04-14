"""Fetch real infrastructure costs from Azure Cost Management API.

Runs daily via QStash cron. Stores per-tenant cost snapshots in
InfraCostSnapshot so the transparency endpoint never hits Azure at
request time.
"""

from __future__ import annotations

import logging
import os
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

# Fallback estimates used when Azure data is unavailable
ESTIMATE_CONTAINER = Decimal("4.00")
ESTIMATE_STORAGE = Decimal("0.25")
ESTIMATE_DATABASE = Decimal("0.50")


def _is_mock() -> bool:
    return os.environ.get("AZURE_MOCK", "false").lower() == "true"


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

    query_body = {
        "type": "Usage",
        "timeframe": "Custom",
        "time_period": {
            "from_property": month_start.isoformat(),
            "to": month_end.isoformat(),
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


def refresh_infra_costs() -> dict:
    """Main entry point: fetch Azure costs and upsert InfraCostSnapshot rows.

    Returns summary dict for cron logging.
    """
    from apps.billing.models import InfraCostSnapshot
    from apps.tenants.models import Tenant

    today = timezone.now().date()
    month_start = today.replace(day=1)

    active_tenants = Tenant.objects.filter(
        status="active",
        container_id__isnull=False,
    ).exclude(container_id="")

    active_count = active_tenants.count()
    db_share = calculate_database_share(active_count)

    if _is_mock():
        logger.info("AZURE_MOCK=true — using estimate fallback for all tenants")
        for tenant in active_tenants:
            InfraCostSnapshot.objects.update_or_create(
                tenant=tenant,
                month=month_start,
                defaults={
                    "container_cost": ESTIMATE_CONTAINER,
                    "storage_cost": ESTIMATE_STORAGE,
                    "database_share": db_share,
                    "total_cost": ESTIMATE_CONTAINER + ESTIMATE_STORAGE + db_share,
                    "source": "estimate",
                },
            )
        return {"updated": active_count, "source": "estimate"}

    # Fetch all resource costs in one API call
    try:
        resource_costs = _query_resource_costs(month_start, today)
    except Exception:
        logger.exception("Failed to query Azure Cost Management — falling back to estimates")
        for tenant in active_tenants:
            InfraCostSnapshot.objects.update_or_create(
                tenant=tenant,
                month=month_start,
                defaults={
                    "container_cost": ESTIMATE_CONTAINER,
                    "storage_cost": ESTIMATE_STORAGE,
                    "database_share": db_share,
                    "total_cost": ESTIMATE_CONTAINER + ESTIMATE_STORAGE + db_share,
                    "source": "estimate",
                },
            )
        return {"updated": active_count, "source": "estimate", "error": "azure_query_failed"}

    container_costs = fetch_all_container_costs(resource_costs)
    storage_costs = fetch_all_storage_costs(resource_costs)

    updated = 0
    for tenant in active_tenants:
        container_name = (tenant.container_id or "").lower()
        # Derive storage share name from container name: oc-xxx → ws-xxx
        storage_name = f"ws-{container_name[3:]}" if container_name.startswith("oc-") else ""

        container_cost = container_costs.get(container_name, ESTIMATE_CONTAINER)
        storage_cost = storage_costs.get(storage_name, ESTIMATE_STORAGE)

        # Source is "azure" only if we got real data for at least the container
        source = "azure" if container_name in container_costs else "estimate"

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

    logger.info(
        "Refreshed infra costs for %d tenants (azure data: %d containers, %d shares)",
        updated,
        len(container_costs),
        len(storage_costs),
    )

    return {
        "updated": updated,
        "source": "azure",
        "containers_found": len(container_costs),
        "shares_found": len(storage_costs),
    }
