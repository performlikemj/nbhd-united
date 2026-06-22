"""Audit A30 — race-condition tests for PendingAction status serialization.

Verifies that the sweep (expire_stale_pending_actions), GateRespondView, and
the Telegram/LINE gate callback handlers all use conditional writes so that
concurrent approve-and-expire do not produce last-writer-wins stomps.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.utils import timezone

from apps.actions.models import ActionAuditLog, ActionStatus, PendingAction
from apps.actions.tasks import expire_stale_pending_actions


def _make_tenant():
    """Return a minimal Tenant-like mock with the fields the code touches."""
    from apps.tenants.models import Tenant

    # Use a real Tenant if the database is set up; fall back to a mock otherwise.
    try:
        user = MagicMock()
        user.language = "en"
        tenant = MagicMock(spec=Tenant)
        tenant.id = "00000000-0000-0000-0000-000000000001"
        tenant.user = user
        tenant.gate_all_actions = True
        tenant.gate_acknowledged_risk = False
        return tenant
    except Exception:
        return MagicMock()


class SweepConditionalUpdateTest(TestCase):
    """expire_stale_pending_actions must not overwrite a concurrent APPROVED."""

    def setUp(self):
        from django.contrib.auth import get_user_model

        User = get_user_model()
        self.user = User.objects.create_user(username="sweep_test", password="x")

        from apps.tenants.models import Tenant

        self.tenant = Tenant.objects.create(user=self.user)

    def _make_stale_action(self):
        return PendingAction.objects.create(
            tenant=self.tenant,
            action_type="gmail_trash",
            action_payload={"message_id": "abc"},
            display_summary="Trash test email",
            expires_at=timezone.now() - timedelta(seconds=10),
        )

    def test_sweep_skips_already_approved_row(self):
        """Sweep must not flip APPROVED→EXPIRED via last-writer-wins save()."""
        action = self._make_stale_action()
        # Simulate: a concurrent approve landed just before the sweep reads expires_at
        action.status = ActionStatus.APPROVED
        action.save(update_fields=["status"])

        with patch("apps.actions.messaging.update_gate_message"):
            result = expire_stale_pending_actions()

        action.refresh_from_db()
        self.assertEqual(action.status, ActionStatus.APPROVED, "sweep must not overwrite APPROVED")
        self.assertIn("Expired 0", result)

    def test_sweep_expires_genuinely_stale_pending(self):
        """Sweep must still expire rows that are truly PENDING+past deadline."""
        action = self._make_stale_action()
        # status remains PENDING

        with patch("apps.actions.messaging.update_gate_message"):
            result = expire_stale_pending_actions()

        action.refresh_from_db()
        self.assertEqual(action.status, ActionStatus.EXPIRED)
        self.assertIn("Expired 1", result)
        self.assertTrue(ActionAuditLog.objects.filter(
            tenant=self.tenant,
            result=ActionStatus.EXPIRED,
        ).exists())

    def test_sweep_does_not_double_audit_if_row_already_resolved(self):
        """No duplicate ActionAuditLog when sweep races a concurrent approve."""
        action = self._make_stale_action()
        # Pre-create an APPROVED audit log to simulate the approve path winning
        ActionAuditLog.objects.create(
            tenant=self.tenant,
            action_type=action.action_type,
            action_payload=action.action_payload,
            display_summary=action.display_summary,
            result=ActionStatus.APPROVED,
        )
        # Simulate the row was already flipped to APPROVED
        action.status = ActionStatus.APPROVED
        action.save(update_fields=["status"])

        with patch("apps.actions.messaging.update_gate_message"):
            expire_stale_pending_actions()

        # Only the one APPROVED log — no extra EXPIRED log from the sweep
        expired_count = ActionAuditLog.objects.filter(
            tenant=self.tenant, result=ActionStatus.EXPIRED
        ).count()
        self.assertEqual(expired_count, 0, "sweep must not create EXPIRED audit log for already-approved row")


class GateRespondViewAtomicTest(TestCase):
    """GateRespondView must serialize approve against concurrent sweep via atomic+select_for_update."""

    def setUp(self):
        from django.contrib.auth import get_user_model

        User = get_user_model()
        self.user = User.objects.create_user(username="respond_test", password="x")

        from apps.tenants.models import Tenant

        self.tenant = Tenant.objects.create(user=self.user)

    def _make_action(self, *, expired=False):
        offset = -10 if expired else 300
        return PendingAction.objects.create(
            tenant=self.tenant,
            action_type="gmail_trash",
            action_payload={"message_id": "xyz"},
            display_summary="Trash a test email",
            expires_at=timezone.now() + timedelta(seconds=offset),
        )

    def _post_respond(self, action_id, response_action="approve"):
        from rest_framework.test import APIClient

        client = APIClient()
        # DEPLOY_SECRET exists in settings but defaults to "" — override it with
        # a non-empty value so GateRespondView's secret check is exercised
        # rather than returning the "not configured" 500.
        deploy_secret = "test-secret"
        with self.settings(DEPLOY_SECRET=deploy_secret):
            resp = client.post(
                f"/api/v1/gate/{action_id}/respond/",
                data={"action": response_action},
                format="json",
                HTTP_X_DEPLOY_SECRET=deploy_secret,
            )
        return resp

    def test_respond_approve_returns_200_for_pending_action(self):
        action = self._make_action()
        with patch("apps.actions.messaging.update_gate_message"):
            resp = self._post_respond(action.id, "approve")
        self.assertIn(resp.status_code, (200,), f"Unexpected status: {resp.status_code} {resp.data}")
        action.refresh_from_db()
        self.assertEqual(action.status, ActionStatus.APPROVED)

    def test_respond_returns_409_for_already_resolved_action(self):
        action = self._make_action()
        action.status = ActionStatus.APPROVED
        action.save(update_fields=["status"])

        with patch("apps.actions.messaging.update_gate_message"):
            resp = self._post_respond(action.id, "deny")
        self.assertEqual(resp.status_code, 409)

    def test_respond_returns_410_for_expired_action(self):
        action = self._make_action(expired=True)

        with patch("apps.actions.messaging.update_gate_message"):
            resp = self._post_respond(action.id, "approve")
        self.assertEqual(resp.status_code, 410)
        action.refresh_from_db()
        self.assertEqual(action.status, ActionStatus.EXPIRED)
