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

from apps.journal.models import DailyNote, Document, JournalEntry, UserMemory
from apps.journal.document_views import _default_markdown, _default_title
from apps.journal.services import (
    append_log_to_note,
    get_or_seed_note_template,
    parse_daily_sections,
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
            d = _parse_iso_date(request.query_params.get("date"), field_name="date") or tz.now().date()
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
        content = doc.markdown_plaintext
        return Response(
            {
                "tenant_id": str(tenant.id),
                "date": str(d),
                "markdown": content,
                "sections": parse_daily_sections(content),
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
            d = _parse_iso_date(request.data.get("date"), field_name="date") or tz.now().date()
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

        md = doc.markdown_plaintext

        if section_slug_str:
            # Derive heading from slug (e.g. "morning-report" → "Morning Report")
            heading = section_slug_str.replace("-", " ").title()
            marker = f"## {heading}"
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
            now = tz.now()
            timestamp = now.strftime("%H:%M")
            entry = f"- **{timestamp}** (agent) — {content}"
            doc.markdown = (md or "").rstrip() + "\n\n" + entry + "\n"

        doc.save()

        markdown_out = doc.markdown_plaintext
        return Response(
            {
                "tenant_id": str(tenant.id),
                "date": str(d),
                "markdown": markdown_out,
                "sections": parse_daily_sections(markdown_out),
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
            {"tenant_id": str(tenant.id), "markdown": doc.markdown_plaintext},
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
            {"tenant_id": str(tenant.id), "markdown": doc.markdown_plaintext},
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

        recent_docs = Document.objects.filter(
            tenant=tenant, kind="daily", slug__gte=str(cutoff)
        ).order_by("slug")

        memory_doc = Document.objects.filter(
            tenant=tenant, kind="memory", slug="long-term"
        ).first()

        notes_data = [
            {
                "tenant_id": str(tenant.id),
                "date": doc.slug,
                "markdown": doc.markdown_plaintext,
                "sections": [],
            }
            for doc in recent_docs
        ]

        return Response(
            {
                "tenant_id": str(tenant.id),
                "recent_notes": notes_data,
                "long_term_memory": memory_doc.markdown_plaintext if memory_doc else "",
                "recent_notes_count": len(notes_data),
                "days_back": days,
            },
            status=status.HTTP_200_OK,
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
                "title": doc.title_plaintext,
                "markdown": doc.markdown_plaintext,
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
                "title": doc.title_plaintext,
                "markdown": doc.markdown_plaintext,
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

        base_qs = Document.objects.filter(tenant=tenant)
        if kind:
            base_qs = base_qs.filter(kind=kind)

        query_terms = [term.strip().lower() for term in query.lower().split() if term.strip()]
        results_with_rank: list[tuple[Document, float]] = []

        for doc in base_qs:
            plaintext = doc.decrypt()
            haystack = f"{plaintext['title']} {plaintext['markdown']}".lower()
            if not all(term in haystack for term in query_terms):
                continue

            rank = float(sum(haystack.count(term) for term in query_terms))
            results_with_rank.append((doc, max(rank, 1.0)))

        results_with_rank.sort(key=lambda row: row[1], reverse=True)
        limited = results_with_rank[:limit]

        results = [
            (doc, rank)
            for doc, rank in limited
        ]

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
                        "title": doc.title_plaintext,
                        "snippet": _make_snippet(doc.markdown_plaintext, query),
                        "updated_at": doc.updated_at.isoformat(),
                        "rank": rank,
                    }
                    for doc, rank in results
                ],
            },
            status=status.HTTP_200_OK,
        )


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
                slug = str(tz.now().date())
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

        time_str = tz.now().strftime("%H:%M")
        entry_block = f"\n\n### {time_str} — Agent\n{content}\n"
        current = doc.markdown_plaintext
        doc.markdown = (current or "").rstrip() + entry_block
        doc.save()

        return Response(
            {
                "tenant_id": str(tenant.id),
                "id": str(doc.id),
                "kind": doc.kind,
                "slug": doc.slug,
                "title": doc.title_plaintext,
                "markdown": doc.markdown_plaintext,
                "updated_at": doc.updated_at.isoformat(),
            },
            status=status.HTTP_201_CREATED,
        )
