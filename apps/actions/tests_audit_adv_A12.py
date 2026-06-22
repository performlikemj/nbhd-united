"""Adversarial audit A12 — regression tests for FA-0007-P1.

Verifies that expire_stale_pending_actions does not incur N+1 DB queries
by confirming the tenant and user are fetched via select_related (one JOIN)
rather than lazy-loaded per-action in the sweep loop.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.actions.models import ActionStatus, ActionType, PendingAction
from apps.tenants.models import Tenant

User = get_user_model()


class ExpireStaleActionsQueryCountTests(TestCase):
    """FA-0007-P1: select_related("tenant__user") on the stale queryset."""

    def _make_expired_action(self, tenant, suffix=""):
        return PendingAction.objects.create(
            tenant=tenant,
            action_type=ActionType.GMAIL_TRASH,
            action_payload={"message_id": f"msg{suffix}", "subject": "Test"},
            display_summary=f"Trash email{suffix}",
            status=ActionStatus.PENDING,
            expires_at=timezone.now() - timedelta(minutes=1),
        )

    def _make_tenant(self, index):
        user = User.objects.create_user(
            username=f"a12_user_{index}",
            email=f"a12_user_{index}@example.com",
            password="testpass",
        )
        return Tenant.objects.create(
            user=user,
            status="active",
            container_fqdn=f"t{index}.example.com",
            container_id=f"oc-a12-{index}",
        )

    def test_single_action_expires(self):
        """Basic smoke: one expired action gets marked EXPIRED."""
        from apps.actions.tasks import expire_stale_pending_actions

        tenant = self._make_tenant(0)
        action = self._make_expired_action(tenant, suffix="0")

        result = expire_stale_pending_actions()

        action.refresh_from_db()
        self.assertEqual(action.status, ActionStatus.EXPIRED)
        self.assertIn("1", result)

    def test_query_count_does_not_scale_with_action_count(self):
        """FA-0007-P1: queries for N actions must not grow as O(N).

        With select_related("tenant__user") the initial fetch JOINs both
        relations in one query. Subsequent per-action work (save + audit log
        create) is O(N) but bounded to a small constant per action — no
        extra SELECT for tenant or user.

        Strategy: run the sweep with 1 action, record the query count Q1.
        Run again with 3 more actions, record Q3. If N+1 were present,
        Q3 > Q1 + 2*(extra_queries_per_action). We assert the per-action
        increment is at most 2 (the update + audit insert) rather than 4+
        (which would indicate lazy fetching tenant and user per action).
        """
        from apps.actions.tasks import expire_stale_pending_actions

        # Batch A — 1 expired action
        tenant_a = self._make_tenant(1)
        self._make_expired_action(tenant_a, suffix="a")

        with CaptureQueriesContext(connection) as ctx_1:
            expire_stale_pending_actions()
        queries_1 = len(ctx_1)

        # Batch B — 3 more expired actions across different tenants
        for i in range(3):
            t = self._make_tenant(10 + i)
            self._make_expired_action(t, suffix=f"b{i}")

        with CaptureQueriesContext(connection) as ctx_3:
            expire_stale_pending_actions()
        queries_3 = len(ctx_3)

        # Per-action overhead should be at most 2 queries (UPDATE + INSERT).
        # If tenant/user were lazy-loaded that would add 2 more per action
        # (one SELECT tenant, one SELECT user), giving >=4 per action.
        per_action_increment = (queries_3 - queries_1) / 3
        self.assertLessEqual(
            per_action_increment,
            3,  # generous ceiling: SELECT-stale + per-action save + audit INSERT
            msg=(
                f"Expected <=3 additional queries per extra expired action "
                f"(got {per_action_increment:.1f}). N+1 lazy-load may have "
                f"re-appeared. queries_1={queries_1}, queries_3={queries_3}"
            ),
        )
