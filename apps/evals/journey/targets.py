"""Journey-canary target resolution (Wave B — docs/evals-wave-b-plan.md).

The journey probes drive the REAL user path against a SYNTHETIC tenant, targeted
by env id (``EVAL_JOURNEY_TENANT_ID``) — never a hardcoded UUID, so provisioning
stays an ops step decoupled from this code.
"""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError

from apps.tenants.models import Tenant


class JourneyConfigError(RuntimeError):
    """The journey canary is misconfigured — a LOUD failure, never a silent skip.

    docs/evals-directive.md INVARIANT #3: a probe that cannot run must FAIL, not
    quietly pass. The chassis has no "skip" state, so an unresolvable target
    raises here; the run then closes ``error`` and lands in the DLQ, surfacing
    the misconfiguration instead of reporting a green that means nothing.
    """


def resolve_journey_tenant() -> Tenant:
    """Return the synthetic journey tenant, or RAISE ``JourneyConfigError``.

    Raises (loudly) when ``EVAL_JOURNEY_TENANT_ID`` is unset, malformed, points at
    a missing tenant, or — defense against a config slip — points at a
    NON-synthetic tenant: a probe must never drive traffic through a real
    subscriber's account.
    """
    tenant_id = getattr(settings, "EVAL_JOURNEY_TENANT_ID", "") or ""
    if not tenant_id:
        raise JourneyConfigError(
            "EVAL_JOURNEY_TENANT_ID is not set — cannot run the journey canary. "
            "Set it to the synthetic journey tenant's id on the Container App."
        )
    try:
        tenant = Tenant.objects.get(id=tenant_id)
    except Tenant.DoesNotExist as exc:
        raise JourneyConfigError(f"EVAL_JOURNEY_TENANT_ID={tenant_id!r} does not match any tenant.") from exc
    except (ValidationError, ValueError) as exc:
        raise JourneyConfigError(
            f"EVAL_JOURNEY_TENANT_ID={tenant_id!r} is not a valid tenant id ({type(exc).__name__})."
        ) from exc

    if not tenant.is_synthetic:
        raise JourneyConfigError(
            f"EVAL_JOURNEY_TENANT_ID={tenant_id!r} points at a NON-synthetic tenant — "
            "refusing to run a journey probe against a real subscriber."
        )
    return tenant
