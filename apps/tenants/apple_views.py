"""Sign in with Apple web popup endpoints."""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.db.models import Subquery
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import APIException, ParseError, UnsupportedMediaType
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.cache import bump_tag

from .apple_client import (
    AppleInvalidGrant,
    AppleUnavailable,
    apple_readiness_error,
    exchange_apple_code,
)
from .apple_models import AppleAuthTransaction
from .apple_serializers import AppleBeginSerializer, AppleCompleteSerializer, AppleLinkSerializer
from .apple_services import (
    AppleResolutionRejected,
    AppleTransactionRejected,
    consume_apple_transaction,
    enqueue_unpersisted_apple_grant,
    link_apple_identity,
    resolve_apple_auth,
)
from .models import Tenant
from .serializers import EmailTokenObtainPairSerializer
from .throttling import AppleBeginMinuteThrottle, AppleCompleteMinuteThrottle, AppleLinkMinuteThrottle

logger = logging.getLogger(__name__)


def _invalid_grant() -> Response:
    return Response({"error": "invalid_grant"}, status=status.HTTP_400_BAD_REQUEST)


class AppleNotConfigured(APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = {"error": "not_configured"}
    default_code = "not_configured"


class AppleReadinessMixin:
    """Run the shared readiness gate before auth, throttles, parsing, or DB."""

    def initial(self, request, *args, **kwargs):
        readiness_error = apple_readiness_error()
        if readiness_error is not None:
            logger.info("auth.apple.not_configured reason=%s", readiness_error)
            raise AppleNotConfigured()
        return super().initial(request, *args, **kwargs)


class AppleStrictParsingMixin:
    """Collapse parser/media errors instead of returning DRF's default body."""

    def parser_failure_response(self) -> Response:
        return _invalid_grant()

    def handle_exception(self, exc):
        if isinstance(exc, (ParseError, UnsupportedMediaType)):
            return self.parser_failure_response()
        return super().handle_exception(exc)


class AppleBeginView(AppleReadinessMixin, AppleStrictParsingMixin, APIView):
    permission_classes = [AllowAny]
    throttle_classes = [AppleBeginMinuteThrottle]

    def parser_failure_response(self) -> Response:
        return Response({"error": "invalid_request"}, status=status.HTTP_400_BAD_REQUEST)

    def post(self, request):
        serializer = AppleBeginSerializer(data=request.data)
        if not serializer.is_valid():
            return self.parser_failure_response()

        now = timezone.now()
        expired_ids = (
            AppleAuthTransaction.objects.filter(expires_at__lte=now).order_by("expires_at", "id").values("id")[:100]
        )
        AppleAuthTransaction.objects.filter(id__in=Subquery(expired_ids)).delete()

        state_value = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        row = AppleAuthTransaction.objects.create(
            state=state_value,
            nonce_hash=hashlib.sha256(nonce.encode("utf-8")).hexdigest(),
            expires_at=now + timedelta(seconds=settings.APPLE_SIWA_TRANSACTION_TTL_SECONDS),
        )
        logger.info("auth.apple.begin.success transaction_id=%s", row.id)
        return Response(
            {
                "transaction_id": str(row.id),
                "state": state_value,
                "nonce": nonce,
            },
            status=status.HTTP_200_OK,
        )


class AppleCompleteView(AppleReadinessMixin, AppleStrictParsingMixin, APIView):
    permission_classes = [AllowAny]
    throttle_classes = [AppleCompleteMinuteThrottle]

    def post(self, request):
        serializer = AppleCompleteSerializer(data=request.data)
        if not serializer.is_valid():
            logger.info("auth.apple.complete.invalid reason=malformed_request")
            return _invalid_grant()
        data = serializer.validated_data
        transaction_id = data["transaction_id"]

        try:
            nonce_hash = consume_apple_transaction(transaction_id, data["state"])
        except AppleTransactionRejected as exc:
            logger.info(
                "auth.apple.complete.invalid transaction_id=%s reason=%s",
                transaction_id,
                exc.reason,
            )
            return _invalid_grant()

        try:
            grant = exchange_apple_code(data["code"], nonce_hash)
        except AppleInvalidGrant as exc:
            logger.info(
                "auth.apple.complete.invalid transaction_id=%s reason=%s",
                transaction_id,
                exc.reason,
            )
            return _invalid_grant()
        except AppleUnavailable as exc:
            logger.warning(
                "auth.apple.complete.unavailable transaction_id=%s reason=%s",
                transaction_id,
                exc.reason,
            )
            return Response(
                {"error": "apple_unavailable"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        try:
            resolution = resolve_apple_auth(grant)
        except AppleResolutionRejected as exc:
            logger.info(
                "auth.apple.complete.invalid transaction_id=%s reason=%s",
                transaction_id,
                exc.reason,
            )
            try:
                enqueue_unpersisted_apple_grant(grant)
            except Exception:
                logger.warning(
                    "auth.apple.revocation.enqueue_failed transaction_id=%s",
                    transaction_id,
                    exc_info=True,
                )
            response_status = {
                "link_required": status.HTTP_409_CONFLICT,
                "signup_gated": status.HTTP_403_FORBIDDEN,
            }.get(exc.error, status.HTTP_400_BAD_REQUEST)
            return Response({"error": exc.error}, status=response_status)
        except Exception:
            try:
                enqueue_unpersisted_apple_grant(grant)
            except Exception:
                logger.warning(
                    "auth.apple.revocation.enqueue_failed transaction_id=%s",
                    transaction_id,
                    exc_info=True,
                )
            raise

        # Phase C committed before minting. This serializer is mandatory because
        # it carries the repo's pw_iat invalidation claim.
        refresh = EmailTokenObtainPairSerializer.get_token(resolution.user)
        logger.info(
            "auth.apple.complete.success transaction_id=%s user_id=%s created=%s",
            transaction_id,
            resolution.user.id,
            resolution.created,
        )
        return Response(
            {
                "access": str(refresh.access_token),
                "refresh": str(refresh),
                "created": resolution.created,
            },
            status=status.HTTP_200_OK,
        )


class AppleLinkView(AppleReadinessMixin, AppleStrictParsingMixin, APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [AppleLinkMinuteThrottle]

    def post(self, request):
        serializer = AppleLinkSerializer(data=request.data)
        if not serializer.is_valid():
            logger.info("auth.apple.link.invalid reason=malformed_request")
            return _invalid_grant()
        data = serializer.validated_data
        transaction_id = data["transaction_id"]

        if not request.user.check_password(data["current_password"]):
            logger.info(
                "auth.apple.link.invalid transaction_id=%s reason=step_up_failed",
                transaction_id,
            )
            return _invalid_grant()

        try:
            nonce_hash = consume_apple_transaction(transaction_id, data["state"])
        except AppleTransactionRejected as exc:
            logger.info(
                "auth.apple.link.invalid transaction_id=%s reason=%s",
                transaction_id,
                exc.reason,
            )
            return _invalid_grant()

        try:
            grant = exchange_apple_code(data["code"], nonce_hash)
        except AppleInvalidGrant as exc:
            logger.info(
                "auth.apple.link.invalid transaction_id=%s reason=%s",
                transaction_id,
                exc.reason,
            )
            return _invalid_grant()
        except AppleUnavailable as exc:
            logger.warning(
                "auth.apple.link.unavailable transaction_id=%s reason=%s",
                transaction_id,
                exc.reason,
            )
            return Response(
                {"error": "apple_unavailable"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        try:
            link_apple_identity(request.user, grant)
        except AppleResolutionRejected as exc:
            logger.info(
                "auth.apple.link.invalid transaction_id=%s reason=%s",
                transaction_id,
                exc.reason,
            )
            try:
                enqueue_unpersisted_apple_grant(grant)
            except Exception:
                logger.warning(
                    "auth.apple.revocation.enqueue_failed transaction_id=%s",
                    transaction_id,
                    exc_info=True,
                )
            response_status = (
                status.HTTP_409_CONFLICT
                if exc.error in {"already_linked", "apple_id_in_use"}
                else status.HTTP_400_BAD_REQUEST
            )
            return Response({"error": exc.error}, status=response_status)
        except Exception:
            try:
                enqueue_unpersisted_apple_grant(grant)
            except Exception:
                logger.warning(
                    "auth.apple.revocation.enqueue_failed transaction_id=%s",
                    transaction_id,
                    exc_info=True,
                )
            raise

        try:
            tenant = request.user.tenant
        except Tenant.DoesNotExist:
            pass
        else:
            bump_tag(tenant.id, "tenant")
        logger.info(
            "auth.apple.link.success transaction_id=%s user_id=%s",
            transaction_id,
            request.user.id,
        )
        return Response({"linked": True}, status=status.HTTP_200_OK)
