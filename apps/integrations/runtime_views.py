"""Internal runtime capability endpoints."""
from __future__ import annotations

from datetime import date, timedelta
from uuid import UUID

import httpx
from django.core.exceptions import ValidationError
from django.utils import timezone as tz
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.journal.md_utils import append_entry_markdown  # noqa: F401 — kept for backward compat
from apps.journal.models import DailyNote, JournalEntry, UserMemory
from apps.journal.services import (
    append_log_to_note,
    get_or_seed_note_template,
    set_daily_note_section,
    set_daily_note_sections,
    upsert_default_daily_note,
)
from apps.journal.serializers import (
    JournalEntryRuntimeSerializer,
    WeeklyReviewRuntimeSerializer,
)
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


def _parse_iso_date(raw_value: str | None, *, field_name: str) -> date | None:
    if raw_value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(raw_value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be YYYY-MM-DD") from exc


def _parse_journal_date_range(request) -> tuple[date | None, date | None]:
    date_from = _parse_iso_date(request.query_params.get("date_from"), field_name="date_from")
    date_to = _parse_iso_date(request.query_params.get("date_to"), field_name="date_to")

    if (date_from is None) != (date_to is None):
        raise ValueError("date_from and date_to must be provided together")
    if date_from is None or date_to is None:
        return None, None
    if date_from > date_to:
        raise ValueError("date_from must be on or before date_to")
    if (date_to - date_from).days > 30:
        raise ValueError("date range must be 31 days or less")
    return date_from, date_to


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


def _build_note_payload(*, tenant: Tenant, note: DailyNote, include_sections: bool = False) -> dict:
    template, sections = get_or_seed_note_template(
        tenant=tenant,
        date_value=note.date,
        markdown=note.markdown,
    )
    payload = {
        "tenant_id": str(tenant.id),
        "date": str(note.date),
        "markdown": note.markdown,
        "template_id": str(template.id),
        "template_slug": template.slug,
        "template_name": template.name,
    }
    if include_sections:
        payload["sections"] = sections
    return payload


class RuntimeJournalEntriesView(APIView):
    """Create/list runtime journal entries for a tenant."""

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
            date_from, date_to = _parse_journal_date_range(request)
        except ValueError as exc:
            return Response(
                {"error": "invalid_request", "detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        queryset = JournalEntry.objects.filter(tenant=tenant).order_by("-date", "-created_at")
        if date_from is not None and date_to is not None:
            queryset = queryset.filter(date__gte=date_from, date__lte=date_to)

        serializer = JournalEntryRuntimeSerializer(queryset, many=True)
        return Response(
            {
                "tenant_id": str(tenant.id),
                "entries": serializer.data,
                "count": len(serializer.data),
            },
            status=status.HTTP_200_OK,
        )

    def post(self, request, tenant_id):
        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        serializer = JournalEntryRuntimeSerializer(
            data=request.data,
            context={"tenant": tenant},
        )
        serializer.is_valid(raise_exception=True)
        entry = serializer.save()
        return Response(
            {
                "tenant_id": str(tenant.id),
                "entry": JournalEntryRuntimeSerializer(entry).data,
            },
            status=status.HTTP_201_CREATED,
        )


class RuntimeWeeklyReviewsView(APIView):
    """Create runtime weekly reviews for a tenant."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request, tenant_id):
        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        serializer = WeeklyReviewRuntimeSerializer(
            data=request.data,
            context={"tenant": tenant},
        )
        serializer.is_valid(raise_exception=True)
        review = serializer.save()
        return Response(
            {
                "tenant_id": str(tenant.id),
                "review": WeeklyReviewRuntimeSerializer(review).data,
            },
            status=status.HTTP_201_CREATED,
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


# ---------------------------------------------------------------------------
# Markdown-first daily note & memory runtime endpoints
# ---------------------------------------------------------------------------


class RuntimeDailyNotesView(APIView):
    """GET raw markdown daily note, POST append to daily note (agent access)."""

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
            d = _parse_iso_date(request.query_params.get("date"), field_name="date") or tz.now().date()
        except ValueError as exc:
            return Response(
                {"error": "invalid_request", "detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        note = upsert_default_daily_note(tenant=tenant, note_date=d)
        return Response(_build_note_payload(tenant=tenant, note=note, include_sections=True), status=200)


class RuntimeDailyNoteAppendView(APIView):
    """POST append markdown content to a daily note (agent access)."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request, tenant_id):
        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        content_raw = request.data.get("content")
        content = str(content_raw or "").strip()
        if not content:
            return Response(
                {"error": "invalid_request", "detail": "content is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            d = _parse_iso_date(request.data.get("date"), field_name="date") or tz.now().date()
        except ValueError as exc:
            return Response(
                {"error": "invalid_request", "detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        note = upsert_default_daily_note(tenant=tenant, note_date=d)
        section_slug = request.data.get("section_slug")
        section_slug_str = str(section_slug).strip() if section_slug else ""

        if section_slug_str:
            raw_sections = request.data.get("sections")
            if raw_sections is not None and not isinstance(raw_sections, list):
                return Response(
                    {"error": "invalid_request", "detail": "sections must be an array"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            try:
                if raw_sections:
                    payload_sections: list[dict] = []
                    for section in raw_sections:
                        if not isinstance(section, dict):
                            return Response(
                                {
                                    "error": "invalid_request",
                                    "detail": "each section must be an object",
                                },
                                status=status.HTTP_400_BAD_REQUEST,
                            )
                        payload_sections.append(
                            {
                                "slug": str(section.get("slug") or "").strip(),
                                "title": str(section.get("title") or "").strip(),
                                "content": str(section.get("content") or "").strip(),
                                "source": str(section.get("source") or "shared").strip(),
                            }
                        )
                    note = set_daily_note_sections(
                        note=note,
                        sections=payload_sections,
                        template=note.template,
                    )

                note, _ = set_daily_note_section(
                    note=note,
                    section_slug=section_slug_str,
                    content=content,
                )
            except (ValueError, ValidationError) as exc:
                return Response(
                    {"error": "invalid_request", "detail": str(exc)},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        else:
            note = append_log_to_note(
                note=note,
                content=content,
                author="agent",
            )

        return Response(
            {
                **_build_note_payload(tenant=tenant, note=note, include_sections=True),
                "tenant_id": str(tenant.id),
            },
            status=status.HTTP_201_CREATED,
        )


class RuntimeUserMemoryView(APIView):
    """GET/PUT raw markdown long-term memory (agent access)."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, tenant_id):
        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        memory = UserMemory.objects.filter(tenant=tenant).first()
        return Response(
            {
                "tenant_id": str(tenant.id),
                "markdown": memory.markdown if memory else "",
            },
            status=status.HTTP_200_OK,
        )

    def put(self, request, tenant_id):
        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        markdown = request.data.get("markdown", "")
        memory, _ = UserMemory.objects.get_or_create(tenant=tenant)
        memory.markdown = markdown
        memory.save()

        return Response(
            {
                "tenant_id": str(tenant.id),
                "markdown": memory.markdown,
            },
            status=status.HTTP_200_OK,
        )


class RuntimeJournalContextView(APIView):
    """Combined context: recent daily notes (raw md) + long-term memory (raw md).

    Designed for agent session initialization.
    """

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
            days = _parse_positive_int(
                request.query_params.get("days"),
                default=7,
                max_value=30,
            )
        except ValueError as exc:
            return Response(
                {"error": "invalid_request", "detail": f"days {exc}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        cutoff = (tz.now() - timedelta(days=days)).date()

        recent_notes = DailyNote.objects.filter(
            tenant=tenant, date__gte=cutoff
        ).order_by("date")

        memory = UserMemory.objects.filter(tenant=tenant).first()

        notes_data = [
            {
                **_build_note_payload(tenant=tenant, note=n, include_sections=True),
            }
            for n in recent_notes
        ]

        return Response(
            {
                "tenant_id": str(tenant.id),
                "recent_notes": notes_data,
                "long_term_memory": memory.markdown if memory else "",
                "recent_notes_count": len(notes_data),
                "days_back": days,
            },
            status=status.HTTP_200_OK,
        )
