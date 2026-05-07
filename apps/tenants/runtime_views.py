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
