"""User-facing (session-auth) write endpoints for the typed Task/Goal lifecycle.

Mirrors the agent-facing runtime endpoints in ``apps.integrations.runtime_views``
but authenticated as the logged-in user (``IsAuthenticated`` + the user's own
tenant), so the web UI can update typed rows directly instead of editing
synthesized markdown that the read layer would discard.

Status transitions use the model methods (``Task.complete`` etc.) so timestamps
(``completed_at``, ``achieved_at``) are set correctly — a plain ``status=done``
PATCH would not. See ``DocumentDetailView.patch``, which now rejects markdown
writes to typed tasks/goal docs and points here.

Serializer/model imports are LOCAL per ``feedback_local_reimport_pattern`` (the
lint-on-Edit hook reaps module-level imports that look unused at parse time).
"""

from __future__ import annotations

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .document_views import _get_tenant


def _parse_iso_date(raw, field_name):
    """Parse a YYYY-MM-DD query param, or return None. Raise ValueError if malformed."""
    if not raw:
        return None
    from datetime import date

    try:
        return date.fromisoformat(str(raw))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an ISO date (YYYY-MM-DD)") from exc


def _parse_uuid(raw, field_name):
    """Return ``raw`` if it is a valid UUID string, else None. Raise ValueError if malformed."""
    if not raw:
        return None
    import uuid

    try:
        uuid.UUID(str(raw))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{field_name} must be a valid UUID") from exc
    return raw


class TaskDetailView(APIView):
    """GET/PATCH /api/v1/journal/tasks/<uuid>/ — read or update a task."""

    permission_classes = [IsAuthenticated]

    def get(self, request, task_id):
        from .lifecycle_serializers import TaskSerializer
        from .models import Task

        tenant = _get_tenant(request.user)
        task = Task.objects.filter(tenant=tenant, id=task_id).first()
        if task is None:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        return Response(TaskSerializer(task).data)

    def patch(self, request, task_id):
        from .lifecycle_serializers import TaskSerializer
        from .models import Task

        tenant = _get_tenant(request.user)
        task = Task.objects.filter(tenant=tenant, id=task_id).first()
        if task is None:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)

        serializer = TaskSerializer(task, data=request.data, partial=True, context={"tenant": tenant})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(TaskSerializer(task).data)


class TaskCompleteView(APIView):
    """POST .../tasks/<uuid>/complete/ — status=done + completed_at=now."""

    permission_classes = [IsAuthenticated]

    def post(self, request, task_id):
        from .lifecycle_serializers import TaskSerializer
        from .models import Task

        tenant = _get_tenant(request.user)
        task = Task.objects.filter(tenant=tenant, id=task_id).first()
        if task is None:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)

        task.complete()
        return Response(TaskSerializer(task).data)


class TaskReopenView(APIView):
    """POST .../tasks/<uuid>/reopen/ — status=open, clear completed_at."""

    permission_classes = [IsAuthenticated]

    def post(self, request, task_id):
        from .lifecycle_serializers import TaskSerializer
        from .models import Task

        tenant = _get_tenant(request.user)
        task = Task.objects.filter(tenant=tenant, id=task_id).first()
        if task is None:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)

        task.status = Task.Status.OPEN
        task.completed_at = None
        task.save(update_fields=["status", "completed_at", "updated_at"])
        return Response(TaskSerializer(task).data)


class GoalDetailView(APIView):
    """GET/PATCH /api/v1/journal/goals/<uuid>/ — read or update a goal."""

    permission_classes = [IsAuthenticated]

    def get(self, request, goal_id):
        from .lifecycle_serializers import GoalSerializer
        from .models import Goal

        tenant = _get_tenant(request.user)
        goal = Goal.objects.filter(tenant=tenant, id=goal_id).first()
        if goal is None:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        return Response(GoalSerializer(goal).data)

    def patch(self, request, goal_id):
        from .lifecycle_serializers import GoalSerializer
        from .models import Goal

        tenant = _get_tenant(request.user)
        goal = Goal.objects.filter(tenant=tenant, id=goal_id).first()
        if goal is None:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)

        serializer = GoalSerializer(goal, data=request.data, partial=True, context={"tenant": tenant})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(GoalSerializer(goal).data)


class GoalAchieveView(APIView):
    """POST .../goals/<uuid>/achieve/ — status=achieved + achieved_at=now."""

    permission_classes = [IsAuthenticated]

    def post(self, request, goal_id):
        from .lifecycle_serializers import GoalSerializer
        from .models import Goal

        tenant = _get_tenant(request.user)
        goal = Goal.objects.filter(tenant=tenant, id=goal_id).first()
        if goal is None:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)

        goal.mark_achieved()
        return Response(GoalSerializer(goal).data)


class GoalAbandonView(APIView):
    """POST .../goals/<uuid>/abandon/ — status=abandoned."""

    permission_classes = [IsAuthenticated]

    def post(self, request, goal_id):
        from .lifecycle_serializers import GoalSerializer
        from .models import Goal

        tenant = _get_tenant(request.user)
        goal = Goal.objects.filter(tenant=tenant, id=goal_id).first()
        if goal is None:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)

        goal.abandon()
        return Response(GoalSerializer(goal).data)


class TaskListCreateView(APIView):
    """GET /api/v1/journal/tasks/ — list the tenant's tasks (filters: status,
    pillar, parent_goal_id, due_before, due_after). POST — create a task.

    The detail/transition endpoints (PATCH, complete, reopen) already exist
    above; this adds the collection read + create the connected iOS client and
    web UI need to enumerate and add tasks without going through the agent.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .lifecycle_serializers import TaskSerializer
        from .models import Task

        tenant = _get_tenant(request.user)
        qs = Task.objects.filter(tenant=tenant)
        params = request.query_params
        for field in ("status", "pillar"):
            value = params.get(field)
            if value:
                qs = qs.filter(**{field: value})
        try:
            parent_goal_id = _parse_uuid(params.get("parent_goal_id"), "parent_goal_id")
            due_before = _parse_iso_date(params.get("due_before"), "due_before")
            due_after = _parse_iso_date(params.get("due_after"), "due_after")
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        if parent_goal_id:
            qs = qs.filter(parent_goal_id=parent_goal_id)
        if due_before:
            qs = qs.filter(due_date__lte=due_before)
        if due_after:
            qs = qs.filter(due_date__gte=due_after)
        return Response(TaskSerializer(qs.order_by("-updated_at"), many=True).data)

    def post(self, request):
        from .lifecycle_serializers import TaskSerializer

        tenant = _get_tenant(request.user)
        serializer = TaskSerializer(data=request.data, context={"tenant": tenant})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class GoalListCreateView(APIView):
    """GET /api/v1/journal/goals/ — list the tenant's goals (filters: status,
    pillar, parent_goal_id). POST — create a goal."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .lifecycle_serializers import GoalSerializer
        from .models import Goal

        tenant = _get_tenant(request.user)
        qs = Goal.objects.filter(tenant=tenant)
        params = request.query_params
        for field in ("status", "pillar"):
            value = params.get(field)
            if value:
                qs = qs.filter(**{field: value})
        try:
            parent_goal_id = _parse_uuid(params.get("parent_goal_id"), "parent_goal_id")
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        if parent_goal_id:
            qs = qs.filter(parent_goal_id=parent_goal_id)
        return Response(GoalSerializer(qs.order_by("-updated_at"), many=True).data)

    def post(self, request):
        from .lifecycle_serializers import GoalSerializer

        tenant = _get_tenant(request.user)
        serializer = GoalSerializer(data=request.data, context={"tenant": tenant})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED)
