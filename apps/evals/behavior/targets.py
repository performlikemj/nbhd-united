"""Behavior-suite target resolution (Wave D — docs/evals-directive.md §Suite 2).

The behavior suite drives multi-turn scenarios against a SYNTHETIC behavior
tenant, targeted by env id (``EVAL_BEHAVIOR_TENANT_ID``) — never a hardcoded UUID.
That env-var name was plumbed in PR-B0 (``config/settings/base.py`` +
``production.py``), so Wave D adds no settings change. The behavior tenant is NOT
provisioned yet, so in production this resolver raises LOUDLY until the ops
provisioning step lands — a probe that cannot run must FAIL, never silently pass.
"""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError

from apps.tenants.models import Tenant


class BehaviorConfigError(RuntimeError):
    """The behavior suite is misconfigured — a LOUD failure, never a silent skip.

    docs/evals-directive.md INVARIANT #3: a suite that cannot run must FAIL, not
    quietly pass. The chassis has no "skip" state for a RUN, so an unresolvable
    behavior tenant (or an unwired transport) raises here; ``record_run`` then
    closes the run ``error`` and it lands in the DLQ, surfacing the
    misconfiguration instead of reporting a green that means nothing.
    """


def resolve_behavior_tenant() -> Tenant:
    """Return the synthetic behavior tenant, or RAISE ``BehaviorConfigError``.

    Raises (loudly) when ``EVAL_BEHAVIOR_TENANT_ID`` is unset, malformed, points at
    a missing tenant, or — defense against a config slip — points at a
    NON-synthetic tenant: a behavior scenario must never drive traffic through a
    real subscriber's account.
    """
    tenant_id = getattr(settings, "EVAL_BEHAVIOR_TENANT_ID", "") or ""
    if not tenant_id:
        raise BehaviorConfigError(
            "EVAL_BEHAVIOR_TENANT_ID is not set — cannot run the behavior suite. "
            "The behavior tenant is provisioned as an ops step (Wave D follow-up); "
            "set it to the synthetic behavior tenant's id on the Container App."
        )
    try:
        tenant = Tenant.objects.get(id=tenant_id)
    except Tenant.DoesNotExist as exc:
        raise BehaviorConfigError(f"EVAL_BEHAVIOR_TENANT_ID={tenant_id!r} does not match any tenant.") from exc
    except (ValidationError, ValueError) as exc:
        raise BehaviorConfigError(
            f"EVAL_BEHAVIOR_TENANT_ID={tenant_id!r} is not a valid tenant id ({type(exc).__name__})."
        ) from exc

    if not tenant.is_synthetic:
        raise BehaviorConfigError(
            f"EVAL_BEHAVIOR_TENANT_ID={tenant_id!r} points at a NON-synthetic tenant — "
            "refusing to run behavior scenarios against a real subscriber."
        )
    return tenant
