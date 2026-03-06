"""Tests for the action gating models."""

from datetime import timedelta

from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone

from apps.actions.models import (
    ActionAuditLog,
    ActionStatus,
    ActionType,
    GatePreference,
    PendingAction,
)
from apps.tenants.models import Tenant


class PendingActionModelTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        User = get_user_model()
        self.user = User.objects.create_user(
            username="action_test1", email="action_test1@example.com", password="testpass"
        )
        self.tenant = Tenant.objects.create(user=self.user, status="active", container_fqdn="test.example.com", container_id="oc-test-1")

    def test_create_with_defaults(self):
        action = PendingAction.objects.create(
            tenant=self.tenant,
            action_type=ActionType.GMAIL_TRASH,
            action_payload={"message_id": "abc123", "subject": "Test"},
            display_summary="Trash email: 'Test'",
        )
        self.assertEqual(action.status, ActionStatus.PENDING)
        self.assertIsNotNone(action.expires_at)
        self.assertIsNone(action.responded_at)
        self.assertEqual(action.platform_message_id, "")
        self.assertEqual(action.platform_channel, "")

    def test_expires_at_default_is_5_minutes(self):
        before = timezone.now() + timedelta(minutes=4, seconds=50)
        action = PendingAction.objects.create(
            tenant=self.tenant,
            action_type=ActionType.GMAIL_DELETE,
            action_payload={},
            display_summary="Delete email",
        )
        after = timezone.now() + timedelta(minutes=5, seconds=10)
        self.assertGreater(action.expires_at, before)
        self.assertLess(action.expires_at, after)

    def test_is_expired_when_past_deadline(self):
        action = PendingAction.objects.create(
            tenant=self.tenant,
            action_type=ActionType.CALENDAR_DELETE,
            action_payload={},
            display_summary="Delete event",
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        self.assertTrue(action.is_expired)

    def test_is_not_expired_when_before_deadline(self):
        action = PendingAction.objects.create(
            tenant=self.tenant,
            action_type=ActionType.DRIVE_DELETE,
            action_payload={},
            display_summary="Delete file",
            expires_at=timezone.now() + timedelta(minutes=5),
        )
        self.assertFalse(action.is_expired)

    def test_is_not_expired_when_already_approved(self):
        action = PendingAction.objects.create(
            tenant=self.tenant,
            action_type=ActionType.GMAIL_TRASH,
            action_payload={},
            display_summary="Trash email",
            status=ActionStatus.APPROVED,
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        self.assertFalse(action.is_expired)

    def test_all_action_types(self):
        for action_type in ActionType.values:
            action = PendingAction.objects.create(
                tenant=self.tenant,
                action_type=action_type,
                action_payload={},
                display_summary=f"Test {action_type}",
            )
            self.assertEqual(action.action_type, action_type)

    def test_str_representation(self):
        action = PendingAction.objects.create(
            tenant=self.tenant,
            action_type=ActionType.GMAIL_SEND,
            action_payload={},
            display_summary="Send email",
        )
        self.assertIn("Gmail: Send Email", str(action))
        self.assertIn("pending", str(action))


class GatePreferenceModelTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        User = get_user_model()
        self.user = User.objects.create_user(
            username="action_test2", email="action_test2@example.com", password="testpass"
        )
        self.tenant = Tenant.objects.create(user=self.user, status="active", container_fqdn="test.example.com", container_id="oc-test-2")

    def test_default_requires_confirmation(self):
        pref = GatePreference.objects.create(
            tenant=self.tenant,
            action_type=ActionType.GMAIL_TRASH,
        )
        self.assertTrue(pref.require_confirmation)

    def test_can_disable_confirmation(self):
        pref = GatePreference.objects.create(
            tenant=self.tenant,
            action_type=ActionType.GMAIL_TRASH,
            require_confirmation=False,
        )
        self.assertFalse(pref.require_confirmation)

    def test_unique_together_constraint(self):
        GatePreference.objects.create(
            tenant=self.tenant,
            action_type=ActionType.GMAIL_TRASH,
        )
        with self.assertRaises(IntegrityError):
            GatePreference.objects.create(
                tenant=self.tenant,
                action_type=ActionType.GMAIL_TRASH,
            )

    def test_different_action_types_allowed(self):
        GatePreference.objects.create(
            tenant=self.tenant,
            action_type=ActionType.GMAIL_TRASH,
        )
        pref2 = GatePreference.objects.create(
            tenant=self.tenant,
            action_type=ActionType.CALENDAR_DELETE,
        )
        self.assertEqual(GatePreference.objects.filter(tenant=self.tenant).count(), 2)


class ActionAuditLogModelTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        User = get_user_model()
        self.user = User.objects.create_user(
            username="action_test3", email="action_test3@example.com", password="testpass"
        )
        self.tenant = Tenant.objects.create(user=self.user, status="active", container_fqdn="test.example.com", container_id="oc-test-3")

    def test_create_approved_log(self):
        log = ActionAuditLog.objects.create(
            tenant=self.tenant,
            action_type=ActionType.GMAIL_TRASH,
            action_payload={"message_id": "abc123"},
            display_summary="Trash email: 'Test'",
            result=ActionStatus.APPROVED,
            responded_at=timezone.now(),
        )
        self.assertEqual(log.result, ActionStatus.APPROVED)
        self.assertIsNotNone(log.created_at)

    def test_create_denied_log(self):
        log = ActionAuditLog.objects.create(
            tenant=self.tenant,
            action_type=ActionType.DRIVE_DELETE,
            action_payload={"file_id": "xyz"},
            display_summary="Delete file: report.pdf",
            result=ActionStatus.DENIED,
        )
        self.assertEqual(log.result, ActionStatus.DENIED)
        self.assertIsNone(log.responded_at)

    def test_create_expired_log(self):
        log = ActionAuditLog.objects.create(
            tenant=self.tenant,
            action_type=ActionType.GMAIL_SEND,
            action_payload={},
            display_summary="Send email to boss@work.com",
            result=ActionStatus.EXPIRED,
        )
        self.assertEqual(log.result, ActionStatus.EXPIRED)

    def test_ordering_newest_first(self):
        log1 = ActionAuditLog.objects.create(
            tenant=self.tenant,
            action_type=ActionType.GMAIL_TRASH,
            action_payload={},
            display_summary="First",
            result=ActionStatus.APPROVED,
        )
        log2 = ActionAuditLog.objects.create(
            tenant=self.tenant,
            action_type=ActionType.GMAIL_TRASH,
            action_payload={},
            display_summary="Second",
            result=ActionStatus.DENIED,
        )
        logs = list(ActionAuditLog.objects.all())
        self.assertEqual(logs[0].id, log2.id)
        self.assertEqual(logs[1].id, log1.id)


class TenantGateFieldTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        User = get_user_model()
        self.user = User.objects.create_user(
            username="action_test4", email="action_test4@example.com", password="testpass"
        )

    def test_default_gate_all_actions_true(self):
        tenant = Tenant.objects.create(user=self.user, status="active", container_fqdn="test.example.com", container_id="oc-test-4")
        self.assertTrue(tenant.gate_all_actions)

    def test_default_gate_acknowledged_risk_false(self):
        tenant = Tenant.objects.create(user=self.user, status="active", container_fqdn="test.example.com", container_id="oc-test-5")
        self.assertFalse(tenant.gate_acknowledged_risk)

    def test_can_disable_gating_with_acknowledgment(self):
        tenant = Tenant.objects.create(
            user=self.user,
            status="active",
            container_fqdn="test.example.com",
            container_id="oc-test",
            gate_all_actions=False,
            gate_acknowledged_risk=True,
        )
        self.assertFalse(tenant.gate_all_actions)
        self.assertTrue(tenant.gate_acknowledged_risk)
