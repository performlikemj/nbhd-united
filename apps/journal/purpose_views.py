"""User-facing (session-auth) endpoints for the North Star (Purpose) layer.

Companion to the agent-facing runtime endpoints in ``runtime_purpose_views``.
Authenticated as the logged-in user (``IsAuthenticated`` + the user's own
tenant) so the Horizons UI can add a purpose, confirm/retire an
assistant-proposed one, and edit the statement directly.

A purpose the *user* creates here is implicitly ``confirmed`` — they wrote it,
so there is no proposal to consent to. The assistant's proposal path lives in
the runtime views and always starts at ``proposed``.

Serializer/model imports are LOCAL per ``feedback_local_reimport_pattern`` (the
lint-on-Edit hook reaps module-level imports that look unused at parse time).
"""

from __future__ import annotations

from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .document_views import _get_tenant


def _apply_status_side_effects(purpose, new_status: str) -> list[str]:
    """Set ``confirmed_at`` / ``retired_at`` timestamps for a status change.

    Returns the extra ``update_fields`` touched so the caller can persist them.
    """
    touched: list[str] = []
    if new_status == purpose.Status.CONFIRMED and purpose.confirmed_at is None:
        purpose.confirmed_at = timezone.now()
        touched.append("confirmed_at")
    if new_status == purpose.Status.RETIRED:
        purpose.retired_at = timezone.now()
        touched.append("retired_at")
    return touched


class PurposeListCreateView(APIView):
    """GET /api/v1/journal/purposes/ — list the tenant's purposes (filter: status).
    POST — create a user-authored purpose (implicitly confirmed)."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .models import Purpose
        from .purpose_serializers import PurposeSerializer

        tenant = _get_tenant(request.user)
        qs = Purpose.objects.filter(tenant=tenant)
        status_filter = request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)
        return Response(
            PurposeSerializer(
                qs.order_by("-updated_at"),
                many=True,
                context={"tenant": tenant, "rehydrate": True},
            ).data
        )

    def post(self, request):
        from .models import Purpose
        from .purpose_serializers import PurposeSerializer

        tenant = _get_tenant(request.user)
        serializer = PurposeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        from .store_authoring import author_store_fields

        authored, receipts = author_store_fields(
            tenant,
            {
                "statement": serializer.validated_data["statement"],
                "evidence": serializer.validated_data.get("evidence", []),
            },
            model_label="journal.Purpose",
            seam="journal.purpose.create.owner",
            writer="owner",
        )
        purpose = Purpose.objects.create(
            tenant=tenant,
            statement=authored["statement"],
            pillars=serializer.validated_data.get("pillars", []),
            evidence=authored["evidence"],
            pii_receipts=receipts,
            origin=Purpose.Origin.USER_CREATED,
            status=Purpose.Status.CONFIRMED,
            confirmed_at=timezone.now(),
        )
        return Response(
            PurposeSerializer(
                purpose,
                context={"tenant": tenant, "rehydrate": True},
            ).data,
            status=status.HTTP_201_CREATED,
        )


class PurposeDetailView(APIView):
    """GET/PATCH /api/v1/journal/purposes/<uuid>/ — read or update a purpose.

    PATCH accepts ``statement``, ``pillars``, and ``status`` transitions
    (confirmed / evolving / retired). Confirming or retiring stamps the
    matching timestamp.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, purpose_id):
        from .models import Purpose
        from .purpose_serializers import PurposeSerializer

        tenant = _get_tenant(request.user)
        purpose = Purpose.objects.filter(tenant=tenant, id=purpose_id).first()
        if purpose is None:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        return Response(
            PurposeSerializer(
                purpose,
                context={"tenant": tenant, "rehydrate": True},
            ).data
        )

    def patch(self, request, purpose_id):
        from .models import Purpose
        from .purpose_serializers import PurposeSerializer

        tenant = _get_tenant(request.user)
        purpose = Purpose.objects.filter(tenant=tenant, id=purpose_id).first()
        if purpose is None:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)

        serializer = PurposeSerializer(purpose, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        validated = serializer.validated_data
        from .store_authoring import author_store_fields

        authored, receipts = author_store_fields(
            tenant,
            {field: validated[field] for field in ("statement", "evidence") if field in validated},
            model_label="journal.Purpose",
            seam="journal.purpose.update.owner",
            writer="owner",
            receipts=purpose.pii_receipts,
        )

        update_fields: list[str] = []
        if "statement" in authored:
            purpose.statement = authored["statement"]
            update_fields.append("statement")
        if "pillars" in validated:
            purpose.pillars = validated["pillars"]
            update_fields.append("pillars")
        if "evidence" in authored:
            purpose.evidence = authored["evidence"]
            update_fields.append("evidence")
        if authored:
            purpose.pii_receipts = receipts
            update_fields.append("pii_receipts")
        if "status" in validated:
            new_status = validated["status"]
            purpose.status = new_status
            update_fields.append("status")
            update_fields.extend(_apply_status_side_effects(purpose, new_status))

        if update_fields:
            update_fields.append("updated_at")
            purpose.save(update_fields=update_fields)

        return Response(
            PurposeSerializer(
                purpose,
                context={"tenant": tenant, "rehydrate": True},
            ).data
        )
