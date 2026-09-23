"""Authenticated, optional Talk routing; all model failures fall back locally."""

import logging
from time import monotonic, perf_counter

from pydantic import ValidationError
from rest_framework.exceptions import ParseError
from rest_framework.parsers import JSONParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.router.talk_route import TALK_ROUTE_BUDGET_SECONDS, TalkRouteRequest, TalkRouteResponse, route_talk
from apps.tenants.throttling import _UserScopedThrottle

logger = logging.getLogger(__name__)


class TalkRouteHourThrottle(_UserScopedThrottle):
    scope = "talk_route_hour"
    rate = "600/hour"


class TalkRouteView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [TalkRouteHourThrottle]
    parser_classes = [JSONParser]

    def post(self, request):
        started = perf_counter()
        deadline = monotonic() + TALK_ROUTE_BUDGET_SECONDS
        try:
            payload = TalkRouteRequest.model_validate(request.data)
        except (ValidationError, ParseError):
            return Response({"error": "invalid_request"}, status=400)
        tenant = None
        try:
            tenant = getattr(request.user, "tenant", None)
            result = route_talk(payload, tenant, deadline=deadline)
        except Exception:
            result = TalkRouteResponse()
        result.latency_ms = max(0, round((perf_counter() - started) * 1000))
        log = logger.warning if result.reason == "unavailable" else logger.info
        log(
            "reason=%s ack_kind=%s quick_read=%s latency_ms=%d tenant_id=%s",
            result.reason,
            result.ack_kind,
            result.quick_read,
            result.latency_ms,
            str(tenant.id) if tenant is not None else None,
        )
        return Response(result.model_dump(mode="json"))
