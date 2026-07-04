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

# Grace window (days from the 1st) in which an empty/partial Azure Cost
# Management result is treated as expected month-rollover billing lag rather than
# a real degradation. Azure posts month-to-date actuals ~1-3 days into a new
# month, so the daily refresh legitimately finds no container costs until then.
EARLY_MONTH_GRACE_DAYS = 3


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
            # Group intentionally by reason so grouping is stable and not derived
            # from the (varying) message string — one issue per degradation reason,
            # not a fresh "new issue" each time the detail text shifts.
            scope.fingerprint = ["infra_cost_degraded", reason]
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
    """Per-tenant slice of the shared Supabase monthly cost, capped.

    An even split ($cost / N) loads the *entire* database bill onto whatever
    fleet exists today: at N=3 that is ~$8.33/tenant, which alone exceeds the
    subscription price and structurally zeroes the open-books surplus → no
    donation ever shows. A DB instance has capacity for far more tenants than a
    small early fleet, so the honest marginal per-tenant cost is small. We cap
    the share at ``INFRA_DB_SHARE_CAP`` (the fair per-tenant DB cost at target
    scale) so a small fleet isn't overcharged; once the fleet is large enough
    that the even split drops below the cap, the real split applies.
    """
    supabase_cost = Decimal(str(getattr(settings, "SUPABASE_MONTHLY_COST", 25.0)))
    cap = Decimal(str(getattr(settings, "INFRA_DB_SHARE_CAP", 0.50)))
    even_split = supabase_cost if active_tenant_count <= 0 else supabase_cost / active_tenant_count
    return min(even_split, cap).quantize(Decimal("0.0001"))


def _write_estimate_snapshots(active_tenants, month_start: date, db_share: Decimal) -> int:
    """Upsert flat-estimate snapshots for every active tenant.

    Used by the AZURE_MOCK path and as the fallback when Azure billing data is
    unavailable. Returns the number of tenants written.

    Never downgrades a real ``azure`` row to an estimate: a transient query
    failure must not wipe month-to-date costs we already collected (that would
    whipsaw the displayed infra total → surplus → donation). Tenants that already
    hold real Azure data for the month are left untouched.
    """
    from apps.billing.models import InfraCostSnapshot

    had_real_azure_ids = set(
        InfraCostSnapshot.objects.filter(month=month_start, source="azure").values_list("tenant_id", flat=True)
    )
    total = ESTIMATE_CONTAINER + ESTIMATE_STORAGE + db_share
    count = 0
    for tenant in active_tenants:
        if tenant.id in had_real_azure_ids:
            continue
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


def _prev_month_start(month_start: date) -> date:
    """First day of the month before ``month_start`` (handles the Jan → Dec wrap)."""
    if month_start.month == 1:
        return date(month_start.year - 1, 12, 1)
    return date(month_start.year, month_start.month - 1, 1)


def _is_expected_billing_lag(today: date, month_start: date, reason: str) -> bool:
    """Whether an empty/partial Azure result is expected month-rollover billing
    lag (so the alert should be suppressed) rather than a real degradation.

    Azure Cost Management posts month-to-date actuals with a ~1-3 day lag, so at
    the very start of a month the query legitimately returns no container costs
    and the cron falls back to estimates. That is expected — and only that — when
    ALL of the following hold:

    * we are inside the early-month window (``EARLY_MONTH_GRACE_DAYS``);
    * the signature is an empty/partial posting, not ``azure_no_tenant_match``
      (which means resources *were* found but none matched a tenant — a naming/
      config break, not billing lag);
    * the pipeline produced real Azure data last month, so this is not a
      never-worked misconfiguration that should alert on day 1; and
    * we have not already captured real Azure data this month, so an empty result
      *after* real data landed is treated as a regression and still alerts.

    A genuine "all containers gone" catastrophe on the 1st is caught faster by
    liveness monitors (health checks, the orphan reaper); this cost cron trading a
    ~1-3 day delayed, redundant signal for zero monthly false alarms is the right
    trade.
    """
    from apps.billing.models import InfraCostSnapshot

    if today.day > EARLY_MONTH_GRACE_DAYS:
        return False
    if reason not in ("azure_returned_empty", "azure_no_container_resources"):
        return False
    pipeline_proven = InfraCostSnapshot.objects.filter(month=_prev_month_start(month_start), source="azure").exists()
    if not pipeline_proven:
        return False
    already_have_real_this_month = InfraCostSnapshot.objects.filter(month=month_start, source="azure").exists()
    return not already_have_real_this_month


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

    # Tenants already holding REAL Azure data for this month. A transient empty
    # return or a partial early-month posting must not overwrite those rows back
    # to flat estimates (that whipsaws the displayed infra cost → surplus →
    # donation). Captured pre-write.
    had_real_azure_ids = set(
        InfraCostSnapshot.objects.filter(month=month_start, source="azure").values_list("tenant_id", flat=True)
    )

    updated = 0
    estimated = 0
    preserved = 0
    for tenant in active_tenants:
        container_name = (tenant.container_id or "").lower()
        # Derive storage share name from container name: oc-xxx → ws-xxx
        storage_name = f"ws-{container_name[3:]}" if container_name.startswith("oc-") else ""

        if container_name in container_costs:
            # Real Azure data for this container this run.
            source = "azure"
            container_cost = container_costs[container_name]
            storage_cost = storage_costs.get(storage_name, ESTIMATE_STORAGE)
        elif tenant.id in had_real_azure_ids:
            # We already collected real Azure data for this tenant this month and
            # this run missed it (transient/partial) — keep the real row instead
            # of downgrading it to an estimate.
            preserved += 1
            continue
        else:
            source = "estimate"
            estimated += 1
            container_cost = ESTIMATE_CONTAINER
            storage_cost = ESTIMATE_STORAGE

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

    # The query "succeeded" (no exception) yet not a single active tenant has real
    # Azure data — neither fetched this run nor preserved from earlier this month.
    # This is the quiet degradation that hid for months; alert with a precise
    # reason so the next break is actionable, not invisible.
    degraded = active_count > 0 and azure_count == 0 and preserved == 0
    reason = ""
    if degraded:
        if not resource_costs:
            reason = "azure_returned_empty"
        elif not container_costs:
            reason = "azure_no_container_resources"
        else:
            reason = "azure_no_tenant_match"

        # Month-rollover billing lag is expected, not a degradation: on the first
        # day(s) of a month Azure has not posted month-to-date actuals yet, so an
        # empty/partial result is normal. Keep the estimate snapshots already
        # written above (a conservative $/mo placeholder that keeps surplus →
        # donation honest until real data lands) but suppress the Sentry alert —
        # only when the pipeline has demonstrably worked before and not yet this
        # month, so a never-worked misconfig or a mid-month regression still fires.
        if _is_expected_billing_lag(today, month_start, reason):
            logger.info(
                "infra_cost early-month billing lag (day %d, reason=%s) — Azure "
                "hasn't posted %s data yet; kept estimates, suppressed alert",
                today.day,
                reason,
                month_start.strftime("%B %Y"),
            )
            degraded = False
            reason = "early_month_billing_lag"
        else:
            _alert_cost_degradation(
                reason,
                tenants=active_count,
                resources_seen=len(resource_costs),
                containers_seen=len(container_costs),
            )

    logger.info(
        "Refreshed infra costs for %d tenants (azure: %d, estimate fallback: %d, "
        "preserved real: %d; %d containers, %d shares found)",
        updated,
        azure_count,
        estimated,
        preserved,
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
        "tenants_preserved_real": preserved,
    }
