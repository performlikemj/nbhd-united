"""User-facing Journal "current status" projection endpoint."""

from __future__ import annotations

from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.tenant_tz import tenant_tz

from .document_views import _get_tenant
from .status_projection import build_journal_status


def _rehydrate_status_payload(tenant, payload: dict) -> dict:
    """Rehydrate the user-visible strings in a status snapshot in place.

    ``build_journal_status`` (also consumed by the OpenClaw runtime context,
    ``apps.integrations.runtime_views``) must stay in placeholder space, so the
    rehydration happens HERE at the owner-facing serve boundary, not inside the
    projection. Task/goal titles and finance obligation nicknames are the
    user-visible strings that can carry a ``[TYPE_N]`` placeholder; IDs, dates,
    statuses and amounts never do. Mutating in place is safe because
    ``build_journal_status`` returns a fresh dict per call (nothing shared).
    """
    from apps.finance.models import FinanceAccount
    from apps.pii.authoring import resolve_receipt_values
    from apps.pii.redactor import rehydrate_for_tenant

    from .models import Goal, Task

    entity_map = getattr(tenant, "pii_entity_map", None)

    def _receipts(row) -> dict:
        return resolve_receipt_values((row.pii_receipts if row else {}) or {}, entity_map)

    tasks_by_id = {
        str(task.id): task
        for task in Task.objects.filter(
            tenant=tenant,
            id__in=[item.get("id") for item in payload.get("open_tasks", []) or [] if item.get("id")],
        )
    }
    goals_by_id = {
        str(goal.id): goal
        for goal in Goal.objects.filter(
            tenant=tenant,
            id__in=[item.get("id") for item in payload.get("active_goals", []) or [] if item.get("id")],
        )
    }
    accounts_by_id = {
        str(account.id): account
        for account in FinanceAccount.objects.filter(
            tenant=tenant,
            id__in=[item.get("account_id") for item in payload.get("obligations", []) or [] if item.get("account_id")],
        )
    }

    for task in payload.get("open_tasks", []) or []:
        if task.get("title"):
            task["title"] = rehydrate_for_tenant(tenant, task["title"])
        task["pii_receipts"] = _receipts(tasks_by_id.get(str(task.get("id"))))
    for goal in payload.get("active_goals", []) or []:
        if goal.get("title"):
            goal["title"] = rehydrate_for_tenant(tenant, goal["title"])
        goal["pii_receipts"] = _receipts(goals_by_id.get(str(goal.get("id"))))
    for obligation in payload.get("obligations", []) or []:
        if obligation.get("nickname"):
            obligation["nickname"] = rehydrate_for_tenant(tenant, obligation["nickname"])
        obligation["pii_receipts"] = _receipts(accounts_by_id.get(str(obligation.get("account_id"))))
    return payload


class JournalStatusView(APIView):
    """GET /api/v1/journal/status/ — live current-status projection.

    Read-only. Renders current state from the canonical typed models + the
    finance event ledger, so the journal page never displays a stale baked
    copy. See ``status_projection.build_journal_status`` for the folding.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant = _get_tenant(request.user)
        today = timezone.now().astimezone(tenant_tz(tenant)).date()
        payload = build_journal_status(tenant, today)
        return Response(_rehydrate_status_payload(tenant, payload))
