"""Internal runtime capability endpoints."""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)
from datetime import date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from django.utils import timezone as tz
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.billing.services import record_usage
from apps.journal.document_views import _default_markdown, _default_title
from apps.journal.models import DailyNote, Document, JournalEntry
from apps.journal.serializers import (
    JournalEntryRuntimeSerializer,
    WeeklyReviewRuntimeSerializer,
)
from apps.journal.services import (
    get_or_seed_note_template,
    parse_daily_sections,
)
from apps.lessons.models import Lesson
from apps.lessons.serializers import LessonSerializer
from apps.lessons.services import search_lessons
from apps.orchestrator.personas import get_persona
from apps.tenants.models import Tenant

from .google_api import (
    get_calendar_freebusy,
    get_gmail_message_detail,
    list_calendar_events,
    list_gmail_messages,
)
from .internal_auth import InternalAuthError, validate_internal_runtime_request
from .models import Integration
from .services import (
    IntegrationInactiveError,
    IntegrationNotConnectedError,
    IntegrationProviderConfigError,
    IntegrationRefreshError,
    IntegrationScopeError,
    IntegrationTokenDataError,
    complete_composio_connection,
    disconnect_integration,
    execute_reddit_tool,
    get_valid_provider_access_token,
    initiate_composio_connection,
)


def _get_persona_name(tenant) -> str:
    """Get the persona display name for a tenant, defaulting to 'Neighbor'."""
    persona_key = (tenant.user.preferences or {}).get("agent_persona", "neighbor")
    return get_persona(persona_key)["identity"]["name"]


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


def _parse_non_negative_int(value, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a non-negative integer")

    if isinstance(value, int):
        if value < 0:
            raise ValueError(f"{field_name} must be a non-negative integer")
        return value

    if isinstance(value, float):
        if value < 0 or not value.is_integer():
            raise ValueError(f"{field_name} must be a non-negative integer")
        return int(value)

    if isinstance(value, str):
        value = value.strip()
        if value == "":
            raise ValueError(f"{field_name} is required")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field_name} must be a non-negative integer") from exc
        if parsed < 0:
            raise ValueError(f"{field_name} must be a non-negative integer")
        return parsed

    raise ValueError(f"{field_name} must be a non-negative integer")


def _parse_iso_timestamp(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("timestamp must be ISO format")

    timestamp = value.strip()
    if timestamp == "":
        return None

    normalized = timestamp[:-1] + "+00:00" if timestamp.endswith("Z") else timestamp
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("timestamp must be ISO format") from exc

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tz.utc)
    return parsed


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
    # Auth passed — set RLS context so tenant-scoped queries work
    from apps.tenants.middleware import set_rls_context

    set_rls_context(tenant_id=tenant_id, service_role=True)
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


def _tenant_timezone_name(tenant: Tenant) -> str:
    timezone_name = str(getattr(tenant.user, "timezone", "") or "UTC")
    try:
        ZoneInfo(timezone_name)
        return timezone_name
    except ZoneInfoNotFoundError:
        return "UTC"


def _tenant_now(tenant: Tenant) -> datetime:
    return tz.now().astimezone(ZoneInfo(_tenant_timezone_name(tenant)))


def _tenant_today(tenant: Tenant) -> date:
    return _tenant_now(tenant).date()


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
            token = get_valid_provider_access_token(tenant=tenant, provider="google")
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
                    "provider_status": (exc.response.status_code if exc.response is not None else None),
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except httpx.HTTPError:
            return Response(
                {"error": "provider_request_failed"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        from apps.pii.redactor import redact_tool_response

        payload = redact_tool_response(payload, tenant)

        return Response(
            {
                "provider": "google",
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
                provider="google",
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
                    "provider_status": (exc.response.status_code if exc.response is not None else None),
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except httpx.HTTPError:
            return Response(
                {"error": "provider_request_failed"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        from apps.pii.redactor import redact_tool_response

        payload = redact_tool_response(payload, tenant)

        return Response(
            {
                "provider": "google",
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
            token = get_valid_provider_access_token(tenant=tenant, provider="google")
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
                    "provider_status": (exc.response.status_code if exc.response is not None else None),
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except httpx.HTTPError:
            return Response(
                {"error": "provider_request_failed"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        from apps.pii.redactor import redact_tool_response

        payload = redact_tool_response(payload, tenant)

        return Response(
            {
                "provider": "google",
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
                provider="google",
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
                    "provider_status": (exc.response.status_code if exc.response is not None else None),
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
                "provider": "google",
                "tenant_id": str(tenant.id),
                **payload,
            },
            status=status.HTTP_200_OK,
        )


# ---------------------------------------------------------------------------
# Markdown-first daily note & memory runtime endpoints
# ---------------------------------------------------------------------------


class RuntimeDailyNotesView(APIView):
    """GET raw markdown daily note (agent access). Backed by Document model."""

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
            d = _parse_iso_date(request.query_params.get("date"), field_name="date") or _tenant_today(tenant)
        except ValueError as exc:
            return Response(
                {"error": "invalid_request", "detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        slug = str(d)
        doc, _created = Document.objects.get_or_create(
            tenant=tenant,
            kind="daily",
            slug=slug,
            defaults={
                "title": _default_title("daily", slug),
                "markdown": _default_markdown("daily", slug, tenant=tenant),
            },
        )
        return Response(
            {
                "tenant_id": str(tenant.id),
                "date": str(d),
                "markdown": doc.markdown,
                "sections": parse_daily_sections(doc.markdown),
            },
            status=200,
        )


class RuntimeDailyNoteAppendView(APIView):
    """POST append markdown content to a daily note (agent access). Backed by Document model."""

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
            d = _parse_iso_date(request.data.get("date"), field_name="date") or _tenant_today(tenant)
        except ValueError as exc:
            return Response(
                {"error": "invalid_request", "detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        slug = str(d)
        doc, _created = Document.objects.get_or_create(
            tenant=tenant,
            kind="daily",
            slug=slug,
            defaults={
                "title": _default_title("daily", slug),
                "markdown": _default_markdown("daily", slug, tenant=tenant),
            },
        )

        section_slug = request.data.get("section_slug")
        section_slug_str = str(section_slug).strip() if section_slug else ""

        if section_slug_str:
            # Derive heading from slug (e.g. "morning-report" → "Morning Report")
            heading = section_slug_str.replace("-", " ").title()
            marker = f"## {heading}"
            md = doc.markdown or ""
            idx = md.find(marker)
            if idx != -1:
                # Replace section content (everything between this heading and the next)
                heading_end = md.find("\n", idx)
                if heading_end == -1:
                    heading_end = len(md)
                else:
                    heading_end += 1  # include the newline
                next_heading = md.find("\n## ", heading_end)
                if next_heading == -1:
                    doc.markdown = md[:heading_end] + content + "\n"
                else:
                    doc.markdown = md[:heading_end] + content + "\n" + md[next_heading:]
            else:
                # Section heading doesn't exist yet — append it
                doc.markdown = md.rstrip() + f"\n\n{marker}\n{content}\n"
        else:
            # Quick-log append with timestamp
            now = _tenant_now(tenant)
            timestamp = now.strftime("%H:%M")
            persona_name = _get_persona_name(tenant)
            entry = f"- **{timestamp}** ({persona_name}) — {content}"
            doc.markdown = (doc.markdown or "").rstrip() + "\n\n" + entry + "\n"

        doc.save()

        return Response(
            {
                "tenant_id": str(tenant.id),
                "date": str(d),
                "markdown": doc.markdown,
                "sections": parse_daily_sections(doc.markdown),
            },
            status=status.HTTP_201_CREATED,
        )


class RuntimeUserMemoryView(APIView):
    """GET/PUT raw markdown long-term memory (agent access). Backed by Document model."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, tenant_id):
        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        doc, _created = Document.objects.get_or_create(
            tenant=tenant,
            kind="memory",
            slug="long-term",
            defaults={
                "title": _default_title("memory", "long-term"),
                "markdown": _default_markdown("memory", "long-term", tenant=tenant),
            },
        )
        return Response(
            {"tenant_id": str(tenant.id), "markdown": doc.markdown},
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
        doc, created = Document.objects.get_or_create(
            tenant=tenant,
            kind="memory",
            slug="long-term",
            defaults={
                "title": _default_title("memory", "long-term"),
                "markdown": markdown,
            },
        )
        if not created:
            doc.markdown = markdown
            doc.save()

        return Response(
            {"tenant_id": str(tenant.id), "markdown": doc.markdown},
            status=status.HTTP_200_OK,
        )


class RuntimeJournalContextView(APIView):
    """Combined context: recent daily notes, long-term memory, and backbone docs.

    Designed for agent session initialization.  The ``backbone`` key
    returns the tenant's tasks, goals, and ideas documents so the agent
    always starts a session aware of them.
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

        cutoff = (_tenant_now(tenant) - timedelta(days=days)).date()

        recent_docs = Document.objects.filter(tenant=tenant, kind="daily", slug__gte=str(cutoff)).order_by("slug")

        memory_doc = Document.objects.filter(tenant=tenant, kind="memory", slug="long-term").first()

        # Backbone docs: tasks, goals, ideas — always included so the agent
        # starts every session aware of the user's current state.
        backbone_kinds = [
            Document.Kind.TASKS,
            Document.Kind.GOAL,
            Document.Kind.IDEAS,
        ]
        backbone_docs = Document.objects.filter(
            tenant=tenant,
            kind__in=backbone_kinds,
        )
        backbone_data = {
            doc.kind: {
                "slug": doc.slug,
                "title": doc.title,
                "markdown": doc.markdown,
            }
            for doc in backbone_docs
        }

        notes_data = [
            {
                "tenant_id": str(tenant.id),
                "date": doc.slug,
                "markdown": doc.markdown,
                "sections": [],
            }
            for doc in recent_docs
        ]

        return Response(
            {
                "tenant_id": str(tenant.id),
                "recent_notes": notes_data,
                "long_term_memory": memory_doc.markdown if memory_doc else "",
                "recent_notes_count": len(notes_data),
                "days_back": days,
                "backbone": backbone_data,
            },
            status=status.HTTP_200_OK,
        )


class RuntimeLessonCreateView(APIView):
    """Create runtime lesson suggestions for a tenant."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request, tenant_id):
        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        text = str(request.data.get("text", "")).strip()
        if not text:
            return Response(
                {"error": "invalid_request", "detail": "text is required"},
                status=400,
            )

        context = str(request.data.get("context", "")).strip()
        source_type = str(request.data.get("source_type", "conversation")).strip() or "conversation"
        source_ref = str(request.data.get("source_ref", "")).strip()

        allowed_source_types = {
            "conversation",
            "journal",
            "reflection",
            "article",
            "experience",
        }
        if source_type not in allowed_source_types:
            return Response(
                {"error": "invalid_request", "detail": "invalid source_type"},
                status=400,
            )

        raw_tags = request.data.get("tags", [])
        if raw_tags is None:
            tags: list[str] = []
        elif isinstance(raw_tags, list):
            tags = [str(item).strip() for item in raw_tags if str(item).strip()]
        else:
            return Response(
                {"error": "invalid_request", "detail": "tags must be a list of strings"},
                status=400,
            )

        lesson = Lesson.objects.create(
            tenant=tenant,
            text=text,
            context=context,
            source_type=source_type,
            source_ref=source_ref,
            tags=tags,
            status="pending",
        )

        # Send approval buttons to the user's preferred channel (best-effort)
        try:
            from apps.lessons.notifications import send_lesson_approval_buttons

            send_lesson_approval_buttons(tenant, lesson)
        except Exception:
            logger.exception("runtime: failed to send lesson notification for tenant %s", str(tenant.id)[:8])

        return Response(
            {
                "tenant_id": str(tenant.id),
                "lesson": LessonSerializer(lesson).data,
            },
            status=201,
        )


class RuntimeLessonSearchView(APIView):
    """Search approved lessons for a tenant."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, tenant_id):
        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        query = str(request.query_params.get("q", "")).strip()
        if not query:
            return Response(
                {"error": "invalid_request", "detail": "q parameter required"},
                status=400,
            )

        try:
            limit = _parse_positive_int(request.query_params.get("limit"), default=10, max_value=50)
        except ValueError as exc:
            return Response(
                {"error": "invalid_request", "detail": str(exc)},
                status=400,
            )

        try:
            lessons = search_lessons(tenant=tenant, query=query, limit=limit)
        except ValueError as exc:
            return Response(
                {"error": "search_failed", "detail": str(exc)},
                status=500,
            )
        except Exception as exc:
            return Response(
                {"error": "search_failed", "detail": str(exc)},
                status=500,
            )

        payload = []
        for lesson in lessons:
            lesson_payload = LessonSerializer(lesson).data
            lesson_payload["similarity"] = float(getattr(lesson, "similarity", 0.0))
            payload.append(lesson_payload)

        return Response(
            {
                "tenant_id": str(tenant.id),
                "query": query,
                "count": len(payload),
                "results": payload,
            },
            status=200,
        )


class RuntimeLessonPendingView(APIView):
    """Get pending lessons for a tenant."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, tenant_id):
        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        lessons = Lesson.objects.filter(tenant=tenant, status="pending").order_by("-suggested_at")
        serializer = LessonSerializer(lessons, many=True)

        return Response(
            {
                "tenant_id": str(tenant.id),
                "count": len(serializer.data),
                "lessons": serializer.data,
            },
            status=200,
        )


# ---------------------------------------------------------------------------
# v2 Document runtime endpoints (unified model)
# ---------------------------------------------------------------------------


class RuntimeDocumentView(APIView):
    """GET/PUT a document by kind+slug (agent access)."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, tenant_id):
        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        kind = request.query_params.get("kind", "").strip()
        slug = request.query_params.get("slug", "").strip()

        if not kind:
            return Response(
                {"error": "invalid_request", "detail": "kind is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not slug:
            # For singleton docs, use kind as slug
            slug = kind

        # Validate daily slugs must be valid dates
        if kind == "daily":
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", slug):
                return Response(
                    {
                        "error": "invalid_request",
                        "detail": f"Daily note slug must be a date (YYYY-MM-DD), got: {slug!r}",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        doc, _created = Document.objects.get_or_create(
            tenant=tenant,
            kind=kind,
            slug=slug,
            defaults={
                "title": _default_title(kind, slug),
                "markdown": _default_markdown(kind, slug, tenant=tenant),
            },
        )

        return Response(
            {
                "tenant_id": str(tenant.id),
                "id": str(doc.id),
                "kind": doc.kind,
                "slug": doc.slug,
                "title": doc.title,
                "markdown": doc.markdown,
                "updated_at": doc.updated_at.isoformat(),
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

        kind = str(request.data.get("kind", "")).strip()
        slug = str(request.data.get("slug", "")).strip()
        markdown = str(request.data.get("markdown", ""))
        title = str(request.data.get("title", "")).strip()

        if not kind:
            return Response(
                {"error": "invalid_request", "detail": "kind is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not slug:
            slug = kind

        doc, created = Document.objects.get_or_create(
            tenant=tenant,
            kind=kind,
            slug=slug,
            defaults={
                "title": title or _default_title(kind, slug),
                "markdown": markdown,
            },
        )

        if not created:
            doc.markdown = markdown
            if title:
                doc.title = title
            doc.save()

        return Response(
            {
                "tenant_id": str(tenant.id),
                "id": str(doc.id),
                "kind": doc.kind,
                "slug": doc.slug,
                "title": doc.title,
                "markdown": doc.markdown,
                "updated_at": doc.updated_at.isoformat(),
            },
            status=status.HTTP_200_OK if not created else status.HTTP_201_CREATED,
        )


class RuntimeJournalSearchView(APIView):
    """Full-text search across all documents for a tenant."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, tenant_id):
        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        query = request.query_params.get("q", "").strip()
        kind = request.query_params.get("kind", "").strip()
        limit = min(int(request.query_params.get("limit", "20")), 50)

        if not query:
            return Response(
                {"error": "invalid_request", "detail": "q parameter required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector

        qs = Document.objects.filter(tenant=tenant)
        if kind:
            qs = qs.filter(kind=kind)

        search_vector = SearchVector("title", weight="A") + SearchVector("markdown", weight="B")
        search_query = SearchQuery(query, search_type="websearch")

        results = (
            qs.annotate(rank=SearchRank(search_vector, search_query)).filter(rank__gt=0.0).order_by("-rank")[:limit]
        )

        def _make_snippet(text: str, query_terms: str, max_len: int = 300) -> str:
            """Extract relevant snippet around first match."""
            if not text:
                return ""
            lower_text = text.lower()
            terms = [t.lower() for t in query_terms.split() if len(t) > 2]
            best_pos = 0
            for term in terms:
                pos = lower_text.find(term)
                if pos >= 0:
                    best_pos = max(0, pos - 100)
                    break
            snippet = text[best_pos : best_pos + max_len]
            if best_pos > 0:
                snippet = "..." + snippet
            if best_pos + max_len < len(text):
                snippet = snippet + "..."
            return snippet

        return Response(
            {
                "query": query,
                "count": len(results),
                "results": [
                    {
                        "kind": doc.kind,
                        "slug": doc.slug,
                        "title": doc.title,
                        "snippet": _make_snippet(doc.markdown, query),
                        "updated_at": doc.updated_at.isoformat(),
                        "rank": float(doc.rank),
                    }
                    for doc in results
                ],
            },
            status=status.HTTP_200_OK,
        )


class RuntimeUsageReportView(APIView):
    """Record token usage reported by polling-mode runtime executions."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request, tenant_id):
        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        if not isinstance(request.data, dict):
            return Response(
                {"error": "invalid_request", "detail": "invalid JSON payload"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        payload = request.data

        event_type = str(payload.get("event_type", "")).strip()
        if not event_type:
            return Response(
                {"error": "invalid_request", "detail": "event_type is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        model_used = str(payload.get("model_used", "")).strip()
        if not model_used:
            return Response(
                {"error": "invalid_request", "detail": "model_used is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            input_tokens = _parse_non_negative_int(
                payload.get("input_tokens"),
                field_name="input_tokens",
            )
            output_tokens = _parse_non_negative_int(
                payload.get("output_tokens"),
                field_name="output_tokens",
            )
        except ValueError as exc:
            return Response(
                {"error": "invalid_request", "detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            record_usage(
                tenant=tenant,
                event_type=event_type,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                model_used=model_used,
            )
        except Exception as exc:  # pragma: no cover - defensive only
            return Response(
                {"error": "usage_record_failed", "detail": str(exc)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response({"status": "ok"}, status=status.HTTP_200_OK)


# Map raw runtime "reason" tokens to user-facing copy that we can stash in
# `BYOCredential.last_error` (rendered by the rose banner in
# `BYOProviderCard`). Keep these short — the banner is one line on mobile.
_BYO_ERROR_HINT = {
    "anthropic": {
        "billing": ("Your Claude account is out of extra usage. Top up at claude.ai/settings/usage and try again."),
        "auth": ("Your Claude session expired. Reconnect to continue routing through your Anthropic account."),
        "auth_permanent": (
            "Your Claude credentials were revoked. Reconnect to continue routing through your Anthropic account."
        ),
    },
}
_BYO_REASONS_THAT_FAIL_CRED = frozenset({"billing", "auth", "auth_permanent"})


class RuntimeBYOErrorReportView(APIView):
    """Record a BYO provider error from the runtime so the UI can surface it.

    Posted by the in-container `nbhd-usage-reporter` plugin when an
    `agent_end` event reports a failed turn whose error matches a
    billing/auth signature on a BYO route. The handler flips the
    matching `BYOCredential.status` to `error` and stores a clean
    user-facing message in `last_error` — the AI Provider page already
    renders that field in a rose banner via `BYOProviderCard`.

    Idempotent and tolerant: if no matching credential exists (e.g. the
    user disconnected between the failure and the report), we record
    the event in logs and return 200 — the runtime should not retry.
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request, tenant_id):
        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        if not isinstance(request.data, dict):
            return Response(
                {"error": "invalid_request", "detail": "invalid JSON payload"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        provider = str(request.data.get("provider", "")).strip().lower()
        reason = str(request.data.get("reason", "")).strip().lower()
        message = str(request.data.get("message", "")).strip()
        model_used = str(request.data.get("model_used", "")).strip()

        if provider not in _BYO_ERROR_HINT:
            return Response(
                {"error": "invalid_request", "detail": "unknown provider"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if reason not in _BYO_REASONS_THAT_FAIL_CRED:
            # Not an actionable failure for the user (e.g. transient
            # rate-limit or overload). Log and ack so the plugin doesn't
            # retry.
            logger.info(
                "BYO error report ignored (non-actionable reason=%s) for tenant=%s provider=%s",
                reason or "unknown",
                tenant.id,
                provider,
            )
            return Response({"status": "ignored"}, status=status.HTTP_200_OK)

        from apps.byo_models.services import mark_credential_error

        hint = _BYO_ERROR_HINT[provider].get(reason) or message[:200]
        try:
            cred = mark_credential_error(
                tenant=tenant,
                provider=provider,
                last_error=hint,
            )
        except Exception as exc:  # pragma: no cover - defensive only
            return Response(
                {"error": "byo_error_record_failed", "detail": str(exc)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        if cred is None:
            logger.info(
                "BYO error report: no matching cred for tenant=%s provider=%s (reason=%s, model=%s)",
                tenant.id,
                provider,
                reason,
                model_used or "?",
            )
            return Response({"status": "no_credential"}, status=status.HTTP_200_OK)

        return Response({"status": "ok", "credential_id": str(cred.id)}, status=status.HTTP_200_OK)


class RuntimeMemorySyncView(APIView):
    """GET files dict for workspace memory sync (agent/container access).

    Returns all syncable documents as a mapping of workspace-relative paths
    to markdown content.  The caller writes them to the local filesystem so
    OpenClaw's ``memory_search`` can index them.
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

        from apps.orchestrator.memory_sync import render_memory_files

        files = render_memory_files(tenant)

        return Response(
            {
                "tenant_id": str(tenant.id),
                "files": files,
                "count": len(files),
            },
            status=status.HTTP_200_OK,
        )


class RuntimeDocumentAppendView(APIView):
    """POST append content to a document (agent access)."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request, tenant_id):
        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        kind = str(request.data.get("kind", "daily")).strip()
        slug = str(request.data.get("slug", "")).strip()
        content = str(request.data.get("content", "")).strip()

        if not content:
            return Response(
                {"error": "invalid_request", "detail": "content is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not slug:
            if kind == "daily":
                slug = str(_tenant_today(tenant))
            else:
                slug = kind

        doc, _created = Document.objects.get_or_create(
            tenant=tenant,
            kind=kind,
            slug=slug,
            defaults={
                "title": _default_title(kind, slug),
                "markdown": _default_markdown(kind, slug, tenant=tenant),
            },
        )

        time_str = _tenant_now(tenant).strftime("%H:%M")
        persona_name = _get_persona_name(tenant)
        entry_block = f"\n\n### {time_str} — {persona_name}\n{content}\n"
        doc.markdown = (doc.markdown or "").rstrip() + entry_block
        doc.save()

        return Response(
            {
                "tenant_id": str(tenant.id),
                "id": str(doc.id),
                "kind": doc.kind,
                "slug": doc.slug,
                "title": doc.title,
                "markdown": doc.markdown,
                "updated_at": doc.updated_at.isoformat(),
            },
            status=status.HTTP_201_CREATED,
        )


class RuntimeProfileUpdateView(APIView):
    """PATCH /api/v1/integrations/runtime/<tenant_id>/profile/

    Allows the agent to update user profile fields (timezone, display_name, language).
    All changes require prior user confirmation in conversation.
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    ALLOWED_FIELDS = {"timezone", "display_name", "language", "location_city", "location_lat", "location_lon"}

    def patch(self, request, tenant_id):
        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        user = tenant.user
        if user is None:
            return Response(
                {"error": "no_user", "detail": "Tenant has no associated user."},
                status=status.HTTP_404_NOT_FOUND,
            )

        data = request.data
        updated_fields = []

        # Validate timezone if provided
        if "timezone" in data:
            tz_value = (data["timezone"] or "").strip()
            if tz_value:
                try:
                    from zoneinfo import ZoneInfo

                    ZoneInfo(tz_value)  # validate
                except (KeyError, Exception):
                    return Response(
                        {"error": "invalid_timezone", "detail": f"Unknown timezone: {tz_value!r}"},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                user.timezone = tz_value
                updated_fields.append("timezone")

        if "display_name" in data:
            name = (data["display_name"] or "").strip()
            if name and len(name) <= 100:
                user.display_name = name
                updated_fields.append("display_name")

        if "language" in data:
            lang = (data["language"] or "").strip()
            if lang and len(lang) <= 10:
                user.language = lang
                updated_fields.append("language")

        if "location_city" in data:
            city = (data["location_city"] or "").strip()
            if city and len(city) <= 255:
                user.location_city = city
                updated_fields.append("location_city")

        if "location_lat" in data and "location_lon" in data:
            try:
                lat = float(data["location_lat"])
                lon = float(data["location_lon"])
                if -90 <= lat <= 90 and -180 <= lon <= 180:
                    user.location_lat = lat
                    user.location_lon = lon
                    updated_fields.extend(["location_lat", "location_lon"])
                else:
                    return Response(
                        {"error": "invalid_coordinates", "detail": "Latitude must be -90..90, longitude -180..180."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            except (ValueError, TypeError):
                return Response(
                    {"error": "invalid_coordinates", "detail": "latitude and longitude must be numbers."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        if not updated_fields:
            return Response(
                {"error": "no_changes", "detail": "No valid fields to update."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user.save(update_fields=updated_fields)

        # If timezone changed, re-seed system crons with the correct timezone.
        # Patching schedule.tz in-place via cron.update is unreliable — delete
        # each system cron and recreate it using the canonical seed definitions
        # (which now carry the user's real timezone).
        if "timezone" in updated_fields:
            try:
                from apps.cron.gateway_client import invoke_gateway_tool
                from apps.orchestrator.config_generator import build_cron_seed_jobs

                new_tz = user.timezone
                seed_jobs = build_cron_seed_jobs(tenant)
                seed_names = {j["name"] for j in seed_jobs}

                # List existing jobs and delete any that match a system cron name
                list_result = invoke_gateway_tool(tenant, "cron.list", {"includeDisabled": True})
                jobs = []
                if isinstance(list_result, dict):
                    jobs = list_result.get("jobs", [])
                elif isinstance(list_result, list):
                    jobs = list_result

                deleted = 0
                for job in jobs:
                    job_name = job.get("name", "")
                    if job_name in seed_names:
                        job_id = job.get("jobId") or job_name
                        try:
                            invoke_gateway_tool(tenant, "cron.remove", {"jobId": job_id})
                            deleted += 1
                        except Exception:
                            logger.warning(
                                "Failed to remove cron job %s during tz resync for tenant %s",
                                job_id,
                                tenant.id,
                                exc_info=True,
                            )

                # Recreate system crons with correct timezone
                created = 0
                for job in seed_jobs:
                    try:
                        invoke_gateway_tool(tenant, "cron.add", {"job": job})
                        created += 1
                    except Exception:
                        logger.warning(
                            "Failed to recreate cron job %s during tz resync for tenant %s",
                            job["name"],
                            tenant.id,
                            exc_info=True,
                        )

                logger.info(
                    "Timezone resync for tenant %s (tz=%s): deleted=%d recreated=%d",
                    tenant.id,
                    new_tz,
                    deleted,
                    created,
                )
            except Exception:
                logger.exception("Failed to resync cron timezones for tenant %s", tenant.id, exc_info=True)

        # Trigger config refresh so the agent picks up the new userTimezone
        if "timezone" in updated_fields:
            try:
                tenant.bump_pending_config()
                from apps.orchestrator.services import update_tenant_config

                update_tenant_config(str(tenant.id))
            except Exception:
                logger.exception("Failed to refresh config after profile update for tenant %s", tenant.id)

        # If location changed, trigger config refresh so weather URL updates
        if any(f in updated_fields for f in ("location_lat", "location_lon", "location_city")):
            try:
                tenant.bump_pending_config()
                from apps.orchestrator.services import update_tenant_config

                update_tenant_config(str(tenant.id))
            except Exception:
                logger.exception("Failed to refresh config after location update for tenant %s", tenant.id)

        return Response(
            {
                "tenant_id": str(tenant.id),
                "updated": updated_fields,
                "timezone": user.timezone,
                "display_name": getattr(user, "display_name", ""),
                "language": getattr(user, "language", ""),
                "location_city": getattr(user, "location_city", ""),
                "location_lat": getattr(user, "location_lat", None),
                "location_lon": getattr(user, "location_lon", None),
            }
        )


# ---------------------------------------------------------------------------
# Workspace runtime endpoints
# ---------------------------------------------------------------------------

# Workspace business logic lives in apps.journal.workspace_services so it can
# be reused by user-facing CRUD endpoints (apps/journal/workspace_views.py).
# These aliases preserve the original local names used throughout this file.
from apps.journal.workspace_services import (
    WORKSPACE_LIMIT,
)
from apps.journal.workspace_services import (
    embed_workspace_description as _embed_workspace_description,
)
from apps.journal.workspace_services import (
    ensure_default_workspace as _ensure_default_workspace,
)
from apps.journal.workspace_services import (
    generate_unique_slug as _generate_unique_slug,
)
from apps.journal.workspace_services import (
    serialize_workspace as _serialize_workspace,
)


class RuntimeWorkspaceListView(APIView):
    """List or create workspaces for a tenant.

    GET  /runtime/<tenant_id>/workspaces/        — List workspaces
    POST /runtime/<tenant_id>/workspaces/        — Create workspace {name, description}
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

        from apps.journal.models import Workspace

        workspaces = Workspace.objects.filter(tenant=tenant).order_by("-is_default", "-last_used_at", "name")
        active_id = tenant.active_workspace_id

        return Response(
            {
                "tenant_id": str(tenant.id),
                "workspaces": [_serialize_workspace(ws, active_workspace_id=active_id) for ws in workspaces],
                "active_workspace_id": str(active_id) if active_id else None,
                "limit": WORKSPACE_LIMIT,
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

        name = str(request.data.get("name", "")).strip()
        description = str(request.data.get("description", "")).strip()

        if not name:
            return Response(
                {"error": "invalid_request", "detail": "name is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(name) > 60:
            return Response(
                {"error": "invalid_request", "detail": "name must be 60 characters or less"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        from apps.journal.models import Workspace

        # Auto-create the default workspace on first creation
        is_first_create = not Workspace.objects.filter(tenant=tenant).exists()
        if is_first_create:
            _ensure_default_workspace(tenant)

        # Enforce max workspaces per tenant
        current_count = Workspace.objects.filter(tenant=tenant).count()
        if current_count >= WORKSPACE_LIMIT:
            return Response(
                {
                    "error": "workspace_limit_reached",
                    "detail": f"Maximum {WORKSPACE_LIMIT} workspaces per tenant",
                },
                status=status.HTTP_409_CONFLICT,
            )

        slug = _generate_unique_slug(tenant, name)
        workspace = Workspace.objects.create(
            tenant=tenant,
            name=name,
            slug=slug,
            description=description,
            description_embedding=_embed_workspace_description(description),
            is_default=False,
        )

        # Make the new workspace active
        tenant.active_workspace = workspace
        tenant.save(update_fields=["active_workspace"])

        return Response(
            {
                "tenant_id": str(tenant.id),
                "workspace": _serialize_workspace(workspace, active_workspace_id=workspace.id),
                "default_workspace_created": is_first_create,
            },
            status=status.HTTP_201_CREATED,
        )


class RuntimeWorkspaceDetailView(APIView):
    """Update or delete a single workspace.

    PATCH  /runtime/<tenant_id>/workspaces/<slug>/   — Update {name?, description?}
    DELETE /runtime/<tenant_id>/workspaces/<slug>/   — Delete (not default)
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    def patch(self, request, tenant_id, slug):
        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        from apps.journal.models import Workspace

        workspace = Workspace.objects.filter(tenant=tenant, slug=slug).first()
        if workspace is None:
            return Response(
                {"error": "workspace_not_found", "detail": f"No workspace with slug {slug!r}"},
                status=status.HTTP_404_NOT_FOUND,
            )

        updated_fields = []

        if "name" in request.data:
            new_name = str(request.data.get("name", "")).strip()
            if not new_name:
                return Response(
                    {"error": "invalid_request", "detail": "name cannot be empty"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if len(new_name) > 60:
                return Response(
                    {"error": "invalid_request", "detail": "name must be 60 characters or less"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            workspace.name = new_name
            updated_fields.append("name")

        if "description" in request.data:
            new_description = str(request.data.get("description", "")).strip()
            workspace.description = new_description
            workspace.description_embedding = _embed_workspace_description(new_description)
            updated_fields.extend(["description", "description_embedding"])

        if updated_fields:
            workspace.save(update_fields=updated_fields)

        return Response(
            {
                "tenant_id": str(tenant.id),
                "workspace": _serialize_workspace(workspace, active_workspace_id=tenant.active_workspace_id),
                "updated": updated_fields,
            },
            status=status.HTTP_200_OK,
        )

    def delete(self, request, tenant_id, slug):
        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        from apps.journal.models import Workspace

        workspace = Workspace.objects.filter(tenant=tenant, slug=slug).first()
        if workspace is None:
            return Response(
                {"error": "workspace_not_found", "detail": f"No workspace with slug {slug!r}"},
                status=status.HTTP_404_NOT_FOUND,
            )

        if workspace.is_default:
            return Response(
                {
                    "error": "cannot_delete_default",
                    "detail": "Cannot delete the default workspace",
                },
                status=status.HTTP_409_CONFLICT,
            )

        # If deleting the active workspace, fall back to default
        was_active = tenant.active_workspace_id == workspace.id
        if was_active:
            default_ws = Workspace.objects.filter(tenant=tenant, is_default=True).first()
            tenant.active_workspace = default_ws
            tenant.save(update_fields=["active_workspace"])

        deleted_id = str(workspace.id)
        workspace.delete()

        return Response(
            {
                "tenant_id": str(tenant.id),
                "deleted_id": deleted_id,
                "fell_back_to_default": was_active,
            },
            status=status.HTTP_200_OK,
        )


class RuntimeWorkspaceSwitchView(APIView):
    """Switch the active workspace for a tenant.

    POST /runtime/<tenant_id>/workspaces/switch/  — Body: {slug}
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request, tenant_id):
        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        slug = str(request.data.get("slug", "")).strip()
        if not slug:
            return Response(
                {"error": "invalid_request", "detail": "slug is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        from apps.journal.models import Workspace

        workspace = Workspace.objects.filter(tenant=tenant, slug=slug).first()
        if workspace is None:
            return Response(
                {"error": "workspace_not_found", "detail": f"No workspace with slug {slug!r}"},
                status=status.HTTP_404_NOT_FOUND,
            )

        previous_id = tenant.active_workspace_id
        tenant.active_workspace = workspace
        tenant.save(update_fields=["active_workspace"])

        workspace.last_used_at = tz.now()
        workspace.save(update_fields=["last_used_at"])

        return Response(
            {
                "tenant_id": str(tenant.id),
                "workspace": _serialize_workspace(workspace, active_workspace_id=workspace.id),
                "previous_workspace_id": str(previous_id) if previous_id else None,
            },
            status=status.HTTP_200_OK,
        )


# ---------------------------------------------------------------------------
# Reddit runtime endpoints
# ---------------------------------------------------------------------------


class RedditConnectView(APIView):
    """POST — initiate Composio Reddit OAuth connection."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request, tenant_id):
        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        callback_url = str(request.data.get("callback_url", "")).strip()
        if not callback_url:
            return Response(
                {"error": "invalid_request", "detail": "callback_url is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            redirect_url, connection_request_id = initiate_composio_connection(tenant, "reddit", callback_url)
        except IntegrationProviderConfigError as exc:
            return _integration_error_response(exc)
        except Exception as exc:
            logger.exception("Reddit connect failed for tenant %s", tenant_id)
            return Response(
                {"error": "connect_failed", "detail": str(exc)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(
            {
                "redirect_url": redirect_url,
                "connection_request_id": connection_request_id,
            },
            status=status.HTTP_200_OK,
        )


class RedditCompleteView(APIView):
    """POST — complete Composio Reddit OAuth connection."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request, tenant_id):
        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        connection_request_id = str(request.data.get("connection_request_id", "")).strip()
        if not connection_request_id:
            return Response(
                {"error": "invalid_request", "detail": "connection_request_id is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            integration = complete_composio_connection(tenant, "reddit", connection_request_id)
        except (IntegrationProviderConfigError, IntegrationInactiveError) as exc:
            return _integration_error_response(exc)
        except Exception as exc:
            logger.exception("Reddit complete failed for tenant %s", tenant_id)
            return Response(
                {"error": "complete_failed", "detail": str(exc)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(
            {
                "connected": True,
                "provider_email": integration.provider_email,
            },
            status=status.HTTP_200_OK,
        )


class RedditStatusView(APIView):
    """GET — check Reddit integration status for a tenant."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, tenant_id):
        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        integration = Integration.objects.filter(
            tenant=tenant,
            provider="reddit",
            status=Integration.Status.ACTIVE,
        ).first()

        connected = integration is not None
        provider_email = integration.provider_email if integration else ""

        return Response(
            {"connected": connected, "provider_email": provider_email},
            status=status.HTTP_200_OK,
        )


class RedditDisconnectView(APIView):
    """POST — disconnect Reddit integration for a tenant."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request, tenant_id):
        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        try:
            disconnect_integration(tenant, "reddit")
        except Exception as exc:
            logger.exception("Reddit disconnect failed for tenant %s", tenant_id)
            return Response(
                {"error": "disconnect_failed", "detail": str(exc)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response({"disconnected": True}, status=status.HTTP_200_OK)


class RedditToolView(APIView):
    """POST — execute a Reddit tool action via Composio."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request, tenant_id):
        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        action = str(request.data.get("action", "")).strip()
        if not action:
            return Response(
                {"error": "invalid_request", "detail": "action is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        params = {k: v for k, v in request.data.items() if k != "action"}

        try:
            result = execute_reddit_tool(tenant, action, params)
        except ValueError as exc:
            return Response(
                {"error": "invalid_action", "detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except RuntimeError as exc:
            # Tool executed but Composio returned unsuccessful — surface as 400
            # so the agent gets a readable error it can relay to the user
            return Response(
                {"error": "tool_error", "detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except IntegrationProviderConfigError as exc:
            return _integration_error_response(exc)
        except Exception as exc:
            logger.exception("Reddit tool execution failed for tenant %s action=%s", tenant_id, action)
            return Response(
                {"error": "tool_execution_failed", "detail": str(exc)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        from apps.pii.redactor import redact_tool_response

        result = redact_tool_response(result, tenant)

        return Response(result, status=status.HTTP_200_OK)
