"""Session API views — CRUD for external app work sessions."""

import logging

from django.db import IntegrityError
from django.utils.text import slugify
from rest_framework import serializers, status
from rest_framework.generics import get_object_or_404
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.tenants.permissions import HasSessionsReadScope, HasSessionsWriteScope
from apps.tenants.throttling import PATSessionIngestDayThrottle, PATSessionIngestMinuteThrottle

from .models import Document
from .session_models import Session

logger = logging.getLogger(__name__)


class SessionCreateSerializer(serializers.Serializer):
    source = serializers.CharField(max_length=128)
    project = serializers.CharField(max_length=256)
    project_identity = serializers.CharField(max_length=512, required=False, default="", allow_blank=True)
    project_type = serializers.CharField(max_length=128, required=False, default="")
    session_start = serializers.DateTimeField()
    session_end = serializers.DateTimeField()
    summary = serializers.CharField()
    accomplishments = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    blockers = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    next_steps = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    references = serializers.DictField(required=False, default=dict)
    test_mode = serializers.BooleanField(required=False, default=False)
    schema_version = serializers.IntegerField(required=False, default=1)

    def validate(self, attrs):
        if attrs["session_end"] <= attrs["session_start"]:
            raise serializers.ValidationError("session_end must be after session_start.")
        return attrs


class SessionDetailSerializer(serializers.ModelSerializer):
    class Meta:
        model = Session
        fields = (
            "id",
            "source",
            "project",
            "project_identity",
            "project_type",
            "session_start",
            "session_end",
            "summary",
            "accomplishments",
            "blockers",
            "next_steps",
            "references",
            "test_mode",
            "schema_version",
            "created_at",
        )
        read_only_fields = fields


def _ensure_project_document(tenant, project_name: str) -> None:
    """Auto-create a Document(kind='project') if one doesn't exist."""
    slug = slugify(project_name)[:128]
    if not slug:
        return
    Document.objects.get_or_create(
        tenant=tenant,
        kind=Document.Kind.PROJECT,
        slug=slug,
        defaults={"title": project_name},
    )


class SessionCreateView(APIView):
    """POST /api/v1/sessions/ — push a work session from an external app."""

    permission_classes = [HasSessionsWriteScope]
    throttle_classes = [PATSessionIngestMinuteThrottle, PATSessionIngestDayThrottle]

    def post(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"detail": "No tenant found."}, status=status.HTTP_404_NOT_FOUND)

        serializer = SessionCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        idempotency_key = request.META.get("HTTP_IDEMPOTENCY_KEY", "")

        # Idempotency: return existing session if key matches
        if idempotency_key:
            existing = Session.objects.filter(tenant=tenant, idempotency_key=idempotency_key).first()
            if existing:
                return Response(SessionDetailSerializer(existing).data, status=status.HTTP_200_OK)

        try:
            session = Session.objects.create(
                tenant=tenant,
                source=data["source"],
                project=data["project"],
                project_identity=data.get("project_identity", ""),
                project_type=data.get("project_type", ""),
                session_start=data["session_start"],
                session_end=data["session_end"],
                summary=data["summary"],
                accomplishments=data.get("accomplishments", []),
                blockers=data.get("blockers", []),
                next_steps=data.get("next_steps", []),
                references=data.get("references", {}),
                test_mode=data.get("test_mode", False),
                schema_version=data.get("schema_version", 1),
                idempotency_key=idempotency_key,
            )
        except IntegrityError:
            # Race condition on idempotency key — fetch and return
            if idempotency_key:
                existing = Session.objects.filter(tenant=tenant, idempotency_key=idempotency_key).first()
                if existing:
                    return Response(SessionDetailSerializer(existing).data, status=status.HTTP_200_OK)
            raise

        # Auto-create project document
        _ensure_project_document(tenant, data["project"])

        logger.info(
            "session_created tenant_id=%s project=%s source=%s test_mode=%s",
            tenant.id,
            data["project"],
            data["source"],
            data.get("test_mode", False),
        )

        return Response(SessionDetailSerializer(session).data, status=status.HTTP_201_CREATED)


class SessionListView(APIView):
    """GET /api/v1/sessions/ — list sessions with optional filters."""

    permission_classes = [HasSessionsReadScope]

    def get(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"detail": "No tenant found."}, status=status.HTTP_404_NOT_FOUND)

        qs = Session.objects.filter(tenant=tenant)

        # Filters
        project_identity = request.query_params.get("project_identity")
        if project_identity:
            qs = qs.filter(project_identity=project_identity)
        else:
            project = request.query_params.get("project")
            if project:
                qs = qs.filter(project=project)

        since = request.query_params.get("since")
        if since:
            qs = qs.filter(session_start__gte=since)

        include_test = request.query_params.get("include_test", "false").lower() == "true"
        if not include_test:
            qs = qs.filter(test_mode=False)

        try:
            limit = min(int(request.query_params.get("limit", 50)), 100)
        except (ValueError, TypeError):
            return Response(
                {"detail": "limit must be a positive integer."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        qs = qs[:limit]

        return Response(SessionDetailSerializer(qs, many=True).data)


class SessionDetailView(APIView):
    """GET/DELETE /api/v1/sessions/{id}/ — retrieve or delete a session."""

    permission_classes = [HasSessionsReadScope]

    def get(self, request, session_id):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"detail": "No tenant found."}, status=status.HTTP_404_NOT_FOUND)

        session = get_object_or_404(Session, id=session_id, tenant=tenant)
        return Response(SessionDetailSerializer(session).data)

    def delete(self, request, session_id):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"detail": "No tenant found."}, status=status.HTTP_404_NOT_FOUND)

        session = get_object_or_404(Session, id=session_id, tenant=tenant)
        session.delete()

        logger.info("session_deleted tenant_id=%s session_id=%s", tenant.id, session_id)
        return Response(status=status.HTTP_204_NO_CONTENT)
