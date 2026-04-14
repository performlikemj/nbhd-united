"""Tests for action gating API endpoints."""

from datetime import timedelta

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.actions.models import (
    ActionAuditLog,
    ActionStatus,
    ActionType,
    PendingAction,
)
from apps.tenants.models import Tenant

INTERNAL_KEY = "test-internal-key-12345"
DEPLOY_SECRET = "test-deploy-secret-67890"


def _make_tenant(user, **overrides):
    defaults = {
        "user": user,
        "status": Tenant.Status.ACTIVE,
        "container_fqdn": "test.example.com",
        "container_id": f"oc-test-{user.username[:10]}",
        "model_tier": "starter",
    }
    defaults.update(overrides)
    return Tenant.objects.create(**defaults)


def _internal_headers(tenant_id):
    return {
        "HTTP_X_INTERNAL_KEY": INTERNAL_KEY,
        "HTTP_X_TENANT_ID": str(tenant_id),
    }


@override_settings(NBHD_INTERNAL_API_KEY=INTERNAL_KEY, DEPLOY_SECRET=DEPLOY_SECRET)
class GateRequestViewTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        User = get_user_model()
        self.user = User.objects.create_user(username="gate_req_user", email="gate_req@test.com", password="pass")
        self.tenant = _make_tenant(self.user)
        self.client = APIClient()
        self.url = reverse("gate-request", kwargs={"tenant_id": self.tenant.id})

    def test_starter_tier_blocks_destructive_action(self):
        resp = self.client.post(
            self.url,
            {
                "action_type": "gmail_trash",
                "payload": {"message_id": "abc123", "subject": "Test"},
                "display_summary": "Trash email: 'Test'",
            },
            format="json",
            **_internal_headers(self.tenant.id),
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.data["status"], "blocked")

    def test_invalid_action_type(self):
        resp = self.client.post(
            self.url,
            {
                "action_type": "invalid_type",
                "payload": {},
                "display_summary": "Bad type",
            },
            format="json",
            **_internal_headers(self.tenant.id),
        )
        self.assertEqual(resp.status_code, 400)

    def test_missing_display_summary(self):
        resp = self.client.post(
            self.url,
            {
                "action_type": "gmail_trash",
                "payload": {},
            },
            format="json",
            **_internal_headers(self.tenant.id),
        )
        self.assertEqual(resp.status_code, 400)

    def test_starter_tier_blocked(self):
        self.tenant.model_tier = "starter"
        self.tenant.save()
        resp = self.client.post(
            self.url,
            {
                "action_type": "gmail_trash",
                "payload": {},
                "display_summary": "Trash email",
            },
            format="json",
            **_internal_headers(self.tenant.id),
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.data["status"], "blocked")
        self.assertIn("prompt injection", resp.data["message"])

    def test_gate_disabled_still_blocked_on_starter(self):
        """Even with gate_all_actions=False, starter tier blocks destructive actions."""
        self.tenant.gate_all_actions = False
        self.tenant.gate_acknowledged_risk = True
        self.tenant.save()
        resp = self.client.post(
            self.url,
            {
                "action_type": "gmail_trash",
                "payload": {},
                "display_summary": "Trash email",
            },
            format="json",
            **_internal_headers(self.tenant.id),
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.data["status"], "blocked")

    def test_auth_required(self):
        resp = self.client.post(
            self.url,
            {
                "action_type": "gmail_trash",
                "payload": {},
                "display_summary": "Trash email",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 403)

    def test_all_action_types_blocked_on_starter(self):
        for action_type in ActionType.values:
            resp = self.client.post(
                self.url,
                {
                    "action_type": action_type,
                    "payload": {},
                    "display_summary": f"Test {action_type}",
                },
                format="json",
                **_internal_headers(self.tenant.id),
            )
            self.assertEqual(resp.status_code, 403, f"Expected blocked for {action_type}")


@override_settings(NBHD_INTERNAL_API_KEY=INTERNAL_KEY, DEPLOY_SECRET=DEPLOY_SECRET)
class GatePollViewTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        User = get_user_model()
        self.user = User.objects.create_user(username="gate_poll_user", email="gate_poll@test.com", password="pass")
        self.tenant = _make_tenant(self.user, container_id="oc-poll-test")
        self.client = APIClient()

    def test_poll_pending_action(self):
        action = PendingAction.objects.create(
            tenant=self.tenant,
            action_type=ActionType.GMAIL_TRASH,
            action_payload={},
            display_summary="Trash email",
        )
        url = reverse(
            "gate-poll",
            kwargs={"tenant_id": self.tenant.id, "action_id": action.id},
        )
        resp = self.client.get(url, **_internal_headers(self.tenant.id))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], "pending")

    def test_poll_approved_action(self):
        action = PendingAction.objects.create(
            tenant=self.tenant,
            action_type=ActionType.GMAIL_TRASH,
            action_payload={},
            display_summary="Trash email",
            status=ActionStatus.APPROVED,
        )
        url = reverse(
            "gate-poll",
            kwargs={"tenant_id": self.tenant.id, "action_id": action.id},
        )
        resp = self.client.get(url, **_internal_headers(self.tenant.id))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], "approved")

    def test_poll_expired_action_transitions(self):
        action = PendingAction.objects.create(
            tenant=self.tenant,
            action_type=ActionType.GMAIL_TRASH,
            action_payload={},
            display_summary="Trash email",
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        url = reverse(
            "gate-poll",
            kwargs={"tenant_id": self.tenant.id, "action_id": action.id},
        )
        resp = self.client.get(url, **_internal_headers(self.tenant.id))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], "expired")
        # Should create audit log
        self.assertEqual(ActionAuditLog.objects.count(), 1)

    def test_poll_nonexistent_action(self):
        url = reverse(
            "gate-poll",
            kwargs={"tenant_id": self.tenant.id, "action_id": 99999},
        )
        resp = self.client.get(url, **_internal_headers(self.tenant.id))
        self.assertEqual(resp.status_code, 404)


@override_settings(NBHD_INTERNAL_API_KEY=INTERNAL_KEY, DEPLOY_SECRET=DEPLOY_SECRET)
class GateRespondViewTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        User = get_user_model()
        self.user = User.objects.create_user(username="gate_resp_user", email="gate_resp@test.com", password="pass")
        self.tenant = _make_tenant(self.user, container_id="oc-resp-test")
        self.client = APIClient()

    def _make_pending(self, **kwargs):
        defaults = {
            "tenant": self.tenant,
            "action_type": ActionType.GMAIL_TRASH,
            "action_payload": {"message_id": "abc"},
            "display_summary": "Trash email: 'Test'",
        }
        defaults.update(kwargs)
        return PendingAction.objects.create(**defaults)

    def test_approve_action(self):
        action = self._make_pending()
        url = reverse("gate-respond", kwargs={"action_id": action.id})
        resp = self.client.post(
            url,
            {"action": "approve"},
            format="json",
            HTTP_X_DEPLOY_SECRET=DEPLOY_SECRET,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], "approved")
        action.refresh_from_db()
        self.assertEqual(action.status, ActionStatus.APPROVED)
        self.assertIsNotNone(action.responded_at)
        # Audit log created
        self.assertEqual(ActionAuditLog.objects.count(), 1)
        self.assertEqual(ActionAuditLog.objects.first().result, ActionStatus.APPROVED)

    def test_deny_action(self):
        action = self._make_pending()
        url = reverse("gate-respond", kwargs={"action_id": action.id})
        resp = self.client.post(
            url,
            {"action": "deny"},
            format="json",
            HTTP_X_DEPLOY_SECRET=DEPLOY_SECRET,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], "denied")
        action.refresh_from_db()
        self.assertEqual(action.status, ActionStatus.DENIED)

    def test_cannot_respond_twice(self):
        action = self._make_pending()
        url = reverse("gate-respond", kwargs={"action_id": action.id})
        # First response
        self.client.post(
            url,
            {"action": "approve"},
            format="json",
            HTTP_X_DEPLOY_SECRET=DEPLOY_SECRET,
        )
        # Second response
        resp = self.client.post(
            url,
            {"action": "deny"},
            format="json",
            HTTP_X_DEPLOY_SECRET=DEPLOY_SECRET,
        )
        self.assertEqual(resp.status_code, 409)

    def test_respond_to_expired_action(self):
        action = self._make_pending(
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        url = reverse("gate-respond", kwargs={"action_id": action.id})
        resp = self.client.post(
            url,
            {"action": "approve"},
            format="json",
            HTTP_X_DEPLOY_SECRET=DEPLOY_SECRET,
        )
        self.assertEqual(resp.status_code, 410)
        self.assertEqual(resp.data["status"], "expired")

    def test_invalid_action_value(self):
        action = self._make_pending()
        url = reverse("gate-respond", kwargs={"action_id": action.id})
        resp = self.client.post(
            url,
            {"action": "maybe"},
            format="json",
            HTTP_X_DEPLOY_SECRET=DEPLOY_SECRET,
        )
        self.assertEqual(resp.status_code, 400)

    def test_auth_required(self):
        action = self._make_pending()
        url = reverse("gate-respond", kwargs={"action_id": action.id})
        resp = self.client.post(
            url,
            {"action": "approve"},
            format="json",
        )
        self.assertEqual(resp.status_code, 403)

    def test_wrong_deploy_secret(self):
        action = self._make_pending()
        url = reverse("gate-respond", kwargs={"action_id": action.id})
        resp = self.client.post(
            url,
            {"action": "approve"},
            format="json",
            HTTP_X_DEPLOY_SECRET="wrong-secret",
        )
        self.assertEqual(resp.status_code, 403)
