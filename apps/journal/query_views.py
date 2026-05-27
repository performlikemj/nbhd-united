"""Parameterized query endpoint for the Journal domain.

POST ``/api/v1/journal/runtime/<tenant_id>/query/`` → ``JournalQueryView``.
Resources: ``entries``, ``tasks``, ``goals``.

Companion to the existing journal mutation endpoints (which live under
``apps.integrations.runtime_views`` for historical reasons); this module
is read-only and follows the finance pilot's add-don't-replace stance.

Tasks and Goals have multiple meaningful date axes (due_date vs created_at
vs completed_at). The request shape adds ``window_field`` so the agent can
say "tasks completed last month" vs "tasks due this week" with the same
``Window`` vocabulary. Per-resource defaults: ``entries.date``,
``tasks.due_date``, ``goals.target_date``.

See ``apps.common.query_view.BaseQueryView`` for the dispatch contract
(auth, RLS, window resolution, hashing, meta envelope).
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from django.db.models import Avg, Count, Max, Min, QuerySet, Sum
from django.utils import timezone as dj_tz
from pydantic import BaseModel, ConfigDict, Field

from apps.common.query_view import BaseQueryView, QueryExecutionError, jsonify
from apps.common.tenant_tz import tenant_tz
from apps.common.windows import Window
from apps.journal.models import Goal, JournalEntry, Task
from apps.tenants.models import Tenant

# ─── Request schema ────────────────────────────────────────────────────────


JournalResource = Literal["entries", "tasks", "goals"]
Aggregate = Literal["sum", "count", "avg", "min", "max"]


class JournalQueryRequest(BaseModel):
    """Strict request shape for ``nbhd_journal_query``.

    ``extra="forbid"`` — typos surface as 400 instead of silent no-ops.
    ``window_field`` lets the same Window resolve against different date
    columns per resource; left blank, each resource picks a default
    documented in ``_DEFAULT_WINDOW_FIELD``.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    resource: JournalResource
    window: Window | None = None
    window_field: str | None = None
    filter: dict[str, Any] = Field(default_factory=dict)
    fields: list[str] | None = None
    aggregate: Aggregate | None = None
    aggregate_field: str | None = None
    group_by: str | None = None
    order_by: str | None = None
    limit: int = Field(default=50, ge=1, le=500)


# ─── Field catalogues + filter handlers ────────────────────────────────────


_AGG_MAP = {"sum": Sum, "count": Count, "avg": Avg, "min": Min, "max": Max}

_IDENTIFIER = {"entries": "id", "tasks": "id", "goals": "id"}

# Fields the agent may request via ``fields``; omitted => all fields.
_ALLOWED_FIELDS = {
    "entries": {
        "id",
        "date",
        "mood",
        "energy",
        "wins",
        "challenges",
        "reflection",
        "created_at",
        "updated_at",
    },
    "tasks": {
        "id",
        "title",
        "description",
        "pillar",
        "status",
        "due_date",
        "completed_at",
        "parent_goal_id",
        "parent_task_id",
        "related_ref",
        "created_at",
        "updated_at",
    },
    "goals": {
        "id",
        "title",
        "description",
        "pillar",
        "status",
        "target",
        "target_date",
        "achieved_at",
        "parent_goal_id",
        "created_at",
        "updated_at",
    },
}

_ALLOWED_FILTERS = {
    "entries": {"mood", "energy"},
    "tasks": {"status", "pillar", "parent_goal_id", "has_due_date", "overdue"},
    "goals": {"status", "pillar", "parent_goal_id", "has_target_date"},
}

_ALLOWED_GROUP_BY = {
    "entries": {"energy", "mood"},
    "tasks": {"status", "pillar"},
    "goals": {"status", "pillar"},
}

# Resource → set of columns the agent may pick for ``window_field``.
# The default (used when ``window_field`` is None) is the first entry.
_ALLOWED_WINDOW_FIELDS = {
    "entries": ("date", "created_at"),
    "tasks": ("due_date", "created_at", "updated_at", "completed_at"),
    "goals": ("target_date", "created_at", "updated_at", "achieved_at"),
}

_DEFAULT_WINDOW_FIELD = {k: v[0] for k, v in _ALLOWED_WINDOW_FIELDS.items()}

_DEFAULT_ORDER_BY = {
    "entries": ("-date", "-created_at"),
    "tasks": ("status", "due_date", "-created_at"),
    "goals": ("status", "target_date", "-created_at"),
}

# Aggregate field whitelists are tiny — most journal fields are categorical,
# not numeric. ``count`` is always valid; sum/avg/min/max disallowed.
_NUMERIC_AGGREGATE_FIELDS: dict[str, set[str]] = {
    "entries": set(),
    "tasks": set(),
    "goals": set(),
}


# ─── The view ──────────────────────────────────────────────────────────────


class JournalQueryView(BaseQueryView):
    """Parameterized query for journal entries / tasks / goals.

    Subclasses ``BaseQueryView`` — base handles auth, RLS, window resolution,
    hashing, and the ``meta`` envelope. ``execute()`` here picks the right
    resource handler and returns ``(data, row_count)``.
    """

    query_model: ClassVar[type[BaseModel]] = JournalQueryRequest

    def execute(self, query, tenant: Tenant, window_resolved):
        self._validate_filter_keys(query)
        self._validate_fields(query)
        self._validate_group_by(query)
        self._validate_aggregate(query)
        self._validate_window_field(query)

        if query.resource == "entries":
            return self._execute_entries(query, tenant, window_resolved)
        if query.resource == "tasks":
            return self._execute_tasks(query, tenant, window_resolved)
        if query.resource == "goals":
            return self._execute_goals(query, tenant, window_resolved)

        raise QueryExecutionError("unknown_resource", f"unknown resource: {query.resource!r}")

    # ── Entries ────────────────────────────────────────────────────────

    def _execute_entries(self, query: JournalQueryRequest, tenant: Tenant, window_resolved):
        qs = JournalEntry.objects.filter(tenant=tenant)
        qs = self._apply_window(qs, query, window_resolved)

        f = query.filter
        if "mood" in f:
            mood = str(f["mood"]).strip()
            if mood:
                qs = qs.filter(mood__icontains=mood)
        if "energy" in f:
            qs = qs.filter(energy=f["energy"])

        if query.aggregate is not None:
            return self._aggregate(qs, "entries", query)

        qs = self._order(qs, query, "entries")
        rows = list(qs[: query.limit])
        data = [self._serialize_entry(e, query.fields) for e in rows]
        return data, len(data)

    # ── Tasks ──────────────────────────────────────────────────────────

    def _execute_tasks(self, query: JournalQueryRequest, tenant: Tenant, window_resolved):
        qs = Task.objects.filter(tenant=tenant)
        qs = self._apply_window(qs, query, window_resolved)

        f = query.filter
        if "status" in f:
            qs = qs.filter(status=f["status"])
        if "pillar" in f:
            qs = qs.filter(pillar=f["pillar"])
        if "parent_goal_id" in f:
            qs = qs.filter(parent_goal_id=f["parent_goal_id"])
        if "has_due_date" in f:
            qs = qs.exclude(due_date__isnull=bool(f["has_due_date"]))
        if "overdue" in f and f["overdue"]:
            today = dj_tz.now().astimezone(tenant_tz(tenant)).date()
            qs = qs.filter(due_date__lt=today).exclude(status__in=[Task.Status.DONE, Task.Status.SKIPPED])

        if query.aggregate is not None:
            return self._aggregate(qs, "tasks", query)

        qs = self._order(qs, query, "tasks")
        rows = list(qs[: query.limit])
        data = [self._serialize_task(t, query.fields) for t in rows]
        return data, len(data)

    # ── Goals ──────────────────────────────────────────────────────────

    def _execute_goals(self, query: JournalQueryRequest, tenant: Tenant, window_resolved):
        qs = Goal.objects.filter(tenant=tenant)
        qs = self._apply_window(qs, query, window_resolved)

        f = query.filter
        if "status" in f:
            qs = qs.filter(status=f["status"])
        if "pillar" in f:
            qs = qs.filter(pillar=f["pillar"])
        if "parent_goal_id" in f:
            qs = qs.filter(parent_goal_id=f["parent_goal_id"])
        if "has_target_date" in f:
            qs = qs.exclude(target_date__isnull=bool(f["has_target_date"]))

        if query.aggregate is not None:
            return self._aggregate(qs, "goals", query)

        qs = self._order(qs, query, "goals")
        rows = list(qs[: query.limit])
        data = [self._serialize_goal(g, query.fields) for g in rows]
        return data, len(data)

    # ── Window application ─────────────────────────────────────────────

    @staticmethod
    def _apply_window(qs: QuerySet, query: JournalQueryRequest, window_resolved) -> QuerySet:
        if window_resolved is None:
            return qs
        field = query.window_field or _DEFAULT_WINDOW_FIELD[query.resource]
        # DateTimeFields filter by date with __date lookup; DateFields take date directly.
        if field in {"created_at", "updated_at", "completed_at", "achieved_at"}:
            return qs.filter(**{f"{field}__date__gte": window_resolved[0], f"{field}__date__lte": window_resolved[1]})
        return qs.filter(**{f"{field}__gte": window_resolved[0], f"{field}__lte": window_resolved[1]})

    # ── Aggregation ────────────────────────────────────────────────────

    def _aggregate(self, qs: QuerySet, resource: str, query: JournalQueryRequest):
        agg_cls = _AGG_MAP[query.aggregate]
        if query.aggregate != "count" and not query.aggregate_field:
            raise QueryExecutionError(
                "aggregate_field_required",
                f"aggregate={query.aggregate} requires aggregate_field",
            )
        if query.aggregate == "count":
            agg_expr = agg_cls("id")
        else:
            allowed_numeric = _NUMERIC_AGGREGATE_FIELDS[resource]
            if query.aggregate_field not in allowed_numeric:
                raise QueryExecutionError(
                    "invalid_aggregate_field",
                    f"aggregate_field={query.aggregate_field!r} not allowed for resource={resource}; "
                    f"resource has no numeric columns — use aggregate='count' instead",
                )
            agg_expr = agg_cls(query.aggregate_field)

        if query.group_by:
            qs = qs.values(query.group_by).annotate(value=agg_expr, count=Count("id"))
            rows = list(qs)
            data = [
                {"group": {query.group_by: r[query.group_by]}, "value": r["value"], "count": r["count"]} for r in rows
            ]
            data = jsonify(data)
            return data, len(data)

        result = qs.aggregate(value=agg_expr, count=Count("id"))
        data = [{"value": jsonify(result["value"]) if result["value"] is not None else None, "count": result["count"]}]
        return data, 1

    # ── Ordering helper ────────────────────────────────────────────────

    def _order(self, qs: QuerySet, query: JournalQueryRequest, resource: str) -> QuerySet:
        if query.order_by:
            field = query.order_by.lstrip("-")
            if field not in _ALLOWED_FIELDS[resource]:
                raise QueryExecutionError(
                    "invalid_order_by",
                    f"order_by={query.order_by!r} not allowed for resource={resource}",
                )
            return qs.order_by(query.order_by)
        return qs.order_by(*_DEFAULT_ORDER_BY[resource])

    # ── Validation helpers ─────────────────────────────────────────────

    @staticmethod
    def _validate_filter_keys(query: JournalQueryRequest) -> None:
        allowed = _ALLOWED_FILTERS[query.resource]
        unknown = set(query.filter.keys()) - allowed
        if unknown:
            raise QueryExecutionError(
                "unknown_filter_keys",
                f"filter keys {sorted(unknown)!r} not allowed for resource={query.resource}; "
                f"allowed: {sorted(allowed)!r}",
            )

    @staticmethod
    def _validate_fields(query: JournalQueryRequest) -> None:
        if not query.fields:
            return
        allowed = _ALLOWED_FIELDS[query.resource]
        unknown = set(query.fields) - allowed
        if unknown:
            raise QueryExecutionError(
                "unknown_fields",
                f"fields {sorted(unknown)!r} not allowed for resource={query.resource}; allowed: {sorted(allowed)!r}",
            )

    @staticmethod
    def _validate_group_by(query: JournalQueryRequest) -> None:
        if not query.group_by:
            return
        allowed = _ALLOWED_GROUP_BY[query.resource]
        if query.group_by not in allowed:
            raise QueryExecutionError(
                "unknown_group_by",
                f"group_by={query.group_by!r} not allowed for resource={query.resource}; allowed: {sorted(allowed)!r}",
            )
        if query.aggregate is None:
            raise QueryExecutionError(
                "group_by_requires_aggregate",
                "group_by requires aggregate to be set",
            )

    @staticmethod
    def _validate_aggregate(query: JournalQueryRequest) -> None:
        if query.aggregate is None and query.aggregate_field:
            raise QueryExecutionError(
                "aggregate_field_without_aggregate",
                "aggregate_field is set but aggregate is None",
            )

    @staticmethod
    def _validate_window_field(query: JournalQueryRequest) -> None:
        if not query.window_field:
            return
        allowed = _ALLOWED_WINDOW_FIELDS[query.resource]
        if query.window_field not in allowed:
            raise QueryExecutionError(
                "unknown_window_field",
                f"window_field={query.window_field!r} not allowed for resource={query.resource}; "
                f"allowed: {list(allowed)!r}",
            )

    # ── Row serializers ────────────────────────────────────────────────

    @staticmethod
    def _serialize_entry(e: JournalEntry, fields: list[str] | None) -> dict[str, Any]:
        full = {
            "id": str(e.id),
            "date": e.date.isoformat(),
            "mood": e.mood,
            "energy": e.energy,
            "wins": e.wins,
            "challenges": e.challenges,
            "reflection": e.reflection,
            "created_at": e.created_at.isoformat(),
            "updated_at": e.updated_at.isoformat(),
        }
        return _project(full, fields, identifier="id")

    @staticmethod
    def _serialize_task(t: Task, fields: list[str] | None) -> dict[str, Any]:
        full = {
            "id": str(t.id),
            "title": t.title,
            "description": t.description,
            "pillar": t.pillar,
            "status": t.status,
            "due_date": t.due_date.isoformat() if t.due_date else None,
            "completed_at": t.completed_at.isoformat() if t.completed_at else None,
            "parent_goal_id": str(t.parent_goal_id) if t.parent_goal_id else None,
            "parent_task_id": str(t.parent_task_id) if t.parent_task_id else None,
            "related_ref": t.related_ref,
            "created_at": t.created_at.isoformat(),
            "updated_at": t.updated_at.isoformat(),
        }
        return _project(full, fields, identifier="id")

    @staticmethod
    def _serialize_goal(g: Goal, fields: list[str] | None) -> dict[str, Any]:
        full = {
            "id": str(g.id),
            "title": g.title,
            "description": g.description,
            "pillar": g.pillar,
            "status": g.status,
            "target": g.target,
            "target_date": g.target_date.isoformat() if g.target_date else None,
            "achieved_at": g.achieved_at.isoformat() if g.achieved_at else None,
            "parent_goal_id": str(g.parent_goal_id) if g.parent_goal_id else None,
            "created_at": g.created_at.isoformat(),
            "updated_at": g.updated_at.isoformat(),
        }
        return _project(full, fields, identifier="id")


def _project(full: dict[str, Any], fields: list[str] | None, *, identifier: str) -> dict[str, Any]:
    """Hint-based field projection: always include identifier; include all if
    ``fields`` is None; otherwise include identifier + listed fields."""
    if fields is None:
        return full
    keep = set(fields) | {identifier}
    return {k: v for k, v in full.items() if k in keep}
