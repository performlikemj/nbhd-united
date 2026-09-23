"""Authenticated, optional chat panel selection; never blocks the chat send."""

import logging
from time import monotonic, perf_counter

from pydantic import ValidationError
from rest_framework.exceptions import ParseError
from rest_framework.parsers import JSONParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.router.chat_shape import SHAPE_BUDGET_SECONDS, ChatShapeRequest, ChatShapeResponse, shape_chat
from apps.tenants.throttling import _UserScopedThrottle

logger = logging.getLogger(__name__)


class ChatShapeHourThrottle(_UserScopedThrottle):
    scope = "chat_shape_hour"
    rate = "300/hour"


class ChatShapeView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ChatShapeHourThrottle]
    parser_classes = [JSONParser]

    def post(self, request):
        started = perf_counter()
        deadline = monotonic() + SHAPE_BUDGET_SECONDS
        try:
            payload = ChatShapeRequest.model_validate(request.data)
        except (ValidationError, ParseError):
            # Validation errors contain input_value; JSON errors can echo keys.
            return Response({"error": "invalid_request"}, status=400)
        tenant = None
        try:
            tenant = getattr(request.user, "tenant", None)
            result = shape_chat(payload, tenant, deadline=deadline)
        except Exception:
            result = ChatShapeResponse(reason="unavailable")
        result.latency_ms = max(0, round((perf_counter() - started) * 1000))
        log = logger.warning if result.reason == "unavailable" else logger.info
        log(
            "reason=%s panel=%s latency_ms=%d tenant_id=%s",
            result.reason,
            result.panel,
            result.latency_ms,
            str(tenant.id) if tenant is not None else None,
        )
        return Response(result.model_dump(mode="json"))
