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
from pydantic import ValidationError as PydanticValidationError
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.billing.services import record_usage
from apps.common.tenant_tz import safe_zoneinfo, tenant_tz_name
from apps.common.windows import Window, resolve_window
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
from apps.journal.session_models import Session
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
    """Thin wrapper kept for callers in this module — use ``apps.common.tenant_tz``."""
    return tenant_tz_name(tenant)


def _tenant_now(tenant: Tenant) -> datetime:
    return tz.now().astimezone(safe_zoneinfo(tenant_tz_name(tenant)))


def _tenant_today(tenant: Tenant) -> date:
    return _tenant_now(tenant).date()


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


# ── Typed Goal/Task lifecycle (feat/journal-typed-lifecycle) ──────────────
#
# These endpoints back the nbhd_goal_*/nbhd_task_* tools in nbhd-journal-tools.
# Imports of Goal/Task/GoalSerializer/TaskSerializer are intentionally LOCAL
# inside each method — the lint-on-Edit hook reaps module-level imports that
# look unused at parse time. See ``feedback_local_reimport_pattern.md``.


class RuntimeGoalListCreateView(APIView):
    """List or create goals for a tenant runtime."""

    permission_classes = [AllowAny]
    authentication_classes = []

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

        serializer = GoalSerializer(data=request.data, context={"tenant": tenant})
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

        goal = serializer.save()
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

        serializer = GoalSerializer(goal, data=request.data, partial=True, context={"tenant": tenant})
        serializer.is_valid(raise_exception=True)
        serializer.save()
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


class RuntimeTaskListCreateView(APIView):
    """List or create tasks for a tenant runtime."""

    permission_classes = [AllowAny]
    authentication_classes = []

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

        serializer = TaskSerializer(data=request.data, context={"tenant": tenant})
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

        task = serializer.save()
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

        serializer = TaskSerializer(task, data=request.data, partial=True, context={"tenant": tenant})
        serializer.is_valid(raise_exception=True)
        serializer.save()
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


class RuntimeLessonCreateView(APIView):
    """Create lessons captured by the assistant for a tenant.

    Lessons are auto-approved on creation (status="approved") — they join the
    constellation immediately and get an embedding + connections, matching the
    journal-extraction approval path. Users prune unwanted lessons from the
    constellation UI rather than gating each one through an approval queue.
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
        from apps.journal.path_validation import validate_kind_slug

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

        # General path-component validation — closes the auto-create-on-get path
        # that previously seeded rows with NTFS-hostile kind/slug values.
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
        from apps.journal.path_validation import validate_kind_slug

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


class RuntimeDocumentAppendView(APIView):
    """POST append content to a document (agent access)."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request, tenant_id):
        from apps.journal.path_validation import validate_kind_slug

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


class RuntimeReconcileScanView(APIView):
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

    def get(self, request, tenant_id):
        from apps.fuel.models import BodyWeightLog, Workout, WorkoutStatus
        from apps.journal.models import Goal, Task

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

        candidates: list[dict] = []

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
                },
                "count": len(candidates),
                "candidates": candidates,
            },
            status=status.HTTP_200_OK,
        )


class RuntimeCronPhase2SummaryView(APIView):
    """POST /api/v1/integrations/runtime/<tenant_id>/cron-phase2-summary/

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


class _RuntimeCronCreateBase(APIView):
    """Common boilerplate for typed cron creation endpoints.

    Subclasses set ``pattern`` (CronPattern value) and ``_extract_payload``
    (turns request.data into the pattern's typed_payload dict).
    """

    permission_classes = [AllowAny]
    authentication_classes = []

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


class RuntimeCronPatternContextView(APIView):
    """GET /runtime/<tenant_id>/crons/<cron_name>/pattern_context/

    Returns (pattern, typed_payload, name, prompt_injection) for a typed
    cron — consumed by the nbhd-cron-enforcement plugin's
    ``cron_changed`` / ``before_prompt_build`` hooks so it can resolve
    the right validator + prompt addition for a firing cron.

    Returns 404 if the cron isn't typed or doesn't exist.
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, tenant_id, cron_name: str):
        from apps.cron.services import fetch_cron_pattern_context

        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure
        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        ctx = fetch_cron_pattern_context(tenant.id, cron_name)
        if ctx is None:
            return Response(
                {"error": "not_typed", "detail": "Cron is not a typed pattern."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(ctx, status=status.HTTP_200_OK)


class RuntimeCronGroundingView(APIView):
    """GET /runtime/<tenant_id>/crons/<cron_name>/grounding/

    Tells the nbhd-cron-enforcement plugin whether to inject the lightweight
    grounding rule into THIS firing cron, plus the rule text. Crons whose
    message already bakes the full grounding preamble (system seed jobs) are
    skipped (``inject=False``) to avoid double-injection; every other cron —
    typed patterns, user/freeform, legacy, agent-created, or unknown — gets
    ``inject=True`` so ALL custom crons ground on live state, not narration.
    See docs/grounding/cron-stale-status-grounding.md.
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, tenant_id, cron_name: str):
        from apps.cron.models import CronJob
        from apps.orchestrator.config_generator import CRON_GROUNDING_RULE, CRON_PREAMBLE_MARKER
        from apps.orchestrator.services import _SYSTEM_GENERATED_PREFIXES

        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure
        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        name = (cron_name or "").strip()
        # Platform-internal sync/fuel crons (_sync:/_fuel:) are not custom user
        # crons — skip (fuel also bakes the preamble; sync is ephemeral).
        if name.startswith(_SYSTEM_GENERATED_PREFIXES):
            return Response({"inject": False, "rule": ""}, status=status.HTTP_200_OK)
        row = CronJob.objects.filter(tenant=tenant, name=name).only("data").first()
        if row is not None and isinstance(row.data, dict):
            payload = row.data.get("payload")
            message = payload.get("message") if isinstance(payload, dict) else ""
            if isinstance(message, str) and CRON_PREAMBLE_MARKER in message:
                # Full preamble already baked into the message at seed time.
                return Response({"inject": False, "rule": ""}, status=status.HTTP_200_OK)
        return Response({"inject": True, "rule": CRON_GROUNDING_RULE}, status=status.HTTP_200_OK)


class RuntimeCronValidateOutboundView(APIView):
    """POST /runtime/<tenant_id>/crons/<cron_name>/validate_outbound/

    Called by the enforcement plugin's ``message_sending`` hook to
    validate that an outbound message satisfies the firing cron's
    pattern contract.

    Body: {content: str}
    Response: {ok: bool, reason?: str, fallback_content?: str}
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request, tenant_id, cron_name: str):
        from apps.cron.services import validate_typed_cron_outbound

        auth_failure = _internal_auth_or_401(request, tenant_id)
        if auth_failure is not None:
            return auth_failure
        tenant, tenant_failure = _load_tenant_or_404(tenant_id)
        if tenant_failure is not None or tenant is None:
            return tenant_failure

        content = request.data.get("content")
        if not isinstance(content, str):
            return Response(
                {"error": "invalid_payload", "detail": "content must be a string"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        result = validate_typed_cron_outbound(
            tenant_id=tenant.id,
            cron_name=cron_name,
            content=content,
        )
        return Response(result, status=status.HTTP_200_OK)
