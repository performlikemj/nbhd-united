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


# --- Synthetic iOS delivery channel (journey_cron precondition) --------------
#
# ``resolve_user_channel`` (apps/router/cron_delivery.py) routes to the "app"
# channel — and ``CronDeliveryView`` only writes the ``ProactiveOutbound`` row
# the cron probe asserts on — when the user has at least one ``DeviceToken``. The
# synthetic journey user has no Telegram/LINE link, so the cron probe MUST plant
# an iOS ``DeviceToken`` before it arms, or the delivery view returns
# ``422 no_channel_linked``.
#
# The catch that makes this a per-run STEP, not one-time setup: a SUCCESSFUL fire
# pushes to this (fabricated) token, APNs rejects it as ``BadDeviceToken``
# (apps/common/apns.py), and ``_push_to_user_devices`` prunes the row
# (apps/router/push_views.py) so the table self-heals on reinstall. So every pass
# destroys the channel for the next fire — a daily schedule alternated pass/fail
# forever (prod runs 8→9: run 8 PASS pruned the token → run 9 no_channel_linked).
# Re-ensuring the token before each arm heals it.
#
# The token value is a fixed, obviously-synthetic 64-hex string. It never reaches
# a real device — the only push it could ever receive is the probe's own, which
# APNs rejects — and carries the same ``bundle_id`` / ``sandbox`` environment an
# eval-synthetic iOS install would.
SYNTHETIC_DEVICE_TOKEN = "0" * 64
SYNTHETIC_DEVICE_BUNDLE_ID = "org.hoodunited.nbhd.eval-synthetic"
SYNTHETIC_DEVICE_ENVIRONMENT = "sandbox"


def ensure_synthetic_delivery_channel(tenant) -> bool:
    """Get-or-create the synthetic tenant's iOS ``DeviceToken``; return ``created``.

    ``resolve_user_channel`` only routes to the app channel when a ``DeviceToken``
    exists for the user, and a successful cron fire prunes the fabricated token
    (see the module comment above), so the cron probe calls this BEFORE every arm.

    Returns ``True`` when a new row was created (the prior fire pruned it, or it
    never existed) and ``False`` when the row was already present — content-free
    metadata for the run details, never any user data.

    Raises on failure (does not swallow) so the caller's ``record_run`` closes the
    run ERROR: a probe that cannot set up its own delivery precondition is broken,
    never a silent skip (docs/evals-directive.md INVARIANT #3).
    """
    from apps.router.models import DeviceToken

    # Keyed on ``token`` (its sole unique constraint) so the call is idempotent —
    # a second run returns the existing row instead of colliding on the constraint.
    _row, created = DeviceToken.objects.get_or_create(
        token=SYNTHETIC_DEVICE_TOKEN,
        defaults={
            "tenant": tenant,
            "user": tenant.user,
            "environment": SYNTHETIC_DEVICE_ENVIRONMENT,
            "bundle_id": SYNTHETIC_DEVICE_BUNDLE_ID,
        },
    )
    return created
