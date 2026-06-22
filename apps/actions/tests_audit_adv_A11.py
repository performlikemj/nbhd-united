"""Regression tests for FA-0006 (cluster A11 — incomplete fix follow-up).

The C33 fix changed ``send_gate_confirmation`` to use ``resolve_user_channel``
instead of ``preferred_channel`` directly and added a no-surface warning, but
left ``views.GateRequestView`` always returning HTTP 202 "pending" even when
no channel exists.  An iOS-only user would then wait 5 minutes for the
container to time-out on the poll and receive "expired" with no explanation.

This A11 fix:
1. Changes ``send_gate_confirmation`` to return ``bool`` (True = delivered,
   False = no deliverable channel).
2. Changes ``GateRequestView.post`` to return HTTP 422 {"status": "undeliverable"}
   immediately when ``delivered`` is False, instead of HTTP 202 "pending".
"""

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from apps.actions import messaging
from apps.actions.messaging import send_gate_confirmation
from apps.actions.models import ActionStatus, ActionType, PendingAction
from apps.router.models import DeviceToken
from apps.tenants.models import Tenant

User = get_user_model()

INTERNAL_KEY = "a11-test-internal-key-xyzzy"


class SendGateConfirmationReturnValueTests(TestCase):
    """send_gate_confirmation must return bool, not None."""

    def _make(self, username, **user_kwargs):
        user = User.objects.create_user(
            username=username, email=f"{username}@example.com", password="x", **user_kwargs
        )
        tenant = Tenant.objects.create(
            user=user,
            status="active",
            container_fqdn=f"{username}.example.com",
            container_id=f"oc-{username}",
        )
        action = PendingAction.objects.create(
            tenant=tenant,
            action_type=ActionType.GMAIL_DELETE,
            action_payload={"message_id": "abc"},
            display_summary="Delete email",
        )
        return tenant, action

    def _patched_senders(self, tg=None, line=None):
        tg = tg or mock.Mock(return_value=None)
        line = line or mock.Mock(return_value=None)
        editor = mock.Mock()
        return mock.patch.dict(
            messaging._SENDERS,
            {"telegram": (tg, editor), "line": (line, editor)},
        ), tg, line

    def test_ios_only_user_returns_false(self):
        """iOS-only user (no Telegram/LINE) → returns False (undeliverable)."""
        user = User.objects.create_user(
            username="a11_ios", email="a11_ios@example.com", password="x"
        )
        tenant = Tenant.objects.create(
            user=user,
            status="active",
            container_fqdn="a11_ios.example.com",
            container_id="oc-a11ios",
        )
        DeviceToken.objects.create(
            tenant=tenant, user=user, token="b" * 64, environment="production"
        )
        action = PendingAction.objects.create(
            tenant=tenant,
            action_type=ActionType.GMAIL_DELETE,
            action_payload={"message_id": "x"},
            display_summary="Delete email",
        )
        patcher, tg, line = self._patched_senders()
        with patcher:
            result = send_gate_confirmation(tenant, action)

        self.assertIs(result, False)
        tg.assert_not_called()
        line.assert_not_called()

    def test_no_surface_returns_false(self):
        """User with no channel at all → returns False."""
        tenant, action = self._make("a11_none")
        patcher, tg, line = self._patched_senders()
        with patcher:
            result = send_gate_confirmation(tenant, action)

        self.assertIs(result, False)

    def test_telegram_user_returns_true(self):
        """Telegram-linked user → sender fires, returns True."""
        tenant, action = self._make("a11_tg", telegram_chat_id=777888999)
        patcher, tg, line = self._patched_senders(tg=mock.Mock(return_value="999"))
        with patcher:
            result = send_gate_confirmation(tenant, action)

        self.assertIs(result, True)
        tg.assert_called_once_with(tenant, action)

    def test_telegram_user_sender_returns_none_still_true(self):
        """Telegram sender ran but returned None (transient failure) → True
        (we dispatched; the failure is downstream, not a missing channel)."""
        tenant, action = self._make("a11_tg_fail", telegram_chat_id=111222333)
        patcher, tg, line = self._patched_senders(tg=mock.Mock(return_value=None))
        with patcher:
            result = send_gate_confirmation(tenant, action)

        self.assertIs(result, True)

    def test_line_user_returns_true(self):
        """LINE-linked user → LINE sender fires, returns True."""
        tenant, action = self._make(
            "a11_line", line_user_id="U" + "1" * 32, preferred_channel="line"
        )
        patcher, tg, line_mock = self._patched_senders(
            line=mock.Mock(return_value="line-push-y")
        )
        with patcher:
            result = send_gate_confirmation(tenant, action)

        self.assertIs(result, True)
        line_mock.assert_called_once_with(tenant, action)


@override_settings(NBHD_INTERNAL_API_KEY=INTERNAL_KEY)
class GateRequestViewUndeliverableTests(TestCase):
    """GateRequestView returns 422 undeliverable when no channel exists.

    Uses ``@override_settings(NBHD_INTERNAL_API_KEY=INTERNAL_KEY)`` and passes
    that key directly in ``X-Internal-Key``, matching the pattern in tests_api.py.
    Tenants are created with ``model_tier="pro"`` (not "starter") so the early
    Starter-tier 403 branch is bypassed and the gate-creation path runs.
    """

    def setUp(self):
        self.client = APIClient()

    def _make_tenant(self, username, **user_kwargs):
        user = User.objects.create_user(
            username=username, email=f"{username}@example.com", password="x", **user_kwargs
        )
        tenant = Tenant.objects.create(
            user=user,
            status="active",
            container_fqdn=f"{username}.example.com",
            container_id=f"oc-{username}",
            model_tier="pro",
        )
        return tenant

    def _post_gate(self, tenant):
        url = reverse("gate-request", kwargs={"tenant_id": tenant.id})
        return self.client.post(
            url,
            data={
                "action_type": ActionType.GMAIL_DELETE,
                "payload": {"message_id": "msg-1"},
                "display_summary": "Delete test email",
            },
            format="json",
            HTTP_X_INTERNAL_KEY=INTERNAL_KEY,
            HTTP_X_TENANT_ID=str(tenant.id),
        )

    def test_ios_only_returns_422_undeliverable(self):
        """iOS-only user triggers gate request → 422 with status=undeliverable."""
        tenant = self._make_tenant("a11_view_ios")
        DeviceToken.objects.create(
            tenant=tenant,
            user=tenant.user,
            token="c" * 64,
            environment="production",
        )

        with mock.patch(
            "apps.router.cron_delivery.resolve_user_channel", return_value="app"
        ):
            resp = self._post_gate(tenant)

        self.assertEqual(resp.status_code, 422)
        data = resp.json()
        self.assertEqual(data["status"], "undeliverable")
        self.assertEqual(data["reason"], "no_channel")
        # Action must be EXPIRED immediately (not left as PENDING)
        action = PendingAction.objects.get(id=data["action_id"])
        self.assertEqual(action.status, ActionStatus.EXPIRED)

    def test_no_channel_returns_422_undeliverable(self):
        """User with no channel at all → 422 undeliverable, action immediately expired."""
        tenant = self._make_tenant("a11_view_none")

        with mock.patch(
            "apps.router.cron_delivery.resolve_user_channel", return_value=None
        ):
            resp = self._post_gate(tenant)

        self.assertEqual(resp.status_code, 422)
        data = resp.json()
        self.assertEqual(data["status"], "undeliverable")
        action = PendingAction.objects.get(id=data["action_id"])
        self.assertEqual(action.status, ActionStatus.EXPIRED)

    def test_telegram_user_still_gets_202_pending(self):
        """Telegram-linked user → 202 pending (unchanged happy path)."""
        tenant = self._make_tenant("a11_view_tg", telegram_chat_id=123456789)

        # Patch the Telegram HTTP call so we don't need a real bot token.
        with mock.patch(
            "apps.actions.messaging._send_telegram_confirmation", return_value="tg-msg-1"
        ), mock.patch(
            "apps.router.cron_delivery.resolve_user_channel", return_value="telegram"
        ):
            resp = self._post_gate(tenant)

        self.assertEqual(resp.status_code, 202)
        data = resp.json()
        self.assertEqual(data["status"], "pending")
        self.assertIn("expires_at", data)
