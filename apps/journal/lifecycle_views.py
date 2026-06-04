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


class TaskDetailView(APIView):
    """PATCH /api/v1/journal/tasks/<uuid>/ — update title/description/status/etc."""

    permission_classes = [IsAuthenticated]

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
    """PATCH /api/v1/journal/goals/<uuid>/ — update title/description/status/etc."""

    permission_classes = [IsAuthenticated]

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
