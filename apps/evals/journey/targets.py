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
    """Return the configured eval-sink journey tenant, or raise.

    Raises (loudly) when ``EVAL_JOURNEY_TENANT_ID`` is unset, malformed, points at
    a missing tenant, or — defense against a config slip — points at a
    tenant that is not explicitly marked as an eval sink: a probe must never
    drive traffic through an ordinary or demo account.
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

    if not tenant.is_eval_sink:
        raise JourneyConfigError(
            f"EVAL_JOURNEY_TENANT_ID={tenant_id!r} does not point at an eval-sink tenant — "
            "refusing to run a journey probe against an ordinary or demo account."
        )
    return tenant


# --- Delivery channel -------------------------------------------------------
#
# RETIRED 2026-07-14. This module used to plant a FAKE iOS ``DeviceToken`` before
# every cron arm, because ``resolve_user_channel`` returned None for a channel-less
# synthetic tenant and ``CronDeliveryView`` then 422'd before writing the
# ``ProactiveOutbound`` row the probe asserts on.
#
# That hack was self-destroying: a SUCCESSFUL fire pushed to the fabricated token,
# APNs rejected it as ``BadDeviceToken``, and ``push_views`` pruned the row — so
# every pass destroyed the channel for the next fire (prod runs 8→9 alternated
# pass/fail forever), and it fired a real, rejected request at Apple on every run.
#
# Explicit eval-sink tenants now resolve to ``eval`` instead
# (``resolve_user_channel``, gated on ``Tenant.is_eval_sink``): no external
# transport is called, the ProactiveOutbound row is internal delivery evidence,
# and there is no token to prune or Apple round trip.
