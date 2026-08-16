"""Internal endpoint for agents to report platform issues."""

from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone
from rest_framework import serializers
from rest_framework import status as http_status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.integrations.internal_auth import InternalAuthError, validate_internal_runtime_request
from apps.router.document_write_guard import record_runtime_write_activity
from apps.tenants.models import Tenant

from .models import PlatformIssueLog

logger = logging.getLogger(__name__)

RATE_LIMIT_PER_HOUR = 10
DEDUP_WINDOW_MINUTES = 60


class ReportIssueSerializer(serializers.Serializer):
    category = serializers.ChoiceField(
        choices=PlatformIssueLog.Category.choices,
        default=PlatformIssueLog.Category.OTHER,
    )
    severity = serializers.ChoiceField(
        choices=PlatformIssueLog.Severity.choices,
        default=PlatformIssueLog.Severity.LOW,
    )
    tool_name = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")
    summary = serializers.CharField(max_length=500)
    detail = serializers.CharField(required=False, allow_blank=True, default="", max_length=2000)


class PlatformIssueReportView(APIView):
    """Internal endpoint for agents to report platform issues.

    Auth: X-NBHD-Internal-Key + X-NBHD-Tenant-Id headers (same as journal tools).
    """

    authentication_classes = []
    permission_classes = []

    def post(self, request, tenant_id):
        # Auth check — same pattern as runtime endpoints
        try:
            validate_internal_runtime_request(
                provided_key=request.headers.get("X-NBHD-Internal-Key", ""),
                provided_tenant_id=request.headers.get("X-NBHD-Tenant-Id", ""),
                expected_tenant_id=str(tenant_id),
            )
        except InternalAuthError as exc:
            return Response(
                {"error": "internal_auth_failed", "detail": str(exc)},
                status=http_status.HTTP_401_UNAUTHORIZED,
            )

        # Auth passed — set RLS context so tenant-scoped queries work
        from apps.tenants.middleware import set_rls_context

        set_rls_context(tenant_id=tenant_id, service_role=True)

        tenant = Tenant.objects.filter(id=tenant_id).first()
        if tenant is None:
            return Response(
                {"error": "tenant_not_found"},
                status=http_status.HTTP_404_NOT_FOUND,
            )

        serializer = ReportIssueSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=http_status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        now = timezone.now()

        # Rate limit: max 10 reports per hour per tenant
        recent_count = PlatformIssueLog.objects.filter(
            tenant=tenant,
            created_at__gte=now - timedelta(hours=1),
        ).count()
        if recent_count >= RATE_LIMIT_PER_HOUR:
            return Response(
                {"detail": "Rate limit exceeded. Max 10 reports per hour."},
                status=http_status.HTTP_429_TOO_MANY_REQUESTS,
            )

        # Dedup: skip if same category + tool_name reported in last hour
        if data.get("tool_name"):
            existing = PlatformIssueLog.objects.filter(
                tenant=tenant,
                category=data["category"],
                tool_name=data["tool_name"],
                created_at__gte=now - timedelta(minutes=DEDUP_WINDOW_MINUTES),
            ).exists()
            if existing:
                return Response(
                    {"detail": "Duplicate issue already reported recently.", "deduplicated": True},
                    status=http_status.HTTP_200_OK,
                )

        record_runtime_write_activity(tenant)

        # Redact-at-write: these fields say "no user PII" but nothing enforces
        # it, and they're stored plaintext (admin triage searches them). Swap
        # any value the tenant map ALREADY knows for its placeholder before we
        # persist — reuse-only, no minting, so agent-authored issue text can't
        # coin junk entries into the map (there is active map-hygiene work in
        # flight). Fields stay plaintext/searchable; only known PII is masked.
        from apps.pii.redactor import redact_known_entities

        safe_summary = redact_known_entities(tenant, data["summary"])
        safe_detail = redact_known_entities(tenant, data.get("detail", ""))

        issue = PlatformIssueLog.objects.create(
            tenant=tenant,
            category=data["category"],
            severity=data["severity"],
            tool_name=data.get("tool_name", ""),
            summary=safe_summary,
            detail=safe_detail,
        )

        logger.info(
            "Platform issue reported: tenant=%s category=%s tool=%s summary=%s",
            tenant.id,
            data["category"],
            data.get("tool_name", ""),
            safe_summary[:100],
        )

        return Response(
            {"id": str(issue.id), "status": "logged"},
            status=http_status.HTTP_201_CREATED,
        )
