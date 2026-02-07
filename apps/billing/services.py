"""Stripe customer and subscription management."""
import logging

from apps.tenants.models import Tenant
from .models import Plan, UsageEvent

logger = logging.getLogger(__name__)


def record_usage(tenant: Tenant, event_type: str, tokens: int, model_used: str = "", cost: float = 0.0):
    """Record a usage event for billing tracking."""
    UsageEvent.objects.create(
        tenant=tenant,
        event_type=event_type,
        tokens=tokens,
        model_used=model_used,
        cost_estimate=cost,
    )


def get_tenant_plan(tenant: Tenant) -> Plan | None:
    """Get the active plan for a tenant."""
    return Plan.objects.filter(tier=tenant.plan_tier, is_active=True).first()
