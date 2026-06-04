"""User-facing Journal "current status" projection endpoint."""

from __future__ import annotations

from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.tenant_tz import tenant_tz

from .document_views import _get_tenant
from .status_projection import build_journal_status


class JournalStatusView(APIView):
    """GET /api/v1/journal/status/ — live current-status projection.

    Read-only. Renders current state from the canonical typed models + the
    finance event ledger, so the journal page never displays a stale baked
    copy. See ``status_projection.build_journal_status`` for the folding.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant = _get_tenant(request.user)
        today = timezone.now().astimezone(tenant_tz(tenant)).date()
        return Response(build_journal_status(tenant, today))
