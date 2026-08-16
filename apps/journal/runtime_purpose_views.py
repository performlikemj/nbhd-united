"""Internal runtime endpoints for the North Star (Purpose) layer.

The agent-facing half of the Purpose layer. Mirrors the internal-runtime auth
pattern used by ``apps.insights.runtime_views`` and
``apps.integrations.runtime_views`` (``X-NBHD-Internal-Key`` +
``X-NBHD-Tenant-Id``). The ``nbhd_purpose_*`` tools in
``runtime/openclaw/plugins/nbhd-journal-tools`` call these.

Consent is enforced in the transport, not just the prompt: ``purpose_confirm``
REQUIRES a ``user_confirmed: true`` flag in the body. The tool only sets that
flag after the user has explicitly agreed in conversation — the assistant can
never hard-confirm a North Star the user hasn't assented to. A propose call
always lands at ``status=proposed`` so an un-confirmed hypothesis never grounds
the assistant's reasoning (USER.md renders confirmed/evolving only).

Model/serializer imports are LOCAL inside each method per
``feedback_local_reimport_pattern`` — the lint-on-Edit hook reaps module-level
imports that look unused at parse time.
"""

from __future__ import annotations

from uuid import UUID

from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.integrations.internal_auth import InternalAuthError, validate_internal_runtime_request
from apps.router.document_write_guard import record_runtime_write_activity
from apps.tenants.middleware import set_rls_context
from apps.tenants.models import Tenant


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


def _get_tenant_or_404(tenant_id: UUID) -> Tenant | Response:
    try:
        return Tenant.objects.get(id=tenant_id)
    except Tenant.DoesNotExist:
        return Response({"error": "tenant_not_found"}, status=status.HTTP_404_NOT_FOUND)


class RuntimePurposeListView(APIView):
    """GET runtime/<tenant_id>/purposes/ — list purposes (filter: ?status=)."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    def get(self, request, tenant_id):
        from .models import Purpose
        from .purpose_serializers import PurposeSerializer

        if err := _internal_auth_or_401(request, tenant_id):
            return err
        tenant = _get_tenant_or_404(tenant_id)
        if isinstance(tenant, Response):
            return tenant

        qs = Purpose.objects.filter(tenant=tenant)
        status_filter = (request.query_params.get("status") or "").strip()
        if status_filter:
            qs = qs.filter(status=status_filter)
        data = PurposeSerializer(qs.order_by("-updated_at"), many=True).data
        return Response({"tenant_id": str(tenant.id), "purposes": data, "count": len(data)})


class RuntimePurposeProposeView(APIView):
    """POST runtime/<tenant_id>/purposes/propose/ — propose a North Star.

    Always creates ``status=proposed``, ``origin=assistant_proposed``. A
    proposal is a question, not a fact — it does NOT surface in USER.md until
    the user confirms it.
    """

    permission_classes = [AllowAny]
    authentication_classes: list = []

    def post(self, request, tenant_id):
        from .models import Purpose
        from .purpose_serializers import PurposeSerializer

        if err := _internal_auth_or_401(request, tenant_id):
            return err
        tenant = _get_tenant_or_404(tenant_id)
        if isinstance(tenant, Response):
            return tenant
        record_runtime_write_activity(tenant)

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
            seam="journal.purpose.propose.runtime",
            writer="runtime",
            defer_detection=True,
        )
        purpose = Purpose.objects.create(
            tenant=tenant,
            statement=authored["statement"],
            pillars=serializer.validated_data.get("pillars", []),
            evidence=authored["evidence"],
            pii_receipts=receipts,
            origin=Purpose.Origin.ASSISTANT_PROPOSED,
            status=Purpose.Status.PROPOSED,
        )
        return Response(
            {"tenant_id": str(tenant.id), "purpose": PurposeSerializer(purpose).data},
            status=status.HTTP_201_CREATED,
        )


class RuntimePurposeConfirmView(APIView):
    """POST runtime/<tenant_id>/purposes/<uuid>/confirm/ — confirm a North Star.

    CONSENT GATE: requires ``{"user_confirmed": true}`` in the body. The tool
    only sets that flag once the user has explicitly agreed in conversation.
    Without it the request is rejected 403 — the assistant cannot silently
    promote a proposal into a confirmed fact.
    """

    permission_classes = [AllowAny]
    authentication_classes: list = []

    def post(self, request, tenant_id, purpose_id):
        from .models import Purpose
        from .purpose_serializers import PurposeSerializer

        if err := _internal_auth_or_401(request, tenant_id):
            return err
        tenant = _get_tenant_or_404(tenant_id)
        if isinstance(tenant, Response):
            return tenant

        if request.data.get("user_confirmed") is not True:
            return Response(
                {
                    "error": "consent_required",
                    "detail": (
                        "purpose_confirm requires user_confirmed=true — confirm a North Star "
                        "only after the user has explicitly agreed in conversation."
                    ),
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        record_runtime_write_activity(tenant)

        purpose = Purpose.objects.filter(tenant=tenant, id=purpose_id).first()
        if purpose is None:
            return Response({"error": "purpose_not_found"}, status=status.HTTP_404_NOT_FOUND)

        purpose.confirm()
        return Response({"tenant_id": str(tenant.id), "purpose": PurposeSerializer(purpose).data})


class RuntimePurposeUpdateView(APIView):
    """PATCH runtime/<tenant_id>/purposes/<uuid>/ — edit statement/pillars/evidence.

    May also move a CONFIRMED purpose to ``evolving`` (the user is reshaping a
    direction they already own). It may NOT promote a ``proposed`` purpose to
    ``confirmed`` — that path goes through ``confirm/`` with the consent flag.
    """

    permission_classes = [AllowAny]
    authentication_classes: list = []

    def patch(self, request, tenant_id, purpose_id):
        from .models import Purpose
        from .purpose_serializers import PurposeSerializer

        if err := _internal_auth_or_401(request, tenant_id):
            return err
        tenant = _get_tenant_or_404(tenant_id)
        if isinstance(tenant, Response):
            return tenant
        record_runtime_write_activity(tenant)

        purpose = Purpose.objects.filter(tenant=tenant, id=purpose_id).first()
        if purpose is None:
            return Response({"error": "purpose_not_found"}, status=status.HTTP_404_NOT_FOUND)

        serializer = PurposeSerializer(purpose, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        validated = serializer.validated_data
        from .store_authoring import author_store_fields

        authored, receipts = author_store_fields(
            tenant,
            {field: validated[field] for field in ("statement", "evidence") if field in validated},
            model_label="journal.Purpose",
            seam="journal.purpose.update.runtime",
            writer="runtime",
            receipts=purpose.pii_receipts,
            defer_detection=True,
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
            # Guard the consent path: an un-confirmed proposal can never become
            # confirmed here — the assistant must use confirm/ with user assent.
            if new_status == Purpose.Status.CONFIRMED and purpose.status == Purpose.Status.PROPOSED:
                return Response(
                    {
                        "error": "consent_required",
                        "detail": "Use purpose_confirm (with user assent) to confirm a proposed North Star.",
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )
            if new_status in {Purpose.Status.EVOLVING, Purpose.Status.CONFIRMED}:
                purpose.status = new_status
                update_fields.append("status")
                if new_status == Purpose.Status.CONFIRMED and purpose.confirmed_at is None:
                    purpose.confirmed_at = timezone.now()
                    update_fields.append("confirmed_at")

        if update_fields:
            update_fields.append("updated_at")
            purpose.save(update_fields=update_fields)

        return Response({"tenant_id": str(tenant.id), "purpose": PurposeSerializer(purpose).data})


class RuntimePurposeRetireView(APIView):
    """POST runtime/<tenant_id>/purposes/<uuid>/retire/ — retire a North Star.

    Non-destructive: preserved for history, stamped ``retired_at``, and drops
    out of USER.md and grounding.
    """

    permission_classes = [AllowAny]
    authentication_classes: list = []

    def post(self, request, tenant_id, purpose_id):
        from .models import Purpose
        from .purpose_serializers import PurposeSerializer

        if err := _internal_auth_or_401(request, tenant_id):
            return err
        tenant = _get_tenant_or_404(tenant_id)
        if isinstance(tenant, Response):
            return tenant
        record_runtime_write_activity(tenant)

        purpose = Purpose.objects.filter(tenant=tenant, id=purpose_id).first()
        if purpose is None:
            return Response({"error": "purpose_not_found"}, status=status.HTTP_404_NOT_FOUND)

        purpose.retire()
        return Response({"tenant_id": str(tenant.id), "purpose": PurposeSerializer(purpose).data})


class RuntimePurposeLinkGoalView(APIView):
    """POST runtime/<tenant_id>/purposes/<uuid>/link-goal/ — attach a Goal.

    Body: ``{"goal_id": "<uuid>"}``. Sets ``Goal.purpose`` so the North Star
    gathers the goals that move the user toward it. Both rows are tenant-scoped
    — a cross-tenant goal_id 404s.
    """

    permission_classes = [AllowAny]
    authentication_classes: list = []

    def post(self, request, tenant_id, purpose_id):
        from .models import Goal, Purpose
        from .purpose_serializers import PurposeSerializer

        if err := _internal_auth_or_401(request, tenant_id):
            return err
        tenant = _get_tenant_or_404(tenant_id)
        if isinstance(tenant, Response):
            return tenant
        record_runtime_write_activity(tenant)

        purpose = Purpose.objects.filter(tenant=tenant, id=purpose_id).first()
        if purpose is None:
            return Response({"error": "purpose_not_found"}, status=status.HTTP_404_NOT_FOUND)

        raw_goal_id = request.data.get("goal_id")
        goal_id = raw_goal_id.strip() if isinstance(raw_goal_id, str) else raw_goal_id
        if not goal_id:
            return Response(
                {"error": "invalid_request", "detail": "goal_id is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        goal = Goal.objects.filter(tenant=tenant, id=goal_id).first()
        if goal is None:
            return Response({"error": "goal_not_found"}, status=status.HTTP_404_NOT_FOUND)

        goal.purpose = purpose
        goal.save(update_fields=["purpose", "updated_at"])
        return Response(
            {
                "tenant_id": str(tenant.id),
                "purpose": PurposeSerializer(purpose).data,
                "linked_goal_id": str(goal.id),
            }
        )
