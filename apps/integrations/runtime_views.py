"""Internal runtime capability endpoints."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from datetime import date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from django.core import signing
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone as tz
from pydantic import ValidationError as PydanticValidationError
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.billing.services import record_usage
from apps.common.tenant_tz import safe_zoneinfo, tenant_today, tenant_tz_name
from apps.common.windows import Window, resolve_window
from apps.integrations.content_sanitize import neutralize_remote_image_markdown
from apps.journal.document_views import _default_markdown, _default_title
from apps.journal.models import DailyNote, Document, JournalEntry
from apps.journal.serializers import (
    JournalEntryRuntimeSerializer,
    WeeklyReviewRuntimeSerializer,
)
from apps.journal.services import (
    _validate_template_sections,
    get_default_template,
    get_or_seed_note_template,
    parse_daily_sections,
    resolve_daily_section_heading,
    seed_default_templates_for_tenant,
    upsert_markdown_section,
)
from apps.journal.session_models import Session
from apps.lessons.models import Lesson
from apps.lessons.serializers import LessonSerializer
from apps.lessons.services import search_lessons
from apps.orchestrator.personas import get_persona
from apps.pii.egress import KnownValueResponseGuardMixin
from apps.router.document_write_guard import assert_write_allowed_for_document_turn
from apps.tenants.models import Tenant

from .apple_maps import search_places
from .google_api import (
    get_calendar_freebusy,
    get_gmail_message_detail,
    list_calendar_events,
    list_gmail_messages,
)
from .internal_auth import InternalAuthError, validate_internal_runtime_request
from .models import Integration, SautaiMealPlanJob, SautaiMealPlanJobStatus
from .services import (
    IntegrationAccessError,
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

logger = logging.getLogger(__name__)

_JOURNAL_EGRESS_TEXT_FIELDS = frozenset(
    {
        "title",
        "markdown",
        "description",
        "notes",
        "snippet",
        "excerpt",
        "content",
        "text",
        "query",
        "context",
        "evidence",
        "heading",
        "body",
        "template_name",
    }
)
_LIFECYCLE_EGRESS_TEXT_FIELDS = frozenset({"title", "description", "notes", "evidence"})
_LESSON_EGRESS_TEXT_FIELDS = frozenset({"title", "text", "context", "description", "snippet", "query"})
_CRON_EGRESS_TEXT_FIELDS = frozenset({"text", "description", "message", "summary", "render_block"})
_MISSION_EGRESS_TEXT_FIELDS = frozenset(
    {"title", "mission_title", "description", "commitment", "my_commitment", "next_step", "text", "display_name"}
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


_APPLE_MAPS_QUERY_MAX_CHARS = 200
_APPLE_MAPS_LANGUAGE_MAX_CHARS = 35
_APPLE_MAPS_COUNTRY_MAX_ITEMS = 5
_APPLE_MAPS_CATEGORY_MAX_ITEMS = 10
_APPLE_MAPS_CATEGORY_MAX_CHARS = 64
_APPLE_MAPS_DEFAULT_RESULT_LIMIT = 10
_APPLE_MAPS_MAX_RESULT_LIMIT = 20
_APPLE_MAPS_LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
_APPLE_MAPS_COUNTRY_RE = re.compile(r"^[A-Za-z]{2}$")
_APPLE_MAPS_CATEGORY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")


def _comma_separated_query_values(request, singular: str, plural: str) -> list[str]:
    raw_values = request.query_params.getlist(singular)
    raw_values.extend(request.query_params.getlist(plural))
    values: list[str] = []
    for raw in raw_values:
        values.extend(part.strip() for part in str(raw).split(",") if part.strip())
    return values


def _places_search_params(request) -> dict:
    query = (request.query_params.get("q") or "").strip()
    if not query:
        raise ValueError("q is required and must not be blank")
    if len(query) > _APPLE_MAPS_QUERY_MAX_CHARS:
        raise ValueError(f"q must be at most {_APPLE_MAPS_QUERY_MAX_CHARS} characters")

    raw_latitude = request.query_params.get("lat")
    raw_longitude = request.query_params.get("lon")
    if (raw_latitude is None) != (raw_longitude is None):
        raise ValueError("lat and lon must be provided together")
    latitude = longitude = None
    if raw_latitude is not None:
        try:
            latitude = float(raw_latitude)
            longitude = float(raw_longitude)
        except (TypeError, ValueError) as exc:
            raise ValueError("lat and lon must be numbers") from exc
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise ValueError("lat or lon is out of range")

    language = (request.query_params.get("lang") or "").strip()
    if len(language) > _APPLE_MAPS_LANGUAGE_MAX_CHARS or (language and not _APPLE_MAPS_LANGUAGE_RE.fullmatch(language)):
        raise ValueError("lang must be a bounded BCP 47 language tag")

    countries = _comma_separated_query_values(request, "country", "countries")
    if len(countries) > _APPLE_MAPS_COUNTRY_MAX_ITEMS:
        raise ValueError(f"country accepts at most {_APPLE_MAPS_COUNTRY_MAX_ITEMS} values")
    if any(not _APPLE_MAPS_COUNTRY_RE.fullmatch(country) for country in countries):
        raise ValueError("country values must be two-letter codes")
    countries = [country.upper() for country in countries]

    categories = _comma_separated_query_values(request, "category", "categories")
    if len(categories) > _APPLE_MAPS_CATEGORY_MAX_ITEMS:
        raise ValueError(f"categories accepts at most {_APPLE_MAPS_CATEGORY_MAX_ITEMS} values")
    if any(
        len(category) > _APPLE_MAPS_CATEGORY_MAX_CHARS or not _APPLE_MAPS_CATEGORY_RE.fullmatch(category)
        for category in categories
    ):
        raise ValueError("category values must be bounded Apple POI category names")

    raw_limit = request.query_params.get("limit")
    if raw_limit in (None, ""):
        limit = _APPLE_MAPS_DEFAULT_RESULT_LIMIT
    else:
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError) as exc:
            raise ValueError("limit must be an integer") from exc
        if not 1 <= limit <= _APPLE_MAPS_MAX_RESULT_LIMIT:
            raise ValueError(f"limit must be between 1 and {_APPLE_MAPS_MAX_RESULT_LIMIT}")

    return {
        "query": query,
        "latitude": latitude,
        "longitude": longitude,
        "language": language,
        "countries": countries,
        "categories": categories,
        "limit": limit,
    }


class RuntimePlacesSearchView(APIView):
    """Internal-auth Apple Maps search that exposes normalized places only."""

    permission_classes = [AllowAny]

    def get(self, request, tenant_id):
        if _internal_auth_or_401(request, tenant_id):
            return Response({"error": "internal_auth_failed"}, status=status.HTTP_401_UNAUTHORIZED)
        try:
            params = _places_search_params(request)
        except ValueError as exc:
            return Response(
                {"error": "invalid_request", "detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        envelope = search_places(tenant_id=str(tenant_id), **params)
        response = Response(dict(envelope), status=envelope.http_status)
        if envelope.retry_after:
            response["Retry-After"] = envelope.retry_after
        return response


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
    """Thin wrapper kept for callers in this module — use ``apps.common.tenant_tz``."""
    return tenant_tz_name(tenant)


def _tenant_now(tenant: Tenant) -> datetime:
    return tz.now().astimezone(safe_zoneinfo(tenant_tz_name(tenant)))


def _tenant_today(tenant: Tenant) -> date:
    return _tenant_now(tenant).date()


def _resolve_sautai_week_start(data, tenant: Tenant, *, propose_if_omitted: bool = False) -> date:
    """Resolve an explicit date, symbolic week, or safe generation proposal.

    sautai's generate endpoint defaults an omitted ``week_start`` to ITS OWN
    server timezone. Generation callers instead opt into a tenant-local proposal:
    the current Monday on weekdays, or next Monday on weekends.
    """
    explicit = _parse_iso_date(data.get("week_start"), field_name="week_start")
    if explicit is not None:
        # Explicit calendar dates retain the existing backward-to-Monday rule.
        return explicit - timedelta(days=explicit.weekday())

    raw_week = data.get("week")
    week = str(raw_week or "current").strip()
    if week not in {"current", "next"}:
        raise ValueError('week must be "current" or "next"')

    today = tenant_today(tenant)
    current = today - timedelta(days=today.weekday())
    if propose_if_omitted and raw_week in (None, "") and today.weekday() >= 5:
        return current + timedelta(days=7)
    return current + timedelta(days=7) if week == "next" else current


def _sautai_generation_in_progress(tenant: Tenant, week_start: date) -> dict | None:
    """Describe a recent generation for the target week, if one is active."""
    now = tz.now()
    job = (
        SautaiMealPlanJob.objects.filter(
            tenant=tenant,
            week_start=week_start,
            status__in=[SautaiMealPlanJobStatus.PENDING, SautaiMealPlanJobStatus.GENERATING],
            created_at__gte=now - timedelta(minutes=15),
        )
        .order_by("-created_at")
        .first()
    )
    if job is None:
        return None
    return {
        "week_start": week_start.isoformat(),
        "seconds_since_started": max(0, int((now - job.created_at).total_seconds())),
    }


def _resolve_calendar_window(request, tenant: Tenant) -> tuple[str | None, str | None] | Response:
    """Resolve query-string window params to RFC3339 ``time_min``/``time_max``.

    Accepts two shapes:

      • ``?window_kind=<enum>[&window_value=<v>]`` — preferred. The window
        resolves server-side via ``apps.common.windows.resolve_window`` in
        the tenant's tz. The agent never does the date math.
      • ``?time_min=<rfc3339>&time_max=<rfc3339>`` — legacy. Passed through.

    Returns the resolved ``(time_min, time_max)`` pair, or a 400 ``Response``
    when both shapes are supplied or the window is invalid.
    """
    qp = request.query_params
    window_kind = (qp.get("window_kind") or "").strip()
    window_value_raw = qp.get("window_value")
    time_min = qp.get("time_min")
    time_max = qp.get("time_max")

    if not window_kind:
        return (time_min, time_max)

    if time_min or time_max:
        return Response(
            {
                "error": "invalid_request",
                "detail": "window_kind cannot be combined with time_min/time_max",
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        window_obj = _build_window(window_kind, window_value_raw)
    except (PydanticValidationError, ValueError) as exc:
        return Response(
            {"error": "invalid_window", "detail": str(exc)},
            status=status.HTTP_400_BAD_REQUEST,
        )

    tz_name = tenant_tz_name(tenant)
    resolved = resolve_window(window_obj, tz_name)
    if resolved is None:
        # kind='all' — let Google return everything; emit no time bounds.
        return (None, None)

    zone = safe_zoneinfo(tz_name)
    from_dt = datetime.combine(resolved[0], datetime.min.time()).replace(tzinfo=zone)
    to_dt = datetime.combine(resolved[1], datetime.max.time().replace(microsecond=0)).replace(tzinfo=zone)
    return (from_dt.isoformat(), to_dt.isoformat())


def _build_window(kind: str, value_raw: str | None) -> Window:
    """Construct a ``Window`` from flat query-string parts."""
    if kind in {
        "today",
        "yesterday",
        "tomorrow",
        "all",
        "this_week",
        "last_week",
        "month_to_date",
        "last_month",
        "year_to_date",
        "last_year",
    }:
        return Window(kind=kind)  # type: ignore[arg-type]
    if kind in {"last_n_days", "next_n_days", "last_n_weeks", "last_n_months"}:
        if value_raw is None or value_raw == "":
            raise ValueError(f"window_kind={kind!r} requires window_value=<int>")
        return Window(kind=kind, value=int(value_raw))  # type: ignore[arg-type]
    if kind == "since":
        if not value_raw:
            raise ValueError("window_kind='since' requires window_value=YYYY-MM-DD")
        return Window(kind=kind, value=date.fromisoformat(value_raw))  # type: ignore[arg-type]
    if kind == "between":
        if not value_raw or "," not in value_raw:
            raise ValueError("window_kind='between' requires window_value='YYYY-MM-DD,YYYY-MM-DD'")
        a, b = [s.strip() for s in value_raw.split(",", 1)]
        return Window(kind=kind, value=[date.fromisoformat(a), date.fromisoformat(b)])  # type: ignore[arg-type]
    raise ValueError(f"unknown window_kind={kind!r}")


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


def _integration_error_response(exc: Exception, *, provider: str, tenant: Tenant) -> Response:
    """Map an integration failure to a runtime error payload.

    ``user_action`` is the line the model relays to the user, so it MUST name
    the provider that actually failed. This helper is shared by the Google
    (Gmail/Calendar) views and the Composio-backed Reddit views, so ``provider``
    is a required keyword: an omitted or wrong value sends a user whose Reddit
    call failed off to reconnect Google. Pass the human-facing display name
    ("Google", "Reddit") — it is interpolated into user-facing prose.

    Wrapped provider HTTP failures enrich the existing integration payload with
    redacted provider-authored context. ``tenant`` is required so no caller can
    silently bypass that PII chokepoint.
    """
    if isinstance(exc, IntegrationNotConnectedError):
        payload = {
            "error": "integration_not_connected",
            "detail": str(exc),
            "user_action": (
                f"Tell the user their {provider} account is not connected. They can connect it "
                f"in the NBHD app under Settings → Integrations → {provider}."
            ),
        }
        status_code = status.HTTP_404_NOT_FOUND
    elif isinstance(exc, IntegrationInactiveError):
        payload = {
            "error": "integration_inactive",
            "detail": str(exc),
            "user_action": (
                f"Tell the user their {provider} connection is inactive. Reconnecting {provider} "
                "under Settings → Integrations should restore it."
            ),
        }
        status_code = status.HTTP_409_CONFLICT
    elif isinstance(exc, IntegrationTokenDataError):
        payload = {
            "error": "integration_token_invalid",
            "detail": str(exc),
            "user_action": (
                f"Tell the user their {provider} connection is broken and they should reconnect "
                f"{provider} under Settings → Integrations."
            ),
        }
        status_code = status.HTTP_409_CONFLICT
    elif isinstance(exc, IntegrationProviderConfigError):
        payload = {
            "error": "provider_not_configured",
            "detail": str(exc),
            "user_action": (
                f"Tell the user {provider} integration is temporarily unavailable on the server "
                "side; there is nothing to fix on their end."
            ),
        }
        status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    elif isinstance(exc, IntegrationRefreshError):
        payload = {
            "error": "integration_refresh_failed",
            "detail": str(exc),
            "user_action": (
                f"Tell the user their {provider} connection has expired and they need to reconnect "
                f"{provider} under Settings → Integrations."
            ),
        }
        status_code = status.HTTP_502_BAD_GATEWAY
    elif isinstance(exc, IntegrationScopeError):
        payload = {
            "error": "integration_scope_insufficient",
            "detail": str(exc),
            "user_action": (
                f"Tell the user to reconnect {provider} under Settings → Integrations and grant "
                "all requested permissions."
            ),
        }
        status_code = status.HTTP_409_CONFLICT
    else:
        payload = {
            "error": "integration_access_failed",
            "detail": str(exc),
            "user_action": (
                f"Tell the user their {provider} integration failed unexpectedly. They can retry, "
                f"and reconnect {provider} under Settings → Integrations if the problem continues."
            ),
        }
        status_code = status.HTTP_500_INTERNAL_SERVER_ERROR

    cause = exc.__cause__
    seen_causes: set[int] = set()
    for _ in range(5):
        if cause is None or id(cause) in seen_causes:
            break
        seen_causes.add(id(cause))
        if isinstance(cause, httpx.HTTPStatusError):
            provider_response = getattr(cause, "response", None)
            if provider_response is not None:
                payload.update(_provider_error_enrichment(provider_response, tenant))
                break
        cause = cause.__cause__

    return Response(payload, status=status_code)


_PROVIDER_MESSAGE_MAX_CHARS = 300


def _provider_error_text(value: object, *, limit: int | None = None) -> str:
    """Return a trimmed string (optionally truncated); '' for anything non-string."""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if limit is not None and len(text) > limit:
        return text[:limit]
    return text


def _provider_error_enrichment(response, tenant: Tenant) -> dict:
    """Best-effort provider reason/message extracted from an error body.

    Enrichment must never turn an upstream failure into a new error. The two
    free-text fields are provider-authored prose about the user's mailbox or
    calendar, so they pass through ``redact_tool_response`` — the same PII
    chokepoint used by these views' success payloads. Machine values are not
    redacted.
    """
    try:
        body = response.json() if response is not None else None
        if not isinstance(body, dict):
            return {}

        error = body.get("error")
        reason = ""
        message = ""
        if isinstance(error, dict):
            # Google API shape: {"error": {"code", "message", "status", "errors": [...]}}
            reason = _provider_error_text(error.get("status"), limit=_PROVIDER_MESSAGE_MAX_CHARS)
            if not reason:
                errors = error.get("errors")
                if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                    reason = _provider_error_text(errors[0].get("reason"), limit=_PROVIDER_MESSAGE_MAX_CHARS)
            message = _provider_error_text(
                error.get("message"),
                limit=_PROVIDER_MESSAGE_MAX_CHARS,
            )
        elif isinstance(error, str):
            # OAuth/token shape: {"error": "invalid_grant", "error_description": "..."}
            reason = _provider_error_text(error, limit=_PROVIDER_MESSAGE_MAX_CHARS)
            message = _provider_error_text(
                body.get("error_description"),
                limit=_PROVIDER_MESSAGE_MAX_CHARS,
            )

        enriched = {}
        if reason:
            enriched["provider_reason"] = reason
        if message:
            enriched["provider_message"] = message
        if not enriched:
            return {}

        from apps.pii.redactor import redact_tool_response

        return redact_tool_response(enriched, tenant)
    except Exception:  # noqa: BLE001 - enrichment must never mask the provider failure
        logger.debug("provider error body enrichment failed", exc_info=True)
        return {}


def _provider_error_response(exc: httpx.HTTPStatusError, tenant: Tenant) -> Response:
    """Map a provider HTTP failure to 502, passing through what the provider said.

    ``provider_status`` alone is not actionable: the model needs the provider's
    own reason/message to decide whether to retry, re-scope, or tell the user to
    reconnect. Enrichment is strictly best effort — a provider body that is
    missing, non-JSON, or shaped unexpectedly must never turn an upstream
    failure into an error here.

    The enriched fields are provider-authored prose about the user's mailbox or
    calendar (a Google 403 routinely echoes the mailbox address), so they go
    through ``redact_tool_response`` — the same PII chokepoint every one of
    these views' SUCCESS payloads passes through. Returning early from an error
    branch must not be a way around it. Only the two free-text fields are
    redacted; ``error`` and ``provider_status`` are our own machine values.
    """
    provider_response = getattr(exc, "response", None)
    payload = {
        "error": "provider_request_failed",
        "provider_status": (provider_response.status_code if provider_response is not None else None),
    }

    payload.update(_provider_error_enrichment(provider_response, tenant))

    return Response(payload, status=status.HTTP_502_BAD_GATEWAY)


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


class RuntimeJournalEntriesView(KnownValueResponseGuardMixin, APIView):
    """Create/list runtime journal entries for a tenant."""

    permission_classes = [AllowAny]
    authentication_classes = []
    pii_egress_seam = "journal_entries_runtime_response"
    pii_egress_text_fields = _JOURNAL_EGRESS_TEXT_FIELDS

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

        blocked = assert_write_allowed_for_document_turn(tenant)
        if blocked is not None:
            return blocked

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


# ── Typed Goal/Task lifecycle (feat/journal-typed-lifecycle) ──────────────
#
# These endpoints back the nbhd_goal_*/nbhd_task_* tools in nbhd-journal-tools.
# Imports of Goal/Task/GoalSerializer/TaskSerializer are intentionally LOCAL
# inside each method — the lint-on-Edit hook reaps module-level imports that
# look unused at parse time. See ``feedback_local_reimport_pattern.md``.


def _author_runtime_lifecycle_input(tenant, data, *, seam, receipts=None, include_defaults=False):
    from apps.pii.authoring import author_text

    out = data.dict() if hasattr(data, "dict") else dict(data)
    if include_defaults:
        out.setdefault("description", "")
    next_receipts = dict(receipts or {})
    for field in ("title", "description"):
        if field not in out or not isinstance(out[field], str):
            continue
        authored = author_text(tenant, out[field], seam=seam, writer="runtime", field=field)
        out[field] = authored.text
        next_receipts[field] = authored.receipt
    return out, next_receipts


def _reauthor_runtime_lifecycle_instance(instance, *, seam):
    from apps.pii.authoring import author_text

    receipts = dict(instance.pii_receipts or {})
    # Status transitions (complete/skip/defer/achieve/abandon) don't touch the
    # text, so a field that already carries a receipt and still matches the row
    # on disk has nothing to re-learn — skipping it keeps a full NER inference
    # off every transition. Comparing against the STORED value (not just the
    # receipt's existence) means a caller that edited the instance in memory
    # first still gets authored, rather than a receipt that no longer describes
    # the text.
    stored = type(instance)._default_manager.filter(pk=instance.pk).values("title", "description").first() or {}
    changed_fields = []
    for field in ("title", "description"):
        if receipts.get(field) and stored.get(field) == getattr(instance, field):
            continue
        authored = author_text(instance.tenant, getattr(instance, field), seam=seam, writer="runtime", field=field)
        if authored.text != getattr(instance, field):
            setattr(instance, field, authored.text)
            changed_fields.append(field)
        if receipts.get(field) != authored.receipt:
            receipts[field] = authored.receipt
    if receipts != (instance.pii_receipts or {}):
        instance.pii_receipts = receipts
        changed_fields.append("pii_receipts")
    if changed_fields:
        instance.save(update_fields=changed_fields)


class RuntimeGoalListCreateView(KnownValueResponseGuardMixin, APIView):
    """List or create goals for a tenant runtime."""

    permission_classes = [AllowAny]
    authentication_classes = []
    pii_egress_seam = "goal_list_create_runtime_response"
    pii_egress_text_fields = _LIFECYCLE_EGRESS_TEXT_FIELDS

    def get(self, request, tenant_id):
        from apps.journal.lifecycle_serializers import GoalSerializer
        from apps.journal.models import Goal

        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        qs = Goal.objects.filter(tenant=tenant)
        status_filter = request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)
        pillar_filter = request.query_params.get("pillar")
        if pillar_filter:
            qs = qs.filter(pillar=pillar_filter)
        parent_filter = request.query_params.get("parent_goal_id")
        if parent_filter:
            qs = qs.filter(parent_goal_id=parent_filter)

        serializer = GoalSerializer(qs, many=True)
        return Response(
            {
                "tenant_id": str(tenant.id),
                "goals": serializer.data,
                "count": len(serializer.data),
            },
            status=status.HTTP_200_OK,
        )

    def post(self, request, tenant_id):
        from django.utils import timezone

        from apps.journal.dedup import find_duplicate_goal
        from apps.journal.lifecycle_serializers import GoalSerializer

        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        blocked = assert_write_allowed_for_document_turn(tenant)
        if blocked is not None:
            return blocked

        authored_data, receipts = _author_runtime_lifecycle_input(
            tenant,
            request.data,
            seam="journal.runtime.goal.create",
            include_defaults=True,
        )
        serializer = GoalSerializer(data=authored_data, context={"tenant": tenant})
        serializer.is_valid(raise_exception=True)

        # Idempotency backstop — see RuntimeTaskListCreateView.post. Prevents a
        # cron maintenance turn from minting a second copy of a goal it already
        # has (active, or recently achieved/abandoned).
        proposed_title = (serializer.validated_data.get("title") or "").strip()
        existing = find_duplicate_goal(tenant, proposed_title, now=timezone.now())
        if existing is not None:
            return Response(
                {
                    "tenant_id": str(tenant.id),
                    "goal": GoalSerializer(existing).data,
                    "deduped": True,
                },
                status=status.HTTP_200_OK,
            )

        goal = serializer.save(pii_receipts=receipts)
        return Response(
            {"tenant_id": str(tenant.id), "goal": GoalSerializer(goal).data},
            status=status.HTTP_201_CREATED,
        )


class RuntimeGoalDetailView(APIView):
    """Get or update a single goal."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, tenant_id, goal_id):
        from apps.journal.lifecycle_serializers import GoalSerializer
        from apps.journal.models import Goal

        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        goal = Goal.objects.filter(tenant=tenant, id=goal_id).first()
        if goal is None:
            return Response({"error": "goal_not_found"}, status=status.HTTP_404_NOT_FOUND)

        return Response(
            {"tenant_id": str(tenant.id), "goal": GoalSerializer(goal).data},
            status=status.HTTP_200_OK,
        )

    def patch(self, request, tenant_id, goal_id):
        from apps.journal.lifecycle_serializers import GoalSerializer
        from apps.journal.models import Goal

        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        goal = Goal.objects.filter(tenant=tenant, id=goal_id).first()
        if goal is None:
            return Response({"error": "goal_not_found"}, status=status.HTTP_404_NOT_FOUND)

        authored_data, receipts = _author_runtime_lifecycle_input(
            tenant,
            request.data,
            seam="journal.runtime.goal.patch",
            receipts=goal.pii_receipts,
        )
        serializer = GoalSerializer(goal, data=authored_data, partial=True, context={"tenant": tenant})
        serializer.is_valid(raise_exception=True)
        serializer.save(pii_receipts=receipts)
        return Response(
            {"tenant_id": str(tenant.id), "goal": GoalSerializer(goal).data},
            status=status.HTTP_200_OK,
        )


class RuntimeGoalAchieveView(APIView):
    """Mark a goal as achieved."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request, tenant_id, goal_id):
        from apps.journal.lifecycle_serializers import GoalSerializer
        from apps.journal.models import Goal

        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        goal = Goal.objects.filter(tenant=tenant, id=goal_id).first()
        if goal is None:
            return Response({"error": "goal_not_found"}, status=status.HTTP_404_NOT_FOUND)

        _reauthor_runtime_lifecycle_instance(goal, seam="journal.runtime.goal.achieve")
        goal.mark_achieved()
        return Response(
            {"tenant_id": str(tenant.id), "goal": GoalSerializer(goal).data},
            status=status.HTTP_200_OK,
        )


class RuntimeGoalAbandonView(APIView):
    """Mark a goal as abandoned."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request, tenant_id, goal_id):
        from apps.journal.lifecycle_serializers import GoalSerializer
        from apps.journal.models import Goal

        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        goal = Goal.objects.filter(tenant=tenant, id=goal_id).first()
        if goal is None:
            return Response({"error": "goal_not_found"}, status=status.HTTP_404_NOT_FOUND)

        _reauthor_runtime_lifecycle_instance(goal, seam="journal.runtime.goal.abandon")
        goal.abandon()
        return Response(
            {"tenant_id": str(tenant.id), "goal": GoalSerializer(goal).data},
            status=status.HTTP_200_OK,
        )


class RuntimeCurrentStatusView(APIView):
    """GET the Journal current-status projection for cron/proactive grounding.

    Returns the same as-of-now snapshot the web journal page uses
    (``apps.journal.status_projection.build_journal_status``): open tasks,
    active goals, and recurring finance obligations folded from the ledger.
    Exposed to the runtime so scheduled/proactive turns emit from live state
    instead of carried-forward daily-note narration — the fix for the
    stale-nag class in ``docs/grounding/cron-stale-status-grounding.md``.
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, tenant_id):
        from apps.journal.status_projection import build_journal_status

        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        data = build_journal_status(tenant, _tenant_today(tenant))
        return Response(
            {"tenant_id": str(tenant.id), **data},
            status=status.HTTP_200_OK,
        )


class RuntimeTaskListCreateView(KnownValueResponseGuardMixin, APIView):
    """List or create tasks for a tenant runtime."""

    permission_classes = [AllowAny]
    authentication_classes = []
    pii_egress_seam = "task_list_create_runtime_response"
    pii_egress_text_fields = _LIFECYCLE_EGRESS_TEXT_FIELDS

    def get(self, request, tenant_id):
        from apps.journal.lifecycle_serializers import TaskSerializer
        from apps.journal.models import Task

        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        qs = Task.objects.filter(tenant=tenant)
        for field in ("status", "pillar"):
            value = request.query_params.get(field)
            if value:
                qs = qs.filter(**{field: value})
        parent_filter = request.query_params.get("parent_goal_id")
        if parent_filter:
            qs = qs.filter(parent_goal_id=parent_filter)
        try:
            due_before = _parse_iso_date(request.query_params.get("due_before"), field_name="due_before")
            due_after = _parse_iso_date(request.query_params.get("due_after"), field_name="due_after")
        except ValueError as exc:
            return Response(
                {"error": "invalid_request", "detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if due_before is not None:
            qs = qs.filter(due_date__lte=due_before)
        if due_after is not None:
            qs = qs.filter(due_date__gte=due_after)

        serializer = TaskSerializer(qs, many=True)
        return Response(
            {
                "tenant_id": str(tenant.id),
                "tasks": serializer.data,
                "count": len(serializer.data),
            },
            status=status.HTTP_200_OK,
        )

    def post(self, request, tenant_id):
        from django.utils import timezone

        from apps.journal.dedup import find_duplicate_task
        from apps.journal.lifecycle_serializers import TaskSerializer

        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        blocked = assert_write_allowed_for_document_turn(tenant)
        if blocked is not None:
            return blocked

        authored_data, receipts = _author_runtime_lifecycle_input(
            tenant,
            request.data,
            seam="journal.runtime.task.create",
            include_defaults=True,
        )
        serializer = TaskSerializer(data=authored_data, context={"tenant": tenant})
        serializer.is_valid(raise_exception=True)

        # Idempotency backstop (agent path only — the UI uses a different,
        # authenticated endpoint). A maintenance/cron turn re-derives tasks
        # from journal prose using only the *open* task list for dedup, so a
        # task the user completed earlier the same day is invisible to it and
        # gets recreated as a fresh open duplicate. If the proposed title
        # matches an existing task — open, or recently closed — return that
        # row instead of inserting a duplicate. See apps/journal/dedup.py.
        proposed_title = (serializer.validated_data.get("title") or "").strip()
        existing = find_duplicate_task(tenant, proposed_title, now=timezone.now())
        if existing is not None:
            return Response(
                {
                    "tenant_id": str(tenant.id),
                    "task": TaskSerializer(existing).data,
                    "deduped": True,
                },
                status=status.HTTP_200_OK,
            )

        task = serializer.save(pii_receipts=receipts)
        return Response(
            {"tenant_id": str(tenant.id), "task": TaskSerializer(task).data},
            status=status.HTTP_201_CREATED,
        )


class RuntimeTaskDetailView(APIView):
    """Get or update a single task."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, tenant_id, task_id):
        from apps.journal.lifecycle_serializers import TaskSerializer
        from apps.journal.models import Task

        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        task = Task.objects.filter(tenant=tenant, id=task_id).first()
        if task is None:
            return Response({"error": "task_not_found"}, status=status.HTTP_404_NOT_FOUND)

        return Response(
            {"tenant_id": str(tenant.id), "task": TaskSerializer(task).data},
            status=status.HTTP_200_OK,
        )

    def patch(self, request, tenant_id, task_id):
        from apps.journal.lifecycle_serializers import TaskSerializer
        from apps.journal.models import Task

        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        task = Task.objects.filter(tenant=tenant, id=task_id).first()
        if task is None:
            return Response({"error": "task_not_found"}, status=status.HTTP_404_NOT_FOUND)

        authored_data, receipts = _author_runtime_lifecycle_input(
            tenant,
            request.data,
            seam="journal.runtime.task.patch",
            receipts=task.pii_receipts,
        )
        serializer = TaskSerializer(task, data=authored_data, partial=True, context={"tenant": tenant})
        serializer.is_valid(raise_exception=True)
        serializer.save(pii_receipts=receipts)
        return Response(
            {"tenant_id": str(tenant.id), "task": TaskSerializer(task).data},
            status=status.HTTP_200_OK,
        )


class _RuntimeTaskTransitionView(APIView):
    """Base for task status transitions (complete/skip/defer)."""

    permission_classes = [AllowAny]
    authentication_classes = []
    # Subclass sets ``transition_method`` to a method name on Task.
    transition_method: str = ""

    def post(self, request, tenant_id, task_id):
        from apps.journal.lifecycle_serializers import TaskSerializer
        from apps.journal.models import Task

        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        task = Task.objects.filter(tenant=tenant, id=task_id).first()
        if task is None:
            return Response({"error": "task_not_found"}, status=status.HTTP_404_NOT_FOUND)

        _reauthor_runtime_lifecycle_instance(
            task,
            seam=f"journal.runtime.task.{self.transition_method}",
        )
        getattr(task, self.transition_method)()
        return Response(
            {"tenant_id": str(tenant.id), "task": TaskSerializer(task).data},
            status=status.HTTP_200_OK,
        )


class RuntimeTaskCompleteView(_RuntimeTaskTransitionView):
    """Mark a task as done (sets status=done, completed_at=now)."""

    transition_method = "complete"


class RuntimeTaskSkipView(_RuntimeTaskTransitionView):
    """Mark a task as skipped."""

    transition_method = "skip"


class RuntimeTaskDeferView(_RuntimeTaskTransitionView):
    """Mark a task as deferred."""

    transition_method = "defer"


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
        except IntegrationAccessError as exc:
            return _integration_error_response(exc, provider="Google", tenant=tenant)
        except httpx.HTTPStatusError as exc:
            return _provider_error_response(exc, tenant)
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

        resolved = _resolve_calendar_window(request, tenant)
        if isinstance(resolved, Response):
            return resolved
        time_min, time_max = resolved

        try:
            token = get_valid_provider_access_token(
                tenant=tenant,
                provider="google",
            )
            payload = list_calendar_events(
                access_token=token.access_token,
                time_min=time_min,
                time_max=time_max,
                max_results=max_results,
            )
        except IntegrationAccessError as exc:
            return _integration_error_response(exc, provider="Google", tenant=tenant)
        except httpx.HTTPStatusError as exc:
            return _provider_error_response(exc, tenant)
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
        except IntegrationAccessError as exc:
            return _integration_error_response(exc, provider="Google", tenant=tenant)
        except httpx.HTTPStatusError as exc:
            return _provider_error_response(exc, tenant)
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

        resolved = _resolve_calendar_window(request, tenant)
        if isinstance(resolved, Response):
            return resolved
        time_min, time_max = resolved

        try:
            token = get_valid_provider_access_token(
                tenant=tenant,
                provider="google",
            )
            payload = get_calendar_freebusy(
                access_token=token.access_token,
                time_min=time_min,
                time_max=time_max,
            )
        except IntegrationAccessError as exc:
            return _integration_error_response(exc, provider="Google", tenant=tenant)
        except httpx.HTTPStatusError as exc:
            return _provider_error_response(exc, tenant)
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


class RuntimeDailyNotesView(KnownValueResponseGuardMixin, APIView):
    """GET raw markdown daily note (agent access). Backed by Document model."""

    permission_classes = [AllowAny]
    authentication_classes = []
    pii_egress_seam = "daily_note_runtime_response"
    pii_egress_text_fields = _JOURNAL_EGRESS_TEXT_FIELDS

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


# P1: relative-day words that rot once a fact is filed under the day it was
# REPORTED rather than the day it HAPPENED — "yesterday's dinner" appended to
# tonight's note reads correctly at write time but misattributes on later
# recall (which keys off the note's date, not the relative word). Longest
# phrase first so "day before yesterday" wins over the "yesterday" it contains.
_RELATIVE_DAY_RE = re.compile(r"\b(day before yesterday|yesterday|last night)\b", re.IGNORECASE)


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

        blocked = assert_write_allowed_for_document_turn(tenant)
        if blocked is not None:
            return blocked

        content_raw = request.data.get("content")
        content = str(content_raw or "").strip()
        if not content:
            return Response(
                {"error": "invalid_request", "detail": "content is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # P0-3: strip agent-written markdown-image beacons before they reach
        # any durable store — see apps/integrations/content_sanitize.py.
        content = neutralize_remote_image_markdown(content)

        try:
            explicit_date = _parse_iso_date(request.data.get("date"), field_name="date")
        except ValueError as exc:
            return Response(
                {"error": "invalid_request", "detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        d = explicit_date or _tenant_today(tenant)

        # P1: only nudge when the caller left `date` to our default AND the
        # content uses a relative-day word — an explicit `date` means the
        # agent already made a dating decision, and this is a nudge (surfaced
        # via the tool response, the one channel that reliably steers agent
        # behavior), never a block on the write itself.
        date_attribution_warning = None
        if explicit_date is None:
            relative_match = _RELATIVE_DAY_RE.search(content)
            if relative_match is not None:
                yesterday = d - timedelta(days=1)
                date_attribution_warning = (
                    f"This entry was filed under {d} (the note's date). It mentions "
                    f"'{relative_match.group(0)}' — if it describes events from a "
                    f"different day, append those facts to that day's note by passing "
                    f"date=YYYY-MM-DD (tenant-local yesterday is {yesterday}), and "
                    f"prefer absolute dates in prose so later reads can't misattribute "
                    f"them."
                )

        slug = str(d)
        # get_or_create outside the lock is fine — it only races on the very
        # first write to a brand-new row (extremely rare); the select_for_update
        # inside the atomic block serialises all concurrent appends after that.
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

        with transaction.atomic():
            # Re-read under a row lock so concurrent appends are serialised
            # and neither writer loses its entry (lost-update prevention).
            doc = Document.objects.select_for_update().get(pk=doc.pk)
            md = doc.markdown or ""

            if section_slug_str:
                heading = resolve_daily_section_heading(
                    tenant=tenant,
                    markdown=md,
                    section_slug=section_slug_str,
                )
                doc.markdown = upsert_markdown_section(md, heading, content)
            else:
                # Quick-log append with timestamp
                now = _tenant_now(tenant)
                timestamp = now.strftime("%H:%M")
                persona_name = _get_persona_name(tenant)
                entry = f"- **{timestamp}** ({persona_name}) — {content}"
                doc.markdown = md.rstrip() + "\n\n" + entry + "\n"

            doc.save(update_fields=["markdown", "updated_at"])

        response_payload = {
            "tenant_id": str(tenant.id),
            "date": str(d),
            "markdown": doc.markdown,
            "sections": parse_daily_sections(doc.markdown),
        }
        if date_attribution_warning:
            response_payload["date_attribution_warning"] = date_attribution_warning

        return Response(response_payload, status=status.HTTP_201_CREATED)


def _upsert_markdown_section(md: str, heading: str, body: str) -> str:
    """Replace the body of a ``## <heading>`` section, or append it if absent.

    Mirrors ``RuntimeDailyNoteAppendView``'s section-merge so a scoped memory
    write touches ONLY its own section and leaves the rest of the document
    intact — the difference between a durable person-fact surviving a
    concurrent compaction ``memoryFlush`` full-document write and being
    silently clobbered by it. The heading match is anchored to a full line so
    ``## People`` cannot match ``## People & Context`` and a ``## `` embedded
    mid-body cannot shift the slice boundary.
    """
    return upsert_markdown_section(md, heading, body)


class RuntimeUserMemoryView(KnownValueResponseGuardMixin, APIView):
    """GET/PUT raw markdown long-term memory (agent access). Backed by Document model."""

    permission_classes = [AllowAny]
    authentication_classes = []
    pii_egress_seam = "memory_runtime_response"
    pii_egress_text_fields = _JOURNAL_EGRESS_TEXT_FIELDS

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

        # P0-3: strip agent-written markdown-image beacons before they reach
        # any durable store — see apps/integrations/content_sanitize.py.
        markdown = neutralize_remote_image_markdown(str(request.data.get("markdown", "")))
        # Optional scoped write: when ``section`` names a ``## <heading>``, only
        # that section is replaced/inserted (a tight person-fact note) rather
        # than the whole document — see P2 capture reflex in AGENTS.md.
        section_raw = request.data.get("section")
        section = str(section_raw).strip() if section_raw else ""

        # get_or_create outside the lock only races on the very first write to a
        # brand-new row; the select_for_update inside the atomic block serialises
        # every write after that. Without it this endpoint's read-modify-write
        # raced the compaction ``memoryFlush`` writer (both drive nbhd_memory_update)
        # and silently lost whichever committed first — the clobber the P2 capture
        # reflex would otherwise amplify (mirrors RuntimeDailyNoteAppendView /
        # EntityRegistryItemView lost-update guards).
        doc, _created = Document.objects.get_or_create(
            tenant=tenant,
            kind="memory",
            slug="long-term",
            defaults={
                "title": _default_title("memory", "long-term"),
                "markdown": (_default_markdown("memory", "long-term", tenant=tenant) if section else markdown),
            },
        )

        with transaction.atomic():
            # Re-read under a row lock so a scoped section write merges against
            # the CURRENT document — including anything a concurrent full-document
            # writer just committed — instead of overwriting a stale copy.
            doc = Document.objects.select_for_update().get(pk=doc.pk)
            if section:
                doc.markdown = _upsert_markdown_section(doc.markdown or "", section, markdown)
            else:
                doc.markdown = markdown
            doc.save(update_fields=["markdown", "updated_at"])

        return Response(
            {"tenant_id": str(tenant.id), "markdown": doc.markdown},
            status=status.HTTP_200_OK,
        )


class RuntimeJournalContextView(KnownValueResponseGuardMixin, APIView):
    """Combined context: recent daily notes, long-term memory, and backbone docs.

    Designed for agent session initialization.  The ``backbone`` key
    returns the tenant's tasks, goals, and ideas documents so the agent
    always starts a session aware of them.
    """

    permission_classes = [AllowAny]
    authentication_classes = []
    pii_egress_seam = "journal_context_runtime_response"
    pii_egress_text_fields = _JOURNAL_EGRESS_TEXT_FIELDS

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

        # slug__gte is a TEXT comparison and daily slugs are ISO dates. A
        # non-date daily slug like "NaN-NaN-NaN" sorts ABOVE every real date
        # ("N" > digits in ASCII), so it would slip past the cutoff into the
        # agent's recent-notes context. Constrain to real YYYY-MM-DD slugs
        # (mirrors path_validation.DAILY_SLUG_RE) so only genuine daily notes
        # reach the assistant.
        recent_docs = (
            Document.objects.filter(tenant=tenant, kind="daily", slug__gte=str(cutoff))
            .filter(slug__regex=r"^\d{4}-\d{2}-\d{2}$")
            .order_by("slug")
        )

        memory_doc = Document.objects.filter(tenant=tenant, kind="memory", slug="long-term").first()

        # Backbone docs: tasks, goals, ideas — always included so the agent
        # starts every session aware of the user's current state.
        #
        # Dual-read for #624: goals + tasks may live as typed Goal/Task rows
        # post-migration. Reuse apps/journal/envelope's already-dual-read
        # renderers so the agent sees the same content as USER.md. Ideas
        # are still Document-only.
        # Local import — see feedback_local_reimport_pattern memory.
        from apps.journal.envelope import render_goals, render_open_tasks

        backbone_data: dict[str, dict] = {}

        goals_markdown = render_goals(tenant)
        if goals_markdown.strip():
            backbone_data[Document.Kind.GOAL] = {
                "slug": "goals",
                "title": "Active goals",
                "markdown": goals_markdown,
            }

        tasks_markdown = render_open_tasks(tenant)
        if tasks_markdown.strip():
            backbone_data[Document.Kind.TASKS] = {
                "slug": "tasks",
                "title": "Open tasks",
                "markdown": tasks_markdown,
            }

        ideas_doc = Document.objects.filter(tenant=tenant, kind=Document.Kind.IDEAS).first()
        if ideas_doc:
            backbone_data[Document.Kind.IDEAS] = {
                "slug": ideas_doc.slug,
                "title": ideas_doc.title,
                "markdown": ideas_doc.markdown,
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

        # Constellation activity: pinned notes, star-scoped reflections, and the
        # honest signals tutoring captured — so the agent starts a session aware
        # of what the user has been working through in their galaxy, the same way
        # it starts aware of goals/tasks. Empty dict (key omitted) when quiet.
        # Local import — see feedback_local_reimport_pattern memory.
        from apps.lessons.agent_context import build_constellation_context

        constellation_data = build_constellation_context(tenant, days=days)

        # North Star — confirmed/evolving purposes give the agent the user's
        # stated DIRECTION at session start, the frame above goals/tasks. Only
        # confirmed (+evolving) surface: a proposed hypothesis is a question the
        # user hasn't answered, so it must not ground the agent's reasoning.
        # Local import — see feedback_local_reimport_pattern memory.
        from apps.journal.models import Purpose

        north_star = [
            {
                "id": str(p.id),
                "statement": p.statement,
                "pillars": p.pillars or [],
                "status": p.status,
            }
            for p in Purpose.objects.filter(
                tenant=tenant,
                status__in=[Purpose.Status.CONFIRMED, Purpose.Status.EVOLVING],
            ).order_by("-updated_at")[:10]
        ]

        response_data = {
            "tenant_id": str(tenant.id),
            "recent_notes": notes_data,
            "long_term_memory": memory_doc.markdown if memory_doc else "",
            "recent_notes_count": len(notes_data),
            "days_back": days,
            "backbone": backbone_data,
        }
        if north_star:
            response_data["north_star"] = north_star
        if constellation_data:
            response_data["constellation"] = constellation_data

        return Response(response_data, status=status.HTTP_200_OK)


def _serialize_session_for_runtime(session: Session) -> dict:
    return {
        "id": str(session.id),
        "source": session.source,
        "project": session.project,
        "project_identity": session.project_identity,
        "project_type": session.project_type,
        "session_start": session.session_start.isoformat(),
        "session_end": session.session_end.isoformat(),
        "summary": session.summary,
        "accomplishments": session.accomplishments,
        "blockers": session.blockers,
        "next_steps": session.next_steps,
        "references": session.references,
        "created_at": session.created_at.isoformat(),
    }


class RuntimeSessionsPendingView(APIView):
    """List undistilled work sessions for the tenant.

    Returns sessions that have not yet been distilled into journal/tasks/goals/memory
    by the assistant. Excludes ``test_mode`` sessions. Ordered by session_start desc.
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
            limit = _parse_positive_int(
                request.query_params.get("limit"),
                default=10,
                max_value=25,
            )
        except ValueError as exc:
            return Response(
                {"error": "invalid_request", "detail": f"limit {exc}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        qs = Session.objects.filter(
            tenant=tenant,
            processed_at__isnull=True,
            test_mode=False,
        ).order_by("-session_start")[:limit]

        sessions_data = [_serialize_session_for_runtime(s) for s in qs]

        return Response(
            {
                "tenant_id": str(tenant.id),
                "count": len(sessions_data),
                "sessions": sessions_data,
            },
            status=status.HTTP_200_OK,
        )


class RuntimeSessionMarkProcessedView(APIView):
    """Mark a session as distilled.

    Idempotent: if the session is already processed, returns the existing
    ``processed_at``/``processed_summary`` without overwriting them. The
    assistant is expected to have already written content to the appropriate
    primitives (journal/tasks/goals/memory) before calling this endpoint.
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request, tenant_id, session_id):
        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        session = Session.objects.filter(tenant=tenant, id=session_id).first()
        if session is None:
            return Response(
                {"error": "session_not_found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Idempotency: already-processed sessions return current state without overwrite.
        if session.processed_at is not None:
            return Response(
                {
                    "session_id": str(session.id),
                    "processed_at": session.processed_at.isoformat(),
                    "processed_summary": session.processed_summary,
                    "already_processed": True,
                },
                status=status.HTTP_200_OK,
            )

        raw_summary = request.data.get("processed_summary", {})
        if not isinstance(raw_summary, dict):
            return Response(
                {
                    "error": "invalid_request",
                    "detail": "processed_summary must be an object",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        session.processed_at = tz.now()
        session.processed_summary = raw_summary
        session.save(update_fields=["processed_at", "processed_summary"])

        return Response(
            {
                "session_id": str(session.id),
                "processed_at": session.processed_at.isoformat(),
                "processed_summary": session.processed_summary,
                "already_processed": False,
            },
            status=status.HTTP_200_OK,
        )


class RuntimeLessonCreateView(KnownValueResponseGuardMixin, APIView):
    """Create lessons captured by the assistant for a tenant.

    Lessons are auto-approved on creation (status="approved") — they join the
    constellation immediately and get an embedding + connections, matching the
    journal-extraction approval path. Users prune unwanted lessons from the
    constellation UI rather than gating each one through an approval queue.
    """

    permission_classes = [AllowAny]
    authentication_classes = []
    pii_egress_seam = "lesson_suggest_runtime_response"
    pii_egress_text_fields = _LESSON_EGRESS_TEXT_FIELDS

    def post(self, request, tenant_id):
        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        blocked = assert_write_allowed_for_document_turn(tenant)
        if blocked is not None:
            return blocked

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
            status="approved",
            approved_at=tz.now(),
        )

        # Generate embedding + connections, then re-cluster once enough lessons
        # exist — same post-approval pipeline as the journal-extraction path
        # (apps/router/extraction_callbacks.py). Best-effort: a failure here
        # must not fail the capture.
        try:
            from apps.lessons.services import process_approved_lesson

            process_approved_lesson(lesson)
        except Exception:
            logger.exception("runtime: embedding failed for lesson %s", lesson.id)

        try:
            from apps.lessons.clustering import refresh_constellation

            if Lesson.objects.filter(tenant=tenant, status="approved").count() >= 5:
                refresh_constellation(tenant)
        except Exception:
            logger.exception("runtime: clustering failed for tenant %s", str(tenant.id)[:8])

        return Response(
            {
                "tenant_id": str(tenant.id),
                "lesson": LessonSerializer(lesson).data,
            },
            status=201,
        )


class RuntimeLessonSearchView(KnownValueResponseGuardMixin, APIView):
    """Search approved lessons for a tenant."""

    permission_classes = [AllowAny]
    authentication_classes = []
    pii_egress_seam = "lesson_search_runtime_response"
    pii_egress_text_fields = _LESSON_EGRESS_TEXT_FIELDS

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
            # Materialize the QuerySet inside the guard so DB-execution
            # failures (OperationalError, pgvector unavailable, network drop)
            # are mapped to the search_failed envelope rather than raised
            # during the later, unguarded iteration.
            lessons = list(search_lessons(tenant=tenant, query=query, limit=limit))
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


class RuntimeLessonPendingView(KnownValueResponseGuardMixin, APIView):
    """Get pending lessons for a tenant."""

    permission_classes = [AllowAny]
    authentication_classes = []
    pii_egress_seam = "lesson_pending_runtime_response"
    pii_egress_text_fields = _LESSON_EGRESS_TEXT_FIELDS

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


class RuntimeConstellationNotesView(APIView):
    """Enriched constellation context for the assistant (pull tool).

    Backs ``nbhd_constellation_notes``. Surfaces what the constellation game
    captured but no pillar tool previously exposed: pinned ``galaxy_note``s,
    star-scoped journal reflections, and the honest signals from tutoring
    sessions (restate accuracy, edge-case finding, connections, mastery, topic
    drift). Three modes, in precedence order:

      * ``?star_id=<id>`` — full context for one approved star
      * ``?q=<text>``     — semantic/text search over stars, each enriched
      * (no params)       — recently active stars
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
            limit = _parse_positive_int(request.query_params.get("limit"), default=5, max_value=25)
            days = _parse_positive_int(request.query_params.get("days"), default=30, max_value=365)
        except ValueError as exc:
            return Response({"error": "invalid_request", "detail": str(exc)}, status=400)

        star_id_raw = request.query_params.get("star_id")
        star_id = None
        if star_id_raw not in (None, ""):
            try:
                star_id = int(star_id_raw)
            except (TypeError, ValueError):
                return Response(
                    {"error": "invalid_request", "detail": "star_id must be an integer"},
                    status=400,
                )

        query = str(request.query_params.get("q", "")).strip() or None

        from apps.lessons.agent_context import constellation_notes_payload

        try:
            payload = constellation_notes_payload(tenant, q=query, star_id=star_id, limit=limit, days=days)
        except Exception as exc:
            return Response({"error": "constellation_failed", "detail": str(exc)}, status=500)

        payload["tenant_id"] = str(tenant.id)
        return Response(payload, status=200)


# ---------------------------------------------------------------------------
# v2 Document runtime endpoints (unified model)
# ---------------------------------------------------------------------------


class RuntimeDocumentView(KnownValueResponseGuardMixin, APIView):
    """GET/PUT a document by kind+slug (agent access)."""

    permission_classes = [AllowAny]
    authentication_classes = []
    pii_egress_seam = "journal_document_runtime_response"
    pii_egress_text_fields = _JOURNAL_EGRESS_TEXT_FIELDS

    def get(self, request, tenant_id):
        from apps.journal.path_validation import VALID_KINDS, canonical_singleton_slug, validate_kind_slug

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
        # Singleton kinds (goal→"goals", tasks→"tasks") resolve to their
        # one-per-tenant canonical slug regardless of what the caller passed —
        # closes the footgun where an omitted slug defaulted to the kind string
        # ("goal", singular) and forked a duplicate doc from the real "goals".
        slug = canonical_singleton_slug(kind, slug)
        if not slug:
            # Remaining kinds with no slug fall back to the kind string.
            slug = kind

        # Validate daily slugs must be valid dates (stricter than the general rule)
        if kind == "daily":
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", slug):
                return Response(
                    {
                        "error": "invalid_request",
                        "detail": f"Daily note slug must be a date (YYYY-MM-DD), got: {slug!r}",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # General path-component validation prevents reads from resolving
        # NTFS-hostile kind/slug values that cannot map to workspace paths.
        validation_error = validate_kind_slug(kind, slug)
        if validation_error is not None:
            error_code, detail = validation_error
            body = {"error": error_code, "detail": detail}
            if error_code == "invalid_kind":
                body["valid_kinds"] = sorted(VALID_KINDS)
            return Response(
                body,
                status=status.HTTP_400_BAD_REQUEST,
            )

        doc = Document.objects.filter(
            tenant=tenant,
            kind=kind,
            slug=slug,
        ).first()
        if doc is None:
            available_slugs = list(
                Document.objects.filter(tenant=tenant, kind=kind)
                .order_by("-updated_at")
                .values_list("slug", flat=True)[:20]
            )
            return Response(
                {
                    "exists": False,
                    "kind": kind,
                    "slug": slug,
                    "available_slugs": available_slugs,
                },
                status=status.HTTP_200_OK,
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
        from apps.journal.path_validation import canonical_singleton_slug, validate_kind_slug

        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        blocked = assert_write_allowed_for_document_turn(tenant)
        if blocked is not None:
            return blocked

        kind = str(request.data.get("kind", "")).strip()
        slug = str(request.data.get("slug", "")).strip()
        # P0-3: strip agent-written markdown-image beacons before they reach
        # any durable store — see apps/integrations/content_sanitize.py.
        markdown = neutralize_remote_image_markdown(str(request.data.get("markdown", "")))
        title = str(request.data.get("title", "")).strip()

        if not kind:
            return Response(
                {"error": "invalid_request", "detail": "kind is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # Singleton kinds resolve to their canonical slug so a goal write never
        # forks a second "goals" doc under slug "goal" (see get() above).
        slug = canonical_singleton_slug(kind, slug)
        if not slug:
            slug = kind

        validation_error = validate_kind_slug(kind, slug)
        if validation_error is not None:
            error_code, detail = validation_error
            return Response(
                {"error": error_code, "detail": detail},
                status=status.HTTP_400_BAD_REQUEST,
            )

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


class RuntimeJournalSearchView(KnownValueResponseGuardMixin, APIView):
    """Full-text search across all documents for a tenant."""

    permission_classes = [AllowAny]
    authentication_classes = []
    pii_egress_seam = "journal_search_runtime_response"
    pii_egress_text_fields = _JOURNAL_EGRESS_TEXT_FIELDS

    def get(self, request, tenant_id):
        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        query = request.query_params.get("q", "").strip()
        kind = request.query_params.get("kind", "").strip()
        try:
            limit = _parse_positive_int(request.query_params.get("limit"), default=20, max_value=50)
        except ValueError as exc:
            return Response(
                {"error": "invalid_request", "detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not query:
            return Response(
                {"error": "invalid_request", "detail": "q parameter required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector

        # Content-based full-text search is intentionally NOT slug-filtered by
        # date: a mis-kinded daily with real content (e.g. an old
        # "<date>-debt-chart") must stay findable. Empty pre-guard stubs rank ~0
        # and are removed by migration 0020. Only the date-windowed CONTEXT
        # bundle filters daily slugs (see RuntimeJournalContextView).
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

        # The cred just flipped to ERROR. At paste time the container was
        # forced into a BYO-only state: openclaw.json pins the Claude primary
        # with an intentionally empty fallback list and the env strips
        # ANTHROPIC_API_KEY in favour of CLAUDE_CODE_OAUTH_TOKEN. With the
        # cred now in ERROR, both _byo_model_extras and
        # apply_byo_credentials_to_container exclude it, so a regen restores
        # metered fallbacks AND ANTHROPIC_API_KEY — keeping the assistant
        # responsive on a platform model while the user reconnects. The
        # paste/delete paths do this same coupling; the error path must too,
        # otherwise the running container keeps the stale BYO-only config and
        # the assistant goes dark for ALL turns (the hourly apply-pending
        # cron skips this tenant because the error path never bumped pending).
        # Each step is best-effort: log on failure but still ack 200 so the
        # runtime does not retry.
        from apps.byo_models.services import regenerate_tenant_config
        from apps.orchestrator.azure_client import apply_byo_credentials_to_container

        try:
            tenant.bump_pending_config()
            regenerate_tenant_config(tenant)
        except Exception:
            logger.exception(
                "regenerate_tenant_config failed for tenant=%s after BYO error report — "
                "openclaw.json may stay BYO-only until the next config bump",
                tenant.id,
            )

        try:
            apply_byo_credentials_to_container(tenant)
        except Exception:
            logger.exception(
                "apply_byo_credentials_to_container failed for tenant=%s after BYO error report",
                tenant.id,
            )

        return Response({"status": "ok", "credential_id": str(cred.id)}, status=status.HTTP_200_OK)


class RuntimeMemorySyncView(APIView):
    """GET files dict for workspace memory sync (agent/container access).

    Returns all syncable documents as a mapping of workspace-relative paths
    to markdown content.  The caller writes them to the local filesystem
    as a journal-of-record mirror. OpenClaw's ``memory_search`` no longer
    indexes them (disabled fleet-wide); search now routes through
    ``nbhd_journal_search`` → Postgres.
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


class RuntimeDocumentAppendView(KnownValueResponseGuardMixin, APIView):
    """POST append content to a document (agent access)."""

    permission_classes = [AllowAny]
    authentication_classes = []
    pii_egress_seam = "journal_document_append_runtime_response"
    pii_egress_text_fields = _JOURNAL_EGRESS_TEXT_FIELDS

    def post(self, request, tenant_id):
        from apps.journal.path_validation import canonical_singleton_slug, validate_kind_slug

        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        blocked = assert_write_allowed_for_document_turn(tenant)
        if blocked is not None:
            return blocked

        kind = str(request.data.get("kind", "daily")).strip()
        slug = str(request.data.get("slug", "")).strip()
        content = str(request.data.get("content", "")).strip()

        if not content:
            return Response(
                {"error": "invalid_request", "detail": "content is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # P0-3: strip agent-written markdown-image beacons before they reach
        # any durable store — see apps/integrations/content_sanitize.py.
        content = neutralize_remote_image_markdown(content)
        # Singleton kinds resolve to their canonical slug so an append never
        # forks a second "goals" doc under slug "goal" (see RuntimeDocumentView).
        slug = canonical_singleton_slug(kind, slug)
        if not slug:
            if kind == "daily":
                slug = str(_tenant_today(tenant))
            else:
                slug = kind

        validation_error = validate_kind_slug(kind, slug)
        if validation_error is not None:
            error_code, detail = validation_error
            return Response(
                {"error": error_code, "detail": detail},
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

        with transaction.atomic():
            # Re-read under a row lock to serialise concurrent appends and
            # prevent a lost-update when two writers hit the same document.
            doc = Document.objects.select_for_update().get(pk=doc.pk)
            time_str = _tenant_now(tenant).strftime("%H:%M")
            persona_name = _get_persona_name(tenant)
            entry_block = f"\n\n### {time_str} — {persona_name}\n{content}\n"
            doc.markdown = (doc.markdown or "").rstrip() + entry_block
            doc.save(update_fields=["markdown", "updated_at"])

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
            if not name:
                return Response(
                    {"error": "invalid_display_name", "detail": "display_name must not be empty."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if len(name) > 100:
                return Response(
                    {"error": "invalid_display_name", "detail": "display_name must be 100 characters or fewer."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            user.display_name = name
            updated_fields.append("display_name")

        if "language" in data:
            lang = (data["language"] or "").strip()
            if not lang:
                return Response(
                    {"error": "invalid_language", "detail": "language must not be empty."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if len(lang) > 10:
                return Response(
                    {"error": "invalid_language", "detail": "language must be 10 characters or fewer."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            user.language = lang
            updated_fields.append("language")

        if "location_city" in data:
            city = (data["location_city"] or "").strip()
            if not city:
                return Response(
                    {"error": "invalid_location_city", "detail": "location_city must not be empty."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if len(city) > 255:
                return Response(
                    {"error": "invalid_location_city", "detail": "location_city must be 255 characters or fewer."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
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

        # If timezone changed, update schedule.tz on the tenant's CronJob
        # rows. The post_save signal fires ``regenerate_tenant_crons``,
        # which detects ``schedule.tz`` drift and pushes the new state to
        # OpenClaw. Postgres is the source of truth; the reconciler is
        # the single writer for system cron payload state.
        if "timezone" in updated_fields:
            try:
                from apps.cron.models import CronJob

                new_tz = user.timezone
                updated_rows = 0
                for row in CronJob.objects.filter(tenant=tenant, managed=True):
                    data = dict(row.data or {})
                    sched = dict(data.get("schedule") or {})
                    if sched.get("tz") == new_tz:
                        continue
                    sched["tz"] = new_tz
                    data["schedule"] = sched
                    row.data = data
                    row.save(update_fields=["data", "updated_at"])
                    updated_rows += 1
                logger.info(
                    "Timezone change for tenant %s (tz=%s): updated %d cron rows; "
                    "reconciler will push schedule.tz drift to gateway",
                    tenant.id,
                    new_tz,
                    updated_rows,
                )
            except Exception:
                logger.exception("Failed to update cron row timezones for tenant %s", tenant.id)

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


class RuntimeSituationUpdateView(APIView):
    """POST /api/v1/integrations/runtime/<tenant_id>/situation/

    Allows the agent to record a city-level place stated by the user.
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

        from apps.tenants.situation import clean_place_label

        place_label = clean_place_label(request.data.get("place_label"))
        if not place_label:
            return Response({"ok": False, "reason": "invalid_label"})

        from apps.orchestrator.envelope_registry import suppress_refresh
        from apps.tenants.situation import record_place_observation

        # UserSituation is registry-wired for out-of-band writes, but this
        # request owns the explicit background push below. Suppress the model
        # signal so one accepted label change produces one push.
        with suppress_refresh():
            changed = record_place_observation(
                tenant,
                place_label,
                source="assistant",
            )
        if changed:
            from apps.orchestrator.workspace_envelope import push_user_md_in_background

            push_user_md_in_background(tenant)

        return Response({"ok": True, "changed": changed})


_RECONCILE_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "but",
        "with",
        "from",
        "this",
        "that",
        "have",
        "had",
        "was",
        "were",
        "are",
        "you",
        "your",
        "user",
        "today",
        "just",
        "now",
        "did",
        "got",
        "into",
        "out",
        "than",
        "then",
        "been",
        "being",
        "about",
        "some",
        "any",
        "all",
        "say",
        "said",
        "tell",
        "told",
    }
)

_FINANCE_KEYWORDS = frozenset(
    {
        "paid",
        "pay",
        "payment",
        "payments",
        "paying",
        "bill",
        "bills",
        "billed",
        "loan",
        "loans",
        "debt",
        "debts",
        "card",
        "cards",
        "credit",
        "balance",
        "balances",
        "transaction",
        "transactions",
        "deposit",
        "deposits",
        "withdraw",
        "withdrew",
        "withdrawal",
        "interest",
        "owe",
        "owed",
        "owes",
        "due",
        "minimum",
        "principal",
        "transfer",
        "transferred",
        "mortgage",
        "rent",
        "account",
        "bank",
        "spent",
        "spend",
        "cost",
        "income",
        "invoice",
    }
)

_FUEL_KEYWORDS = frozenset(
    {
        "workout",
        "workouts",
        "ran",
        "run",
        "running",
        "lift",
        "lifted",
        "lifting",
        "train",
        "trained",
        "training",
        "gym",
        "exercise",
        "exercised",
        "cardio",
        "swim",
        "swam",
        "swimming",
        "bike",
        "biked",
        "biking",
        "ride",
        "rode",
        "hike",
        "hiked",
        "yoga",
        "stretch",
        "stretched",
        "weight",
        "weighed",
        "weighs",
        "lbs",
        "kg",
        "kilograms",
        "pounds",
        "pound",
        "push",
        "pull",
        "legs",
        "cycle",
        "cycled",
        "rpe",
        "sets",
        "reps",
        "miles",
        "mile",
        "kilometers",
        "kilometer",
        "km",
    }
)

# Direction-touching claims — the kind of thing where a confirmed North Star is
# the relevant frame ("thinking of quitting X", "should I take this job",
# "not sure this is the right path"). When triggered, ALL confirmed purposes
# surface even without a token overlap, so the agent can weigh the decision
# against the user's stated direction. Detection is two-tier: STRONG words are
# precise enough to fire alone; WEAK words are everyday vocabulary ("should",
# "job", "change") that only signal direction in combination — requiring two
# distinct weak hits keeps "I should buy milk" from surfacing the North Star
# on every reconcile.
_DIRECTION_STRONG = frozenset(
    {
        "quit",
        "quitting",
        "resign",
        "resigning",
        "career",
        "purpose",
        "meaning",
        "direction",
        "dream",
        "calling",
        "vocation",
        "relocate",
        "relocating",
        "pivot",
        "regret",
        "fulfilled",
        "fulfilling",
        "unfulfilled",
        "longterm",
        "priorities",
        "reconsider",
    }
)
_DIRECTION_WEAK = frozenset(
    {
        "leave",
        "leaving",
        "job",
        "path",
        "future",
        "move",
        "moving",
        "change",
        "changing",
        "decision",
        "choose",
        "choosing",
        "should",
        "worth",
        "stuck",
        "lost",
    }
)


def _reconcile_tokenize(text: str) -> list[str]:
    """Lowercase, split on non-alphanumeric, drop stopwords and length<3."""
    if not text:
        return []
    cleaned = re.sub(r"[^a-z0-9 ]+", " ", text.lower())
    return [t for t in cleaned.split() if len(t) >= 3 and t not in _RECONCILE_STOPWORDS]


def _reconcile_match_score(tokens: list[str], haystack: str) -> tuple[int, list[str]]:
    """Return (score, matched_tokens) for token substring matches in haystack."""
    if not tokens or not haystack:
        return 0, []
    lowered = haystack.lower()
    matched: list[str] = []
    for tok in tokens:
        if tok in lowered and tok not in matched:
            matched.append(tok)
    return len(matched), matched


class RuntimeReconcileScanView(KnownValueResponseGuardMixin, APIView):
    """GET /api/v1/integrations/runtime/<tenant_id>/reconcile/scan/

    Given a one-sentence ``claim`` describing what the user just reported,
    return the active goals, open tasks, project docs, finance accounts,
    and fuel rows that are plausibly affected. Each candidate is annotated with which
    typed write tool the agent should call to apply the update.

    This is the function half of the AGENTS.md conversational reconcile
    gate — the agent decides whether the user's message is "material"
    enough to scan, then calls this endpoint, then applies updates via
    the existing typed tools.
    """

    permission_classes = [AllowAny]
    authentication_classes = []
    pii_egress_seam = "reconcile_scan_runtime_response"
    pii_egress_text_fields = _LIFECYCLE_EGRESS_TEXT_FIELDS | frozenset(
        {"before", "after", "reason", "claim", "statement", "excerpt", "matched_tokens"}
    )

    def get(self, request, tenant_id):
        from apps.fuel.models import BodyWeightLog, Workout, WorkoutStatus
        from apps.journal.models import Goal, Purpose, Task

        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        claim = request.query_params.get("claim", "").strip()
        if not claim:
            return Response(
                {"error": "invalid_request", "detail": "claim parameter required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(claim) > 500:
            claim = claim[:500]

        try:
            limit = min(int(request.query_params.get("limit", "15")), 25)
        except (TypeError, ValueError):
            limit = 15

        tokens = _reconcile_tokenize(claim)
        finance_triggered = any(t in _FINANCE_KEYWORDS for t in tokens)
        fuel_triggered = any(t in _FUEL_KEYWORDS for t in tokens)
        direction_triggered = any(t in _DIRECTION_STRONG for t in tokens) or (
            len({t for t in tokens if t in _DIRECTION_WEAK}) >= 2
        )

        candidates: list[dict] = []

        # ── North Star (Purpose) ─────────────────────────────────────
        # Confirmed/evolving purposes are the frame for direction-touching
        # claims. Surfaced on a token overlap with the statement OR whenever a
        # direction keyword fires (a claim like "thinking of quitting my job"
        # may share no tokens with "build a life around my family" yet is
        # exactly the moment to weigh it against the North Star). Purposes are
        # few per tenant, so always scanned like goals/tasks.
        confirmed_purposes = Purpose.objects.filter(
            tenant=tenant,
            status__in=[Purpose.Status.CONFIRMED, Purpose.Status.EVOLVING],
        )[:20]
        for purpose in confirmed_purposes:
            score, matched = _reconcile_match_score(tokens, purpose.statement)
            if score == 0 and not direction_triggered:
                continue
            pillars = [str(p) for p in (purpose.pillars or []) if str(p).strip()]
            candidates.append(
                {
                    "kind": "purpose",
                    "id": str(purpose.id),
                    "title": purpose.statement[:80],
                    "pillar": pillars[0] if pillars else None,
                    "status": purpose.status,
                    "score": score + (1 if direction_triggered else 0),
                    "matched_tokens": matched or (["(direction-keyword)"] if direction_triggered else []),
                    "current_state": {
                        "statement": purpose.statement[:280],
                        "pillars": pillars,
                        "status": purpose.status,
                    },
                    "update_tools": [
                        "nbhd_purpose_update",
                        "nbhd_purpose_link_goal",
                    ],
                }
            )

        # ── Goals ────────────────────────────────────────────────────
        active_goals = Goal.objects.filter(tenant=tenant, status=Goal.Status.ACTIVE)[:50]
        for goal in active_goals:
            haystack = f"{goal.title}\n{goal.description}"
            score, matched = _reconcile_match_score(tokens, haystack)
            if score == 0:
                continue
            candidates.append(
                {
                    "kind": "goal",
                    "id": str(goal.id),
                    "title": goal.title,
                    "pillar": goal.pillar or None,
                    "status": goal.status,
                    "score": score,
                    "matched_tokens": matched,
                    "current_state": {
                        "target": goal.target,
                        "target_date": goal.target_date.isoformat() if goal.target_date else None,
                        "description": goal.description[:280] if goal.description else "",
                    },
                    "update_tools": [
                        "nbhd_goal_update",
                        "nbhd_goal_achieve",
                        "nbhd_goal_abandon",
                    ],
                }
            )

        # ── Tasks ────────────────────────────────────────────────────
        open_tasks = Task.objects.filter(
            tenant=tenant,
            status__in=[Task.Status.OPEN, Task.Status.IN_PROGRESS],
        )[:100]
        for task in open_tasks:
            haystack = f"{task.title}\n{task.description}"
            score, matched = _reconcile_match_score(tokens, haystack)
            if score == 0:
                continue
            candidates.append(
                {
                    "kind": "task",
                    "id": str(task.id),
                    "title": task.title,
                    "pillar": task.pillar or None,
                    "status": task.status,
                    "score": score,
                    "matched_tokens": matched,
                    "current_state": {
                        "due_date": task.due_date.isoformat() if task.due_date else None,
                        "parent_goal_id": str(task.parent_goal_id) if task.parent_goal_id else None,
                        "related_ref": task.related_ref,
                        "description": task.description[:280] if task.description else "",
                    },
                    "update_tools": [
                        "nbhd_task_complete",
                        "nbhd_task_update",
                        "nbhd_task_skip",
                        "nbhd_task_defer",
                    ],
                }
            )

        # ── Project documents ────────────────────────────────────────
        # Long-lived project threads (journal_document kind='project'). The
        # agent already has nbhd_document_append (which accepts kind='project'),
        # but reconcile never surfaced these — so a conversational project-
        # status update never reached the canonical project doc, leaving it
        # stale for the proactive crons that read it. Always scanned (core
        # threads, like goals/tasks); project docs are few per tenant.
        project_docs = Document.objects.filter(tenant=tenant, kind="project")[:50]
        for doc in project_docs:
            score, matched = _reconcile_match_score(tokens, f"{doc.title}\n{doc.markdown}")
            if score == 0:
                continue
            candidates.append(
                {
                    "kind": "project",
                    "id": doc.slug,
                    "title": doc.title,
                    "pillar": doc.pillar or None,
                    "score": score,
                    "matched_tokens": matched,
                    "current_state": {
                        "slug": doc.slug,
                        "updated_at": doc.updated_at.isoformat(),
                        "excerpt": (doc.markdown or "")[-280:],
                    },
                    "update_tools": ["nbhd_document_append"],
                }
            )

        # ── Finance accounts ─────────────────────────────────────────
        if finance_triggered and getattr(tenant, "finance_active", False):
            from apps.finance.models import FinanceAccount

            active_accounts = list(FinanceAccount.objects.filter(tenant=tenant, is_active=True))
            account_hits: list[tuple[int, list[str], FinanceAccount]] = []
            for account in active_accounts:
                score, matched = _reconcile_match_score(tokens, account.nickname)
                if score > 0:
                    account_hits.append((score + 1, matched, account))  # +1 boost for explicit nickname match
                elif account.is_debt:
                    account_hits.append((1, ["(finance-keyword fallback)"], account))
            account_hits.sort(key=lambda r: (-r[0], -float(r[2].current_balance or 0)))
            for score, matched, account in account_hits[:5]:
                candidates.append(
                    {
                        "kind": "finance_account",
                        "id": str(account.id),
                        "title": account.nickname,
                        "pillar": "gravity",
                        "score": score,
                        "matched_tokens": matched,
                        "current_state": {
                            "account_type": account.account_type,
                            "current_balance": str(account.current_balance),
                            "is_debt": account.is_debt,
                            "due_day": account.due_day,
                            "minimum_payment": (
                                str(account.minimum_payment) if account.minimum_payment is not None else None
                            ),
                            "interest_rate": (
                                str(account.interest_rate) if account.interest_rate is not None else None
                            ),
                        },
                        "update_tools": [
                            "nbhd_finance_record_payment",
                            "nbhd_finance_update_balance",
                        ],
                    }
                )

        # ── Fuel ─────────────────────────────────────────────────────
        if fuel_triggered and getattr(tenant, "fuel_enabled", False):
            today = _tenant_today(tenant)
            window_start = today - timedelta(days=2)
            window_end = today + timedelta(days=1)
            recent_workouts = list(
                Workout.objects.filter(tenant=tenant, date__gte=window_start, date__lte=window_end).order_by(
                    "-date", "-created_at"
                )[:10]
            )
            for workout in recent_workouts:
                bonus = 0
                if workout.status == WorkoutStatus.PLANNED and workout.date == today:
                    bonus = 2  # most likely candidate for "just did it" updates
                score, matched = _reconcile_match_score(tokens, f"{workout.activity} {workout.category}")
                if score == 0 and bonus == 0:
                    continue
                candidates.append(
                    {
                        "kind": "fuel_workout",
                        "id": str(workout.id),
                        "title": workout.activity,
                        "pillar": "fuel",
                        "score": score + bonus,
                        "matched_tokens": matched or (["(scheduled-today)"] if bonus else []),
                        "current_state": {
                            "date": workout.date.isoformat(),
                            "category": workout.category,
                            "status": workout.status,
                            "duration_minutes": workout.duration_minutes,
                            "rpe": workout.rpe,
                        },
                        "update_tools": [
                            "nbhd_fuel_update_workout",
                            "nbhd_fuel_log_workout",
                        ],
                    }
                )

            if any(t in {"weight", "weighed", "weighs", "lbs", "kg", "pounds", "pound", "kilograms"} for t in tokens):
                latest_weight = BodyWeightLog.objects.filter(tenant=tenant).order_by("-date").first()
                if latest_weight is not None:
                    candidates.append(
                        {
                            "kind": "fuel_body_weight",
                            "id": str(latest_weight.id),
                            "title": f"Body weight on {latest_weight.date.isoformat()}",
                            "pillar": "fuel",
                            "score": 1,
                            "matched_tokens": ["(weight-keyword)"],
                            "current_state": {
                                "date": latest_weight.date.isoformat(),
                                "weight_kg": str(latest_weight.weight_kg),
                            },
                            "update_tools": ["nbhd_fuel_log_body_weight"],
                        }
                    )

        candidates.sort(key=lambda c: -c["score"])
        candidates = candidates[:limit]

        return Response(
            {
                "tenant_id": str(tenant.id),
                "claim": claim,
                "tokens": tokens,
                "triggered": {
                    "finance": finance_triggered,
                    "fuel": fuel_triggered,
                    "direction": direction_triggered,
                },
                "count": len(candidates),
                "candidates": candidates,
            },
            status=status.HTTP_200_OK,
        )


class RuntimeCronPhase2SummaryView(KnownValueResponseGuardMixin, APIView):
    """POST /api/v1/integrations/runtime/<tenant_id>/cron-phase2-summary/

    DEPRECATED 2026-07-11: seed-prompt emission of the phase2 sync block was
    removed (config_generator._build_cron_message) — superseded by the
    deterministic ProactiveOutbound bridge. Kept live because hibernated
    tenants wake on stale configs whose prompts still call this tool; remove
    the endpoint only after fleet config convergence.

    Receives a 2-3 sentence summary from a foreground cron run that messaged
    the user, and creates the corresponding ``_sync:<job_name>`` one-shot
    cron with ``sessionTarget=main`` so the user's main chat session learns
    what just happened. The agent only provides the summary; Django owns
    cron expression math, payload composition, and self-removal text — a
    deliberate split that keeps the model out of failure-prone tool-param
    assembly. See ``apps/orchestrator/config_generator.py:_phase2_sync_block``
    for the prompt-side contract.
    """

    permission_classes = [AllowAny]
    authentication_classes = []
    pii_egress_seam = "cron_summary_runtime_response"
    pii_egress_text_fields = _CRON_EGRESS_TEXT_FIELDS

    def post(self, request, tenant_id):
        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        data = request.data or {}
        summary = str(data.get("summary", "")).strip()
        job_name = str(data.get("job_name", "")).strip()
        if not summary:
            return Response(
                {"error": "summary_required", "detail": "summary must be a non-empty string"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not job_name:
            return Response(
                {"error": "job_name_required", "detail": "job_name must be a non-empty string"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Compute a cron expr ~2 minutes from now in the user's tz. Two
        # minutes is enough buffer for the cron run jsonl to flush and the
        # main session to be available, and short enough that the sync
        # arrives while the user is still likely engaged with the message.
        tz_name = _tenant_timezone_name(tenant)
        try:
            user_tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            user_tz = ZoneInfo("UTC")
            tz_name = "UTC"
        fire_at = datetime.now(user_tz) + timedelta(minutes=2)
        # Date-specific expression: this minute, this hour, this day, this
        # month — matches once and then never again, which is what we want
        # for a one-shot whose payload also self-removes.
        cron_expr = f"{fire_at.minute} {fire_at.hour} {fire_at.day} {fire_at.month} *"

        sync_name = f"_sync:{job_name}"
        payload_text = f"[Sync — {job_name}] {summary} After noting this, run: cron remove {sync_name}"

        from apps.cron.gateway_client import GatewayError, invoke_gateway_tool

        try:
            invoke_gateway_tool(
                tenant,
                "cron.add",
                {
                    "name": sync_name,
                    "schedule": {"kind": "cron", "expr": cron_expr, "tz": tz_name},
                    "sessionTarget": "main",
                    "wakeMode": "now",
                    "payload": {"kind": "systemEvent", "text": payload_text},
                    "enabled": True,
                },
            )
        except GatewayError as exc:
            logger.warning(
                "Phase 2 sync cron.add failed for tenant=%s job=%s: %s",
                tenant.id,
                job_name,
                exc,
            )
            return Response(
                {"error": "gateway_failed", "detail": str(exc)},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(
            {
                "ok": True,
                "sync_cron_name": sync_name,
                "fires_at": fire_at.isoformat(),
            },
            status=status.HTTP_200_OK,
        )


# ---------------------------------------------------------------------------
# Workspace runtime endpoints
# ---------------------------------------------------------------------------

# Workspace business logic lives in apps.journal.workspace_services so it can
# be reused by user-facing CRUD endpoints (apps/journal/workspace_views.py).
# These aliases preserve the original local names used throughout this file.
from apps.journal.workspace_services import (
    WORKSPACE_LIMIT,
    workspace_name_reserved_error,
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
        reserved_error = workspace_name_reserved_error(name)
        if reserved_error is not None:
            return Response(
                {"error": "reserved_prefix", "detail": reserved_error},
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
            reserved_error = workspace_name_reserved_error(new_name)
            if reserved_error is not None:
                return Response(
                    {"error": "reserved_prefix", "detail": reserved_error},
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
        except IntegrationAccessError as exc:
            return _integration_error_response(exc, provider="Reddit", tenant=tenant)
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
        except IntegrationAccessError as exc:
            return _integration_error_response(exc, provider="Reddit", tenant=tenant)
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
        # ORDER MATTERS: IntegrationAccessError subclasses RuntimeError. It
        # MUST stay above the `except RuntimeError` arm below — put it after
        # and this arm is unreachable dead code, and an integration failure
        # gets mislabeled to the model as a 400 tool_error with no user_action,
        # i.e. "you did something wrong" for a fault the user cannot fix.
        except IntegrationAccessError as exc:
            return _integration_error_response(exc, provider="Reddit", tenant=tenant)
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
        except Exception as exc:
            logger.exception("Reddit tool execution failed for tenant %s action=%s", tenant_id, action)
            return Response(
                {"error": "tool_execution_failed", "detail": str(exc)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        from apps.pii.redactor import redact_tool_response

        result = redact_tool_response(result, tenant)

        return Response(result, status=status.HTTP_200_OK)


# ── sautai Phase 0 (nbhd-sautai-tools plugin, meal-plan generation) ──────
#
# PROXY-THROUGH-DJANGO, same shape as RedditToolView: the plugin never talks
# to sautai directly (the platform secret stays server-side, and a container-
# direct call would bypass the PII rehydrate/redact chokepoint). This view
# only does the fast NBHD ack — job row + QStash enqueue; the sautai POST
# (legacy synchronous during transition) and bounded post-202 status polling
# live in apps.integrations.tasks. See
# docs/sautai-phase0-contract.md.

_SAUTAI_MAX_PROMPT_CHARS = 2000
_SAUTAI_CONFIRM_TOKEN_MAX_AGE_SECONDS = 10 * 60
_SAUTAI_CONFIRM_TOKEN_SALT = "apps.integrations.sautai.generate-confirm.v1"


def _sautai_tool_parameters(
    *,
    week_start: date,
    number_of_days: int,
    user_prompt: str,
    regenerate: bool,
    confirm_replace: bool,
) -> dict:
    """Return the normalized parameters an agent must replay after approval."""
    parameters: dict = {
        "week_start": week_start.isoformat(),
        "number_of_days": number_of_days,
    }
    if user_prompt:
        parameters["user_prompt"] = user_prompt
    if regenerate:
        parameters["regenerate"] = True
    if confirm_replace:
        parameters["confirm_replace"] = True
    return parameters


def _sautai_confirmation_context(*, tenant: Tenant, request_payload: dict, tool_parameters: dict) -> dict:
    return {
        "tenant_id": str(tenant.id),
        "request": request_payload,
        "tool_parameters": tool_parameters,
    }


def _sautai_confirmation_digest(context: dict) -> str:
    canonical = json.dumps(context, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _sautai_issue_confirm_token(context: dict) -> str:
    digest = _sautai_confirmation_digest(context)
    return signing.TimestampSigner(salt=_SAUTAI_CONFIRM_TOKEN_SALT).sign(digest)


def _sautai_confirm_token_failure(confirm_token: str, context: dict) -> str | None:
    """Return an agent-facing failure reason, or ``None`` for a valid token."""
    if len(confirm_token) > 512:
        return "invalid"
    try:
        signed_digest = signing.TimestampSigner(salt=_SAUTAI_CONFIRM_TOKEN_SALT).unsign(
            confirm_token,
            max_age=_SAUTAI_CONFIRM_TOKEN_MAX_AGE_SECONDS,
        )
    except signing.SignatureExpired:
        return "expired"
    except signing.BadSignature:
        return "invalid"
    if not hmac.compare_digest(signed_digest, _sautai_confirmation_digest(context)):
        return "mismatch"
    return None


def _sautai_display_date(value: date) -> str:
    return f"{value.strftime('%A, %B')} {value.day}, {value.year}"


def _sautai_confirmation_preview(
    *,
    tenant: Tenant,
    week_start: date,
    request_payload: dict,
    tool_parameters: dict,
    user_prompt: str,
    reason: str,
) -> Response:
    week_end = week_start + timedelta(days=6)
    week_label = f"{_sautai_display_date(week_start)} through {_sautai_display_date(week_end)}"
    prompt_line = (
        f"\nPrompt/preferences (verbatim):\n{user_prompt}" if user_prompt else "\nPrompt/preferences: none provided."
    )
    confirmation_message = (
        f"Send this meal-plan request to sautai for {week_label}?{prompt_line}\nDoes this look correct to send?"
    )
    context = _sautai_confirmation_context(
        tenant=tenant,
        request_payload=request_payload,
        tool_parameters=tool_parameters,
    )
    return Response(
        {
            "status": "confirmation_required",
            "confirmation_reason": reason,
            "preview": {
                "week_start": week_start.isoformat(),
                "week_end": week_end.isoformat(),
                "week_label": week_label,
                "request": request_payload,
                "tool_parameters": tool_parameters,
                "confirmation_message": confirmation_message,
            },
            "confirm_token": _sautai_issue_confirm_token(context),
            "confirm_token_expires_in_seconds": _SAUTAI_CONFIRM_TOKEN_MAX_AGE_SECONDS,
            "guidance": (
                "Present preview.confirmation_message to the user and wait for explicit verification. "
                "Do not send anything to sautai yet. After the user says it looks correct, call "
                "nbhd_generate_meal_plan again with this confirm_token and the identical "
                "preview.tool_parameters, including the explicit week_start."
            ),
        }
    )


def _sautai_existing_plan_payload(status_value: str, job: SautaiMealPlanJob, guidance: str) -> dict:
    return {
        "status": status_value,
        "week_start": job.week_start.isoformat() if job.week_start else "",
        "plan": job.result or {},
        "web_link": job.web_link,
        "guidance": guidance,
    }


def _sautai_missing_days(funnel: object) -> list:
    if not isinstance(funnel, dict):
        return []
    missing_days = funnel.get("missing_days")
    return missing_days if isinstance(missing_days, list) else []


def _sautai_plan_is_incomplete(job: SautaiMealPlanJob) -> bool:
    funnel = job.funnel if isinstance(job.funnel, dict) else {}
    return funnel.get("complete") is False or bool(_sautai_missing_days(funnel))


def _sautai_repair_guidance(missing_days: list) -> str:
    dates = ", ".join(str(day) for day in missing_days)
    date_detail = f" ({dates})" if dates else ""
    return f"The missing days{date_detail} are being filled in. Existing meals will be left untouched."


def _sautai_link_required_response() -> Response:
    from apps.integrations.sautai_client import SAUTAI_LINK_REQUIRED_DETAIL

    return Response(
        {
            "error": "sautai_link_required",
            "detail": SAUTAI_LINK_REQUIRED_DETAIL,
        },
        status=status.HTTP_409_CONFLICT,
    )


class RuntimeSautaiGeneratePlanView(APIView):
    """POST — preview, then kick off an async sautai meal-plan generation.

    A no-token call returns the exact explicit-week request and a short-lived
    confirmation token without side effects. A matching confirmed call is a
    fast ack (<20s): it creates a PENDING job and enqueues via QStash, which
    advances sautai's asynchronous generation job through bounded deliveries.
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

        if not getattr(tenant, "sautai_enabled", False):
            # Flips immediately via a tenant settings update — no container
            # restart needed to gate the runtime call, mirrors fuel_disabled.
            return Response({"error": "sautai_disabled"}, status=status.HTTP_409_CONFLICT)

        # Fail loud BEFORE creating a job: if the M2M bridge env is unset the
        # QStash worker could only ever mark the job FAILED with no push, so the
        # user would be promised a plan that never arrives. Surfacing it here
        # lets the tool tell them "sautai integration is not configured" instead.
        from apps.integrations.sautai_client import (
            ASYNC_CONTRACT_REQUEST_DECISION_KEY,
            async_generation_state,
            build_sautai_generate_payload,
            sautai_async_contract_confirmed,
            sautai_identity,
            sautai_m2m_config,
        )

        identity, _integration = sautai_identity(tenant)
        if not identity:
            return _sautai_link_required_response()

        base_url, secret = sautai_m2m_config()
        if not base_url or not secret:
            return Response(
                {"error": "sautai_not_configured", "detail": "sautai integration is not configured"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        raw_user_prompt = request.data.get("user_prompt")
        user_prompt = "" if raw_user_prompt is None else str(raw_user_prompt)
        if len(user_prompt) > _SAUTAI_MAX_PROMPT_CHARS:
            return Response(
                {
                    "error": "invalid_request",
                    "detail": f"user_prompt exceeds {_SAUTAI_MAX_PROMPT_CHARS} characters",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            week_start = _resolve_sautai_week_start(request.data, tenant, propose_if_omitted=True)
        except ValueError as exc:
            return Response(
                {"error": "invalid_request", "detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            number_of_days = _parse_positive_int(
                request.data.get("number_of_days"),
                default=7,
                max_value=7,
            )
        except ValueError as exc:
            return Response(
                {"error": "invalid_request", "detail": f"number_of_days {exc}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            # This remains destructive under the legacy 200 contract and
            # becomes fill-only only after a valid 202 job survives one signed
            # status decode.
            # Explicit replace_slots are not exposed by the NBHD tool contract.
            regenerate = _parse_bool(request.data.get("regenerate"), default=False)
            confirm_replace = _parse_bool(request.data.get("confirm_replace"), default=False)
        except ValueError as exc:
            return Response(
                {"error": "invalid_request", "detail": f"regenerate/confirm_replace {exc}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        tool_parameters = _sautai_tool_parameters(
            week_start=week_start,
            number_of_days=number_of_days,
            user_prompt=user_prompt,
            regenerate=regenerate,
            confirm_replace=confirm_replace,
        )
        request_payload = build_sautai_generate_payload(
            sautai_user_id=identity["sautai_user_id"],
            week_start=week_start,
            number_of_days=number_of_days,
            user_prompt=user_prompt,
            regenerate=regenerate,
        )
        confirmation_context = _sautai_confirmation_context(
            tenant=tenant,
            request_payload=request_payload,
            tool_parameters=tool_parameters,
        )
        confirm_token = str(request.data.get("confirm_token") or "").strip()
        confirmation_failure = "missing"
        if confirm_token:
            raw_confirmed_week = request.data.get("week_start")
            if raw_confirmed_week in (None, ""):
                confirmation_failure = "explicit_week_required"
            elif str(raw_confirmed_week) != week_start.isoformat():
                confirmation_failure = "mismatch"
            else:
                confirmation_failure = _sautai_confirm_token_failure(confirm_token, confirmation_context)
        if confirmation_failure is not None:
            return _sautai_confirmation_preview(
                tenant=tenant,
                week_start=week_start,
                request_payload=request_payload,
                tool_parameters=tool_parameters,
                user_prompt=user_prompt,
                reason=confirmation_failure,
            )

        # One snapshot is the decision for this request: it controls both this
        # confirmation branch and the worker's POST timeout after being written
        # to the job below. The global signal flips only after a valid 202 plus
        # one valid signed status poll, and reverts on a later legacy 200.
        async_contract_live = sautai_async_contract_confirmed()

        # Atomic coalesce: lock the tenant row so concurrent POSTs (the agent
        # retrying after its OWN 20s tool-call timeout, while the first
        # request already created the job, is the realistic trigger — not
        # just a theoretical race) cannot both pass the in-flight check and
        # create duplicate jobs, which would fire duplicate "plan ready"
        # pushes once each finishes. Mirrors
        # apps.core.views.CoreComposeView's compose-coalesce guard. Scoped to
        # (tenant, week_start) — that's how a user perceives "my request";
        # a different week is a genuinely different ask.
        with transaction.atomic():
            Tenant.objects.select_for_update().get(pk=tenant.pk)

            repairing_incomplete_plan = False
            repair_missing_days: list = []

            existing_ready = (
                SautaiMealPlanJob.objects.filter(
                    tenant=tenant,
                    week_start=week_start,
                    status=SautaiMealPlanJobStatus.READY,
                )
                .order_by("-updated_at")
                .first()
            )

            if not async_contract_live:
                if regenerate and existing_ready is None:
                    # Rebuilding a nonexistent plan is just ordinary generation.
                    # Strip the destructive flag even if the caller supplied it.
                    regenerate = False
                elif regenerate and not confirm_replace:
                    return Response(
                        _sautai_existing_plan_payload(
                            "confirm_required",
                            existing_ready,
                            "Show the current plan and ask the user to explicitly confirm replacing it before regenerating.",
                        )
                    )
                elif not regenerate and existing_ready is not None:
                    if _sautai_plan_is_incomplete(existing_ready):
                        repairing_incomplete_plan = True
                        repair_missing_days = _sautai_missing_days(existing_ready.funnel)
                    else:
                        guidance = (
                            "Surface the existing plan. The new guidance was not applied; offer regeneration and require "
                            "explicit confirmation before replacing it."
                            if user_prompt
                            else "A plan already exists for this week. Surface the existing plan. Offer regeneration only "
                            "if the user seems to want a new one, and require explicit confirmation before replacing it."
                        )
                        return Response(
                            _sautai_existing_plan_payload(
                                "exists",
                                existing_ready,
                                guidance,
                            )
                        )
            else:
                if regenerate and existing_ready is None:
                    # Filling gaps in a nonexistent plan is ordinary generation.
                    regenerate = False
                elif existing_ready is not None:
                    if _sautai_plan_is_incomplete(existing_ready):
                        repairing_incomplete_plan = True
                        repair_missing_days = _sautai_missing_days(existing_ready.funnel)
                    else:
                        if regenerate:
                            guidance = (
                                "A complete plan already exists for this week. Regeneration is fill-only, so every occupied "
                                "meal remains untouched. Surface the existing plan."
                            )
                        elif user_prompt:
                            guidance = (
                                "Surface the existing plan. The new guidance was not applied because regeneration only "
                                "fills missing slots and leaves occupied meals untouched."
                            )
                        else:
                            guidance = "A complete plan already exists for this week. Surface the existing plan."
                        return Response(
                            _sautai_existing_plan_payload(
                                "exists",
                                existing_ready,
                                guidance,
                            )
                        )

            in_flight = (
                SautaiMealPlanJob.objects.filter(
                    tenant=tenant,
                    week_start=week_start,
                    status__in=[SautaiMealPlanJobStatus.PENDING, SautaiMealPlanJobStatus.GENERATING],
                )
                .order_by("-created_at")
                .first()
            )
            if in_flight is None:
                job = SautaiMealPlanJob.objects.create(
                    tenant=tenant,
                    week_start=week_start,
                    number_of_days=number_of_days,
                    user_prompt=user_prompt,
                    regenerate=regenerate,
                    funnel={ASYNC_CONTRACT_REQUEST_DECISION_KEY: async_contract_live},
                )

        # publish_task is a network call — enqueue AFTER the txn commits
        # (invariant §8: no external calls inside atomic).
        from apps.cron.publish import publish_task

        if in_flight is not None:
            # Coalesce onto the existing request. RECOVERY: if it's a PENDING row
            # that has gone stale, its original publish_task may have been
            # swallowed on enqueue — and because EVERY later request coalesces
            # onto it, that (tenant, week) would be dead forever with no
            # user-visible way out. Re-enqueue it (best-effort); the every-minute
            # recovery cron is the guaranteed backstop. Generation-token claims
            # make a redundant delivery harmless if the original DID land and
            # the worker is merely slow; a GENERATING row is left alone here.
            if in_flight.status == SautaiMealPlanJobStatus.PENDING and (tz.now() - in_flight.updated_at) > timedelta(
                minutes=3
            ):
                try:
                    state = async_generation_state(in_flight)
                    poll_generation = state.get("poll_generation") if isinstance(state, dict) else None
                    if isinstance(poll_generation, int) and not isinstance(poll_generation, bool):
                        publish_task(
                            "generate_sautai_meal_plan",
                            str(in_flight.id),
                            poll_generation=poll_generation,
                        )
                    else:
                        publish_task("generate_sautai_meal_plan", str(in_flight.id))
                except Exception:
                    logger.warning("Failed to re-enqueue stale sautai job %s", in_flight.id)

            coalesced = {
                "job_id": str(in_flight.id),
                "status": in_flight.status,
                "week_start": week_start.isoformat(),
            }
            if repairing_incomplete_plan:
                coalesced.update(
                    {
                        "repairing_incomplete_plan": True,
                        "repairing_missing_days": repair_missing_days,
                        "guidance": _sautai_repair_guidance(repair_missing_days),
                    }
                )
            # Honesty guard: this request carried NEW guidance (regenerate, or a
            # user_prompt that DIFFERS from the in-flight job's) but coalesced onto
            # a generation that does NOT include it — the guidance is being dropped.
            # An IDENTICAL user_prompt is the plugin retrying the same body after its
            # own timeout (the case coalesce exists for) — the in-flight job already
            # carries that guidance, so it is NOT a false "not applied".
            if regenerate or (user_prompt and user_prompt != in_flight.user_prompt):
                coalesced["request_applied"] = False
            return Response(coalesced)

        try:
            publish_task("generate_sautai_meal_plan", str(job.id))
        except Exception:
            # A swallowed publish leaves this PENDING; the stale-re-enqueue branch
            # above recovers it on the user's next request for the same week —
            # closing the inherited compose_meditation/CoreComposeView gap.
            logger.warning("Failed to enqueue sautai meal-plan generation for job %s", job.id)

        ack = {"job_id": str(job.id), "status": job.status, "week_start": week_start.isoformat()}
        if repairing_incomplete_plan:
            ack.update(
                {
                    "repairing_incomplete_plan": True,
                    "repairing_missing_days": repair_missing_days,
                    "guidance": _sautai_repair_guidance(repair_missing_days),
                }
            )
        return Response(ack, status=status.HTTP_201_CREATED)


class RuntimeSautaiCurrentPlanView(APIView):
    """POST — read the user's current sautai meal plan (fast, synchronous).

    Unlike generate (fire-and-forget via QStash), this is a read the plugin
    waits on inside its own 20s tool budget: it calls sautai's ``/current/``
    endpoint with a short (~10s) timeout. On a sautai timeout/transport error it
    falls back to the most recent READY ``SautaiMealPlanJob``'s cached plan for
    the same week (NBHD is a display cache; sautai stays source of truth) and
    flags the response ``cached: true``. See docs/sautai-phase0-contract.md #2.
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

        if not getattr(tenant, "sautai_enabled", False):
            return Response({"error": "sautai_disabled"}, status=status.HTTP_409_CONFLICT)

        from apps.integrations.sautai_client import (
            clear_sautai_link,
            fetch_sautai_current_plan,
            sautai_identity,
            sautai_m2m_config,
        )

        identity, integration = sautai_identity(tenant)
        if not identity:
            return _sautai_link_required_response()

        base_url, secret = sautai_m2m_config()
        if not base_url or not secret:
            return Response(
                {"error": "sautai_not_configured", "detail": "sautai integration is not configured"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        try:
            week_start = _resolve_sautai_week_start(request.data, tenant)
        except ValueError as exc:
            return Response(
                {"error": "invalid_request", "detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        generation_in_progress = _sautai_generation_in_progress(tenant, week_start)

        def response_payload(payload: dict) -> dict:
            if generation_in_progress is not None:
                payload["generation_in_progress"] = generation_in_progress
            return payload

        # Identity is derived SERVER-SIDE from the linked sautai_user_id — NEVER
        # from the plugin payload (an agent-supplied id would be an injection
        # vector).
        result = fetch_sautai_current_plan(identity=identity, week_start_iso=week_start.isoformat())
        outcome = result.get("outcome")

        if outcome == "link_required":
            if integration is not None:
                clear_sautai_link(integration)
            return _sautai_link_required_response()

        if outcome == "ok":
            return Response(
                response_payload(
                    {
                        "status": "ok",
                        "cached": False,
                        "week_start": week_start.isoformat(),
                        "plan": result.get("plan"),
                        "complete": result.get("complete"),
                        "missing_days": result.get("missing_days"),
                        "web_link": result.get("web_link", ""),
                        "funnel": result.get("funnel", {}),
                    }
                )
            )

        if outcome == "not_found":
            return Response(response_payload({"status": "no_plan", "week_start": week_start.isoformat()}))

        if outcome == "not_configured":
            # Belt-and-suspenders (we checked above; env could race a reload).
            return Response(
                {"error": "sautai_not_configured", "detail": "sautai integration is not configured"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        # outcome == "error": sautai timed out / errored. Show the last plan NBHD
        # cached for this week if we have one, flagged stale, rather than nothing.
        cached_job = (
            SautaiMealPlanJob.objects.filter(
                tenant=tenant,
                week_start=week_start,
                status=SautaiMealPlanJobStatus.READY,
            )
            .order_by("-updated_at")
            .first()
        )
        if cached_job is not None and cached_job.result:
            cached_funnel = cached_job.funnel if isinstance(cached_job.funnel, dict) else {}
            return Response(
                response_payload(
                    {
                        "status": "ok",
                        "cached": True,
                        "week_start": week_start.isoformat(),
                        "plan": cached_job.result,
                        "complete": cached_funnel.get("complete"),
                        "missing_days": cached_funnel.get("missing_days"),
                        "web_link": cached_job.web_link,
                        "funnel": cached_funnel,
                        "detail": "sautai was unreachable; showing the last plan NBHD cached for this week.",
                    }
                )
            )

        if generation_in_progress is not None:
            # The active job is the most useful truth even if sautai's fast read
            # briefly failed. Keep this a successful tool response so the plugin
            # can explain the async wait instead of reducing it to a 502.
            return Response(
                response_payload(
                    {
                        "status": "no_plan",
                        "week_start": week_start.isoformat(),
                        "detail": "Meal-plan generation is still in progress.",
                    }
                )
            )

        return Response(
            {"error": "sautai_unavailable", "detail": result.get("detail", "sautai could not be reached")},
            status=status.HTTP_502_BAD_GATEWAY,
        )


# ── Typed cron creation (feat/cron-typed-patterns) ──────────────────────
#
# Three runtime endpoints — one per agent-creatable pattern. Each maps to
# the pattern's Pydantic payload schema. See CONTINUITY_cron-typed-patterns.md
# for why we split per-pattern (concrete tool schemas beat discriminated
# unions in real-world model behaviour).
#
# Imports of services + handler symbols are intentionally LOCAL inside
# each method, matching the pattern in this file (see
# ``feedback_local_reimport_pattern.md``).


class _RuntimeCronCreateBase(KnownValueResponseGuardMixin, APIView):
    """Common boilerplate for typed cron creation endpoints.

    Subclasses set ``pattern`` (CronPattern value) and ``_extract_payload``
    (turns request.data into the pattern's typed_payload dict).
    """

    permission_classes = [AllowAny]
    authentication_classes = []
    pii_egress_seam = "cron_create_runtime_response"
    pii_egress_text_fields = _CRON_EGRESS_TEXT_FIELDS

    pattern: str = ""

    def _extract_payload(self, request) -> dict:
        raise NotImplementedError

    def post(self, request, tenant_id):
        from apps.cron.services import (
            CronNameConflictError,
            TypedCronError,
            create_typed_cron,
        )

        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure
        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        blocked = assert_write_allowed_for_document_turn(tenant)
        if blocked is not None:
            return blocked

        name = (request.data.get("name") or "").strip()
        schedule = request.data.get("schedule") or {}
        try:
            payload = self._extract_payload(request)
        except (TypeError, ValueError) as exc:
            return Response(
                {"error": "invalid_payload", "detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            cron = create_typed_cron(
                tenant=tenant,
                pattern=self.pattern,
                typed_payload=payload,
                name=name,
                schedule=schedule,
            )
        except CronNameConflictError as exc:
            return Response(
                {"error": exc.code, "detail": str(exc), "name": exc.name},
                status=status.HTTP_409_CONFLICT,
            )
        except TypedCronError as exc:
            return Response(
                {"error": exc.code, "detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception as exc:
            # Pydantic ValidationError, etc. — surface with the message so
            # the agent can correct its call.
            return Response(
                {"error": "validation_failed", "detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            {
                "tenant_id": str(tenant.id),
                "cron": {
                    "id": str(cron.pk),
                    "name": cron.name,
                    "pattern": cron.pattern,
                    "schedule": (cron.data or {}).get("schedule"),
                    "managed": cron.managed,
                    "gateway_job_id": cron.gateway_job_id or None,
                },
            },
            status=status.HTTP_201_CREATED,
        )


class RuntimeCronCreatePureReminderView(_RuntimeCronCreateBase):
    """POST /runtime/<tenant_id>/crons/pure_reminder/

    Body: {name, schedule, text}
    """

    pattern = "pure_reminder"

    def _extract_payload(self, request) -> dict:
        return {"text": request.data.get("text", "")}


class RuntimeCronCreateQuoteUserIntentView(_RuntimeCronCreateBase):
    """POST /runtime/<tenant_id>/crons/quote_user_intent/

    Body: {name, schedule, text, refresh_facts_via?}
    """

    pattern = "quote_user_intent"

    def _extract_payload(self, request) -> dict:
        payload = {"text": request.data.get("text", "")}
        refresh = request.data.get("refresh_facts_via")
        if refresh:
            payload["refresh_facts_via"] = refresh
        return payload


class RuntimeCronCreateDomainSummaryView(_RuntimeCronCreateBase):
    """POST /runtime/<tenant_id>/crons/domain_summary/

    Body: {name, schedule, query_tool, query_args, render_block}
    """

    pattern = "domain_summary"

    def _extract_payload(self, request) -> dict:
        return {
            "query_tool": request.data.get("query_tool", ""),
            "query_args": request.data.get("query_args") or {},
            "render_block": request.data.get("render_block", ""),
        }


# ── Neighborhood (Friends) agent-facing runtime endpoints (design §3.5) ───────
#
# No runtime endpoint accepts a foreign tenant_id: the agent calls only
# runtime/<its own tid>/…; any cross-tenant reference is validated against an
# accepted Friendship for THAT tenant, and only frozen scrubbed rows are
# returned. The chokepoint test scans this module, so cross-tenant reads route
# through apps/friends/access.py (Friendship/NeighborProfile edge checks and the
# Lesson load below are per-tenant, not the confined cross-tenant managers).


class RuntimeProposeShareView(APIView):
    """POST runtime/<tid>/lessons/<lesson_id>/propose-share/

    The agent proposes sharing an EXISTING star to a neighbor. Body:
    ``{target_friendship_id | target_handle, source_context?}``. Creates a
    ``PendingShare(proposed_by="agent")`` (+ ensures the SharedLesson + enqueues
    the fail-closed scrub so the preview is ready) — and NEVER a grant. A human
    approve is the only path that publishes.
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request, tenant_id, lesson_id):
        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure
        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure
        if not getattr(tenant, "friends_agent_propose_enabled", False):
            return Response(
                {"error": "propose_disabled", "detail": "Agent proposing is off for this account (absorb-only)."},
                status=status.HTTP_403_FORBIDDEN,
            )

        from rest_framework.exceptions import PermissionDenied as DRFPermissionDenied

        from apps.friends import services as friends_services
        from apps.lessons.models import Lesson

        lesson = Lesson.objects.filter(id=lesson_id, tenant=tenant).first()
        if lesson is None:
            return Response({"error": "lesson_not_found"}, status=status.HTTP_404_NOT_FOUND)

        data = request.data if isinstance(request.data, dict) else {}
        circle = friends_services.resolve_member_circle(tenant, data.get("target_circle_id"))
        friendship = None
        if circle is None:
            friendship = friends_services.resolve_accepted_friendship(
                tenant, friendship_id=data.get("target_friendship_id"), handle=data.get("target_handle")
            )
            if friendship is None:
                return Response(
                    {"error": "not_neighbors", "detail": "No accepted friendship or circle for the given target."},
                    status=status.HTTP_403_FORBIDDEN,
                )

        try:
            pending, created = friends_services.propose_share(
                tenant,
                lesson,
                friendship,
                data.get("source_context") or data.get("why") or "",
                circle=circle,
            )
        except DRFPermissionDenied as exc:
            # Mechanical share-never list (gravity/core lessons stay private).
            return Response({"error": "pillar_blocked", "detail": str(exc)}, status=status.HTTP_403_FORBIDDEN)

        return Response(
            {
                "pending_share_id": str(pending.id),
                "status": pending.status,
                "created": created,
                "note": "proposal only — a human must approve before anything is shared",
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class RuntimeNeighborhoodContextView(APIView):
    """GET runtime/<tid>/neighborhood/context/?since=<iso>

    The absorb READ side: accessor-approved scrubbed sparks shared TO the tenant
    (one-liners + owner handle + shared_lesson_id), each logged to AbsorbedItem
    on first sight. The response ``cursor`` is the caller's next ``since`` — the
    idempotent ledger + this cursor make a repeat call a no-op, so no separate
    ``absorb-ack`` endpoint is needed (fewer endpoints > symmetric API).
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

        from django.utils.dateparse import parse_datetime

        from apps.friends import services as friends_services

        raw_since = request.query_params.get("since")
        since = parse_datetime(raw_since) if raw_since else None
        return Response(friends_services.neighborhood_context(tenant, since=since))


class RuntimeMissionsView(KnownValueResponseGuardMixin, APIView):
    """GET runtime/<tid>/missions/ — the tid's own Missions + crew projection, so
    the agent can nudge ITS OWN human toward showing up."""

    permission_classes = [AllowAny]
    authentication_classes = []
    pii_egress_seam = "friends_missions_runtime_response"
    pii_egress_text_fields = _MISSION_EGRESS_TEXT_FIELDS

    def get(self, request, tenant_id):
        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure
        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        from apps.friends import services as friends_services

        return Response({"missions": friends_services.runtime_missions(tenant)})


class RuntimeProposeMissionTaskView(APIView):
    """POST runtime/<tid>/missions/<mission_id>/propose-task/ {title, description?,
    due_date?} — the agent proposes ONE Mission task for ITS OWN human →
    PendingGoalAction (human-gated). Never writes another human's task: the
    proposing tid must be an active member, and the task is minted for THAT
    member only on approve."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request, tenant_id, mission_id):
        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure
        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure
        if not getattr(tenant, "friends_agent_propose_enabled", False):
            return Response(
                {"error": "propose_disabled", "detail": "Agent proposing is off for this account (absorb-only)."},
                status=status.HTTP_403_FORBIDDEN,
            )

        from django.utils.dateparse import parse_date

        from apps.friends import services as friends_services

        data = request.data if isinstance(request.data, dict) else {}
        raw_due = data.get("due_date")
        action, created = friends_services.propose_mission_task(
            tenant,
            mission_id,
            title=data.get("title", ""),
            description=data.get("description", ""),
            due_date=parse_date(raw_due) if raw_due else None,
        )
        return Response(
            {
                "pending_goal_action_id": str(action.id),
                "created": created,
                "note": "proposal only — your human must approve before it becomes a task",
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


# ── Document information-keeping (provenance ledger + keep manifest + forget) ─
#
# Backs the nbhd-document-keep plugin's three tools. The keep endpoint VALIDATES
# every artifact against a tenant-owned row of a registered type before recording
# (D2/D4); forget is a server-side ORM/gateway fan-out via the shared service so
# the agent path and the console path can't drift (D3). Business logic lives in
# apps.journal.document_ingestion; these views are thin HMAC-authed wrappers.


class RuntimeDocumentKeepView(APIView):
    """POST — record a validated document-ingestion manifest (nbhd_document_keep)."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request, tenant_id):
        from apps.journal.document_ingestion import record_keep

        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        data = request.data if isinstance(request.data, dict) else {}
        source = data.get("source") if isinstance(data.get("source"), dict) else {}
        artifacts = data.get("artifacts") if isinstance(data.get("artifacts"), list) else []

        result = record_keep(tenant, source=source, artifacts=artifacts)
        return Response(
            {"tenant_id": str(tenant.id), **result},
            status=status.HTTP_201_CREATED if result.get("recorded") else status.HTTP_200_OK,
        )


class RuntimeDocumentIngestionsView(APIView):
    """GET — recent ingestions + artifacts (nbhd_document_list_ingestions)."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, tenant_id):
        from apps.journal.document_ingestion import list_ingestions

        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        try:
            limit = int(request.query_params.get("limit", 20))
        except (TypeError, ValueError):
            limit = 20

        result = list_ingestions(tenant, limit=limit)
        return Response({"tenant_id": str(tenant.id), **result}, status=status.HTTP_200_OK)


class RuntimeDocumentForgetView(APIView):
    """POST — remove every item recorded from one ingestion (nbhd_document_forget)."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request, tenant_id, ingestion_id):
        from apps.journal.document_ingestion import forget_ingestion

        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        result = forget_ingestion(tenant, ingestion_id)
        if result.get("error") == "not_found":
            return Response(
                {"error": "not_found", "detail": "No such document ingestion for this tenant."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response({"tenant_id": str(tenant.id), **result}, status=status.HTTP_200_OK)


# ── Journal shaping (default daily-note template only) ──────────────────────


def _journal_shaping_forbidden(tenant) -> Response | None:
    if getattr(tenant, "journal_shaping_enabled", False):
        return None
    return Response(
        {"error": "journal_shaping_disabled"},
        status=status.HTTP_403_FORBIDDEN,
    )


def _journal_template_payload(template) -> dict:
    return {
        "name": template.name,
        "sections": template.sections,
    }


def _journal_sections_validation_error(detail: str) -> Response:
    return Response(
        {"error": "validation_error", "detail": detail},
        status=status.HTTP_400_BAD_REQUEST,
    )


def _validate_journal_shaping_sections(sections) -> list[dict[str, str]]:
    if not isinstance(sections, list):
        raise DjangoValidationError("sections must be an array.")
    if len(sections) > 12:
        raise DjangoValidationError("sections must contain at most 12 items.")

    serialized = json.dumps(sections, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(serialized) > 20 * 1024:
        raise DjangoValidationError("sections payload must be at most 20KB.")

    for index, section in enumerate(sections):
        if not isinstance(section, dict):
            continue
        slug = str(section.get("slug") or "")
        title = str(section.get("title") or "")
        content = str(section.get("content") or "")
        if len(slug) > 64:
            raise DjangoValidationError(f"section index={index} slug must be at most 64 characters.")
        if len(title) > 120:
            raise DjangoValidationError(f"section index={index} title must be at most 120 characters.")
        if len(content) > 4000:
            raise DjangoValidationError(f"section index={index} content must be at most 4000 characters.")

    return _validate_template_sections(sections)


class RuntimeJournalTemplateView(APIView):
    """GET — return the tenant's default daily-note template."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, tenant_id):
        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        flag_failure = _journal_shaping_forbidden(tenant)
        if flag_failure is not None:
            return flag_failure

        template = get_default_template(tenant=tenant)
        if template is None:
            template = seed_default_templates_for_tenant(tenant=tenant)["template"]
        return Response(_journal_template_payload(template), status=status.HTTP_200_OK)


class RuntimeJournalTemplateUpdateView(APIView):
    """POST — replace the sections on the tenant's default daily-note template."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request, tenant_id):
        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure

        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        flag_failure = _journal_shaping_forbidden(tenant)
        if flag_failure is not None:
            return flag_failure

        data = request.data if isinstance(request.data, dict) else {}
        try:
            sections = _validate_journal_shaping_sections(data.get("sections"))
        except DjangoValidationError as exc:
            return _journal_sections_validation_error(" ".join(exc.messages))

        template = get_default_template(tenant=tenant)
        if template is None:
            template = seed_default_templates_for_tenant(tenant=tenant)["template"]
        template.sections = sections
        template.save()

        try:
            from apps.cron.publish import publish_task

            publish_task("update_tenant_config", str(tenant.id))
        except Exception:
            pass

        return Response(_journal_template_payload(template), status=status.HTTP_200_OK)
