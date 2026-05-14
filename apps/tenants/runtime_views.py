"""Internal runtime views for tenant-level state changes from inside the agent.

Currently hosts the welcome-delivery acknowledgement endpoint used by the
Fuel and Gravity welcome cron prompts. The agent calls this after a
successful ``nbhd_send_to_user`` to mark the welcome as delivered, which:

- Lets the deploy-time backfill skip already-served tenants on re-runs.
- Closes the "scheduled but failed silently" gap — if the agent never
  reaches this endpoint, the next deploy's backfill will try again.

Pattern matches ``apps/finance/runtime_views.py``: shared internal API
key + per-tenant header, validated by ``validate_internal_runtime_request``.
"""

from __future__ import annotations

import logging
from uuid import UUID

from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.integrations.internal_auth import InternalAuthError, validate_internal_runtime_request
from apps.tenants.middleware import set_rls_context
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)


_VALID_FEATURES = {"fuel", "finance"}


def _internal_auth_or_401(request, tenant_id: UUID) -> Response | None:
    try:
        validate_internal_runtime_request(
            provided_key=request.headers.get("X-NBHD-Internal-Key", ""),
            provided_tenant_id=request.headers.get("X-NBHD-Tenant-Id", ""),
            expected_tenant_id=str(tenant_id),
        )
    except InternalAuthError as exc:
        return Response(
            {"error": "internal_auth_failed", "detail": str(exc)},
            status=status.HTTP_401_UNAUTHORIZED,
        )
    set_rls_context(tenant_id=tenant_id, service_role=True)
    return None


class RuntimeWelcomeMarkView(APIView):
    """POST /api/v1/tenants/runtime/<tenant_id>/welcomes/<feature>/

    Called by the welcome cron prompt after a successful
    ``nbhd_send_to_user`` invocation. Sets
    ``Tenant.welcomes_sent[feature]`` to the current ISO-8601 timestamp,
    so the deploy-time backfill skips this tenant on subsequent runs.

    Idempotent: re-calls overwrite the timestamp without error. Unknown
    features return 400 to surface prompt errors quickly during canary.
    """

    permission_classes = [AllowAny]

    def post(self, request, tenant_id, feature):
        auth_error = _internal_auth_or_401(request, tenant_id)
        if auth_error:
            return auth_error

        feature = (feature or "").lower().strip()
        if feature not in _VALID_FEATURES:
            return Response(
                {"error": "unknown_feature", "valid": sorted(_VALID_FEATURES)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            tenant = Tenant.objects.get(id=tenant_id)
        except Tenant.DoesNotExist:
            return Response({"error": "tenant_not_found"}, status=status.HTTP_404_NOT_FOUND)

        marks = dict(tenant.welcomes_sent or {})
        marks[feature] = timezone.now().isoformat()
        tenant.welcomes_sent = marks
        tenant.save(update_fields=["welcomes_sent", "updated_at"])

        logger.info("Welcome marked sent: tenant=%s feature=%s", str(tenant.id)[:8], feature)
        return Response({"feature": feature, "sent_at": marks[feature]})


_VALID_ENGAGEMENT_ACTIONS = {"surfaced", "abandoned", "completed", "active", "dormant", "defer"}


class RuntimeAgendaEngagementView(APIView):
    """POST /api/v1/tenants/runtime/<tenant_id>/agenda/<kind>/<item_id>/

    Records an engagement event for an agenda thread (Phase B). Called by
    OpenClaw plugins or any other source that has signal about how the
    agent surfaced a thread or how the user responded.

    Body shape::

        {"action": "surfaced", "signal": "warm" | "redirect" | ...}
        {"action": "abandoned"}
        {"action": "completed"}
        {"action": "defer", "until": "2026-05-21T00:00:00Z"}

    Authentication mirrors the welcome endpoint: shared internal API
    key + per-tenant header. ``kind`` and ``item_id`` come from the URL
    so callers don't have to deal with body validation gymnastics for
    the common case.
    """

    permission_classes = [AllowAny]

    def post(self, request, tenant_id, kind, item_id):
        from apps.tenants.agenda_models import AgendaEngagement
        from apps.tenants.agenda_service import (
            defer_until,
            mark_state,
            mark_surfaced,
            record_signal,
        )

        auth_error = _internal_auth_or_401(request, tenant_id)
        if auth_error:
            return auth_error

        if kind not in AgendaEngagement.Kind.values:
            return Response(
                {"error": "unknown_kind", "valid": list(AgendaEngagement.Kind.values)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        action = (request.data.get("action") or "").lower().strip()
        if action not in _VALID_ENGAGEMENT_ACTIONS:
            return Response(
                {"error": "unknown_action", "valid": sorted(_VALID_ENGAGEMENT_ACTIONS)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            tenant = Tenant.objects.get(id=tenant_id)
        except Tenant.DoesNotExist:
            return Response({"error": "tenant_not_found"}, status=status.HTTP_404_NOT_FOUND)

        signal = request.data.get("signal")
        if action == "surfaced":
            mark_surfaced(tenant, kind=kind, item_id=item_id, signal=signal)
            if signal:
                # ``mark_surfaced`` already logged the surface; this captures
                # any concurrent response signal in the same call.
                pass
        elif action == "defer":
            until_raw = request.data.get("until")
            if not until_raw:
                return Response(
                    {"error": "missing_until", "detail": "defer requires 'until' (ISO-8601 timestamp)"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            from datetime import datetime

            try:
                until = datetime.fromisoformat(str(until_raw).replace("Z", "+00:00"))
            except ValueError:
                return Response(
                    {"error": "invalid_until", "detail": "must be ISO-8601 timestamp"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            defer_until(tenant, kind=kind, item_id=item_id, when=until)
        elif action in ("abandoned", "completed", "active", "dormant"):
            # State-shaped actions translate one-to-one to the State enum.
            state_value = AgendaEngagement.State(action).value
            mark_state(tenant, kind=kind, item_id=item_id, state=state_value)
            if signal:
                record_signal(tenant, kind=kind, item_id=item_id, signal=signal)

        logger.info(
            "Agenda engagement recorded: tenant=%s kind=%s item=%s action=%s",
            str(tenant.id)[:8],
            kind,
            item_id,
            action,
        )
        return Response({"kind": kind, "item_id": item_id, "action": action})


class RuntimePreferredModelView(APIView):
    """GET + POST /api/v1/tenants/runtime/<tenant_id>/preferred-model/

    Internal endpoint for the assistant to read or change the tenant's
    primary model. Mirrors ``PreferredModelView`` (consumer API) for the
    write path — same tier gate via ``_get_allowed_models``, same
    pending-config bump + immediate apply enqueue. The container hits
    this from inside its own session when the user asks "switch me to
    <model>". Tier-rejected attempts surface to the user as an honest
    "not available on your tier" instead of a hallucinated success.

    GET returns the current state without mutating. POST applies a switch
    (or clears to the tier default when ``model_id`` is empty).
    """

    permission_classes = [AllowAny]

    @staticmethod
    def _state(tenant: Tenant) -> dict:
        from apps.orchestrator.config_generator import TIER_MODEL_CONFIGS, _byo_model_extras

        allowed = {
            **TIER_MODEL_CONFIGS.get(tenant.model_tier, {}),
            **_byo_model_extras(tenant),
        }
        return {
            "preferred_model": tenant.preferred_model,
            "applied_model": tenant.applied_model,
            "model_tier": tenant.model_tier,
            "allowed_models": [
                {"model_id": mid, "alias": (meta or {}).get("alias", "")} for mid, meta in allowed.items()
            ],
        }

    def get(self, request, tenant_id):
        auth_error = _internal_auth_or_401(request, tenant_id)
        if auth_error:
            return auth_error

        try:
            tenant = Tenant.objects.get(id=tenant_id)
        except Tenant.DoesNotExist:
            return Response({"error": "tenant_not_found"}, status=status.HTTP_404_NOT_FOUND)

        return Response(self._state(tenant))

    def post(self, request, tenant_id):
        from apps.tenants.views import _enqueue_immediate_apply, _get_allowed_models

        auth_error = _internal_auth_or_401(request, tenant_id)
        if auth_error:
            return auth_error

        try:
            tenant = Tenant.objects.get(id=tenant_id)
        except Tenant.DoesNotExist:
            return Response({"error": "tenant_not_found"}, status=status.HTTP_404_NOT_FOUND)

        model_id = (request.data.get("model_id") or "").strip()

        if model_id:
            allowed = _get_allowed_models(tenant)
            if model_id not in allowed:
                response_body = {
                    "error": "model_not_allowed",
                    "detail": (
                        f"Model {model_id!r} is not available on tier {tenant.model_tier!r}. "
                        "Available models are listed in 'allowed_models'."
                    ),
                    **self._state(tenant),
                }
                return Response(response_body, status=status.HTTP_400_BAD_REQUEST)

        tenant.preferred_model = model_id
        tenant.save(update_fields=["preferred_model"])
        tenant.bump_pending_config()
        _enqueue_immediate_apply(tenant)

        logger.info(
            "Runtime preferred_model set: tenant=%s model=%s",
            str(tenant.id)[:8],
            model_id or "(cleared)",
        )
        return Response({"updated": True, **self._state(tenant)})


class RuntimeCommitmentRecordView(APIView):
    """POST /api/v1/tenants/runtime/<tenant_id>/commitments/

    Phase D — the agent records a future-aware commitment to follow up
    with the user. Body:

        {
          "about": "<topic>",
          "surface_after": "2026-05-21T00:00:00Z",
          "why": "<reasoning, for future context>"
        }

    Returns ``{kind, item_id}`` so the agent (or its plugin layer) can
    reference the commitment later if needed. Idempotent on
    ``about``-text content hash — same about-text reuses the row
    rather than duplicating.
    """

    permission_classes = [AllowAny]

    def post(self, request, tenant_id):
        from datetime import datetime

        from apps.tenants.agenda_models import AgendaEngagement
        from apps.tenants.agenda_service import record_commitment

        auth_error = _internal_auth_or_401(request, tenant_id)
        if auth_error:
            return auth_error

        about = (request.data.get("about") or "").strip()
        why = (request.data.get("why") or "").strip()
        surface_after_raw = request.data.get("surface_after")

        if not about:
            return Response(
                {"error": "missing_about", "detail": "'about' is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not surface_after_raw:
            return Response(
                {"error": "missing_surface_after", "detail": "'surface_after' is required (ISO-8601)"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            surface_after = datetime.fromisoformat(str(surface_after_raw).replace("Z", "+00:00"))
        except ValueError:
            return Response(
                {"error": "invalid_surface_after", "detail": "must be ISO-8601 timestamp"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            tenant = Tenant.objects.get(id=tenant_id)
        except Tenant.DoesNotExist:
            return Response({"error": "tenant_not_found"}, status=status.HTTP_404_NOT_FOUND)

        commitment = record_commitment(
            tenant,
            about=about,
            surface_after=surface_after,
            why=why,
        )
        logger.info(
            "Commitment recorded: tenant=%s about=%r surface_after=%s",
            str(tenant.id)[:8],
            about[:80],
            surface_after.isoformat(),
        )
        return Response(
            {
                "kind": AgendaEngagement.Kind.ASSISTANT_COMMITMENT,
                "item_id": commitment.item_id,
                "surface_after": commitment.surface_after.isoformat() if commitment.surface_after else None,
            }
        )
