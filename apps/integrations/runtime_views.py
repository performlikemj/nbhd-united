"""Internal runtime Google capability endpoints."""
from __future__ import annotations

from uuid import UUID

import httpx
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.tenants.models import Tenant

from .google_api import (
    get_calendar_freebusy,
    get_gmail_message_detail,
    list_calendar_events,
    list_gmail_messages,
)
from .internal_auth import InternalAuthError, validate_internal_runtime_request
from .services import (
    IntegrationInactiveError,
    IntegrationNotConnectedError,
    IntegrationProviderConfigError,
    IntegrationRefreshError,
    IntegrationScopeError,
    IntegrationTokenDataError,
    get_valid_provider_access_token,
)


def _parse_positive_int(
    raw_value: str | None,
    *,
    default: int,
    max_value: int,
) -> int:
    if raw_value in (None, ""):
        return default

    try:
        value = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("must be an integer") from exc

    if value < 1:
        raise ValueError("must be greater than zero")

    return min(value, max_value)


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
    return None


def _parse_bool(raw_value: str | None, *, default: bool = False) -> bool:
    if raw_value in (None, ""):
        return default
    normalized = str(raw_value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("must be a boolean")


def _load_tenant_or_404(tenant_id: UUID) -> tuple[Tenant | None, Response | None]:
    tenant = Tenant.objects.filter(id=tenant_id).first()
    if tenant is None:
        return None, Response(
            {"error": "tenant_not_found"},
            status=status.HTTP_404_NOT_FOUND,
        )
    return tenant, None


def _integration_error_response(exc: Exception) -> Response:
    if isinstance(exc, IntegrationNotConnectedError):
        return Response(
            {"error": "integration_not_connected", "detail": str(exc)},
            status=status.HTTP_404_NOT_FOUND,
        )
    if isinstance(exc, IntegrationInactiveError):
        return Response(
            {"error": "integration_inactive", "detail": str(exc)},
            status=status.HTTP_409_CONFLICT,
        )
    if isinstance(exc, IntegrationTokenDataError):
        return Response(
            {"error": "integration_token_invalid", "detail": str(exc)},
            status=status.HTTP_409_CONFLICT,
        )
    if isinstance(exc, IntegrationProviderConfigError):
        return Response(
            {"error": "provider_not_configured", "detail": str(exc)},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    if isinstance(exc, IntegrationRefreshError):
        return Response(
            {"error": "integration_refresh_failed", "detail": str(exc)},
            status=status.HTTP_502_BAD_GATEWAY,
        )
    if isinstance(exc, IntegrationScopeError):
        return Response(
            {"error": "integration_scope_insufficient", "detail": str(exc)},
            status=status.HTTP_409_CONFLICT,
        )

    return Response(
        {"error": "integration_access_failed"},
        status=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )


class RuntimeGmailMessagesView(APIView):
    """Return normalized Gmail messages for a tenant runtime."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, tenant_id):
        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        try:
            max_results = _parse_positive_int(
                request.query_params.get("max_results"),
                default=5,
                max_value=10,
            )
        except ValueError as exc:
            return Response(
                {"error": "invalid_request", "detail": f"max_results {exc}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        query = request.query_params.get("q", "")

        try:
            token = get_valid_provider_access_token(tenant=tenant, provider="gmail")
            payload = list_gmail_messages(
                access_token=token.access_token,
                query=query,
                max_results=max_results,
            )
        except (
            IntegrationNotConnectedError,
            IntegrationInactiveError,
            IntegrationTokenDataError,
            IntegrationProviderConfigError,
            IntegrationRefreshError,
            IntegrationScopeError,
        ) as exc:
            return _integration_error_response(exc)
        except httpx.HTTPStatusError as exc:
            return Response(
                {
                    "error": "provider_request_failed",
                    "provider_status": (
                        exc.response.status_code if exc.response is not None else None
                    ),
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except httpx.HTTPError:
            return Response(
                {"error": "provider_request_failed"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(
            {
                "provider": "gmail",
                "tenant_id": str(tenant.id),
                **payload,
            },
            status=status.HTTP_200_OK,
        )


class RuntimeCalendarEventsView(APIView):
    """Return normalized Google Calendar events for a tenant runtime."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, tenant_id):
        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        try:
            max_results = _parse_positive_int(
                request.query_params.get("max_results"),
                default=10,
                max_value=20,
            )
        except ValueError as exc:
            return Response(
                {"error": "invalid_request", "detail": f"max_results {exc}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            token = get_valid_provider_access_token(
                tenant=tenant,
                provider="google-calendar",
            )
            payload = list_calendar_events(
                access_token=token.access_token,
                time_min=request.query_params.get("time_min"),
                time_max=request.query_params.get("time_max"),
                max_results=max_results,
            )
        except (
            IntegrationNotConnectedError,
            IntegrationInactiveError,
            IntegrationTokenDataError,
            IntegrationProviderConfigError,
            IntegrationRefreshError,
            IntegrationScopeError,
        ) as exc:
            return _integration_error_response(exc)
        except httpx.HTTPStatusError as exc:
            return Response(
                {
                    "error": "provider_request_failed",
                    "provider_status": (
                        exc.response.status_code if exc.response is not None else None
                    ),
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except httpx.HTTPError:
            return Response(
                {"error": "provider_request_failed"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(
            {
                "provider": "google-calendar",
                "tenant_id": str(tenant.id),
                **payload,
            },
            status=status.HTTP_200_OK,
        )


class RuntimeGmailMessageDetailView(APIView):
    """Return a normalized Gmail message detail payload."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, tenant_id, message_id):
        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        if not str(message_id).strip():
            return Response(
                {"error": "invalid_request", "detail": "message_id is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            include_thread = _parse_bool(
                request.query_params.get("include_thread"),
                default=True,
            )
            thread_limit = _parse_positive_int(
                request.query_params.get("thread_limit"),
                default=5,
                max_value=10,
            )
        except ValueError as exc:
            return Response(
                {"error": "invalid_request", "detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            token = get_valid_provider_access_token(tenant=tenant, provider="gmail")
            payload = get_gmail_message_detail(
                access_token=token.access_token,
                message_id=str(message_id),
                include_thread=include_thread,
                thread_limit=thread_limit,
            )
        except (
            IntegrationNotConnectedError,
            IntegrationInactiveError,
            IntegrationTokenDataError,
            IntegrationProviderConfigError,
            IntegrationRefreshError,
            IntegrationScopeError,
        ) as exc:
            return _integration_error_response(exc)
        except httpx.HTTPStatusError as exc:
            return Response(
                {
                    "error": "provider_request_failed",
                    "provider_status": (
                        exc.response.status_code if exc.response is not None else None
                    ),
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except httpx.HTTPError:
            return Response(
                {"error": "provider_request_failed"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(
            {
                "provider": "gmail",
                "tenant_id": str(tenant.id),
                **payload,
            },
            status=status.HTTP_200_OK,
        )


class RuntimeCalendarFreeBusyView(APIView):
    """Return normalized free/busy windows for the primary calendar."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, tenant_id):
        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        try:
            token = get_valid_provider_access_token(
                tenant=tenant,
                provider="google-calendar",
            )
            payload = get_calendar_freebusy(
                access_token=token.access_token,
                time_min=request.query_params.get("time_min"),
                time_max=request.query_params.get("time_max"),
            )
        except (
            IntegrationNotConnectedError,
            IntegrationInactiveError,
            IntegrationTokenDataError,
            IntegrationProviderConfigError,
            IntegrationRefreshError,
            IntegrationScopeError,
        ) as exc:
            return _integration_error_response(exc)
        except httpx.HTTPStatusError as exc:
            return Response(
                {
                    "error": "provider_request_failed",
                    "provider_status": (
                        exc.response.status_code if exc.response is not None else None
                    ),
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except httpx.HTTPError:
            return Response(
                {"error": "provider_request_failed"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(
            {
                "provider": "google-calendar",
                "tenant_id": str(tenant.id),
                **payload,
            },
            status=status.HTTP_200_OK,
        )
