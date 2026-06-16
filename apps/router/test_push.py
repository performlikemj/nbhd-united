"""Tests for APNs device-token registration, the gated sender, and the
reply-ready push hook.

Adversarial coverage: token validation, upsert + user-migration on
re-register, unregister, the "no-op unless configured" gate, the HTTP/2
unavailable gate, unregistered-token (410) pruning, and that a ready iOS reply
triggers (only) a configured push.
"""

from __future__ import annotations

import secrets
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.router.models import DeviceToken
from apps.tenants.models import Tenant, User

_VALID_TOKEN = "a" * 64
_VALID_TOKEN_2 = "b" * 64

_APNS_SETTINGS = dict(
    APNS_AUTH_KEY="-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----",
    APNS_KEY_ID="ABC1234567",
    APNS_TEAM_ID="TEAM123456",
    APNS_BUNDLE_ID="org.hoodunited.nbhd",
)


def _make_user() -> User:
    return User.objects.create_user(
        username=f"push_{secrets.token_hex(4)}",
        email=f"{secrets.token_hex(4)}@example.com",
    )


def _make_tenant(user: User) -> Tenant:
    return Tenant.objects.create(user=user, status=Tenant.Status.ACTIVE, container_fqdn="oc-push.example.com")


class PushRegisterTest(TestCase):
    def setUp(self):
        self.user = _make_user()
        self.tenant = _make_tenant(self.user)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_requires_auth(self):
        resp = APIClient().post("/api/v1/push/register/", {"device_token": _VALID_TOKEN}, format="json")
        self.assertIn(resp.status_code, (401, 403))

    def test_register_creates_token(self):
        resp = self.client.post(
            "/api/v1/push/register/",
            {"device_token": _VALID_TOKEN, "environment": "sandbox", "bundle_id": "org.hoodunited.nbhd"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        row = DeviceToken.objects.get(user=self.user, token=_VALID_TOKEN)
        self.assertEqual(row.environment, "sandbox")
        self.assertEqual(row.tenant_id, self.tenant.id)

    def test_invalid_token_rejected(self):
        resp = self.client.post("/api/v1/push/register/", {"device_token": "not-hex!"}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(DeviceToken.objects.count(), 0)

    def test_reregister_is_idempotent_upsert(self):
        self.client.post(
            "/api/v1/push/register/", {"device_token": _VALID_TOKEN, "environment": "sandbox"}, format="json"
        )
        self.client.post(
            "/api/v1/push/register/", {"device_token": _VALID_TOKEN, "environment": "production"}, format="json"
        )
        rows = DeviceToken.objects.filter(user=self.user, token=_VALID_TOKEN)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().environment, "production")

    def test_unknown_environment_defaults_production(self):
        self.client.post(
            "/api/v1/push/register/", {"device_token": _VALID_TOKEN, "environment": "weird"}, format="json"
        )
        self.assertEqual(DeviceToken.objects.get(token=_VALID_TOKEN).environment, "production")

    def test_token_migrates_to_current_user(self):
        # Same physical device token previously registered to another user
        # (account switch on one install) re-points to the current user — and the
        # token stays globally unique (exactly one owner), so a push for the old
        # user can never reach the device now used by the new one.
        other = _make_user()
        _make_tenant(other)
        DeviceToken.objects.create(user=other, tenant=other.tenant, token=_VALID_TOKEN)
        self.client.post("/api/v1/push/register/", {"device_token": _VALID_TOKEN}, format="json")
        rows = DeviceToken.objects.filter(token=_VALID_TOKEN)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().user_id, self.user.id)

    def test_unregister(self):
        DeviceToken.objects.create(user=self.user, tenant=self.tenant, token=_VALID_TOKEN)
        resp = self.client.delete("/api/v1/push/register/", {"device_token": _VALID_TOKEN}, format="json")
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(DeviceToken.objects.filter(token=_VALID_TOKEN).count(), 0)


class ApnsSenderTest(TestCase):
    def test_skips_when_not_configured(self):
        from apps.common.apns import send_push

        res = send_push([_VALID_TOKEN], title="t", body="b")
        self.assertEqual(res["skipped"], "not_configured")
        self.assertEqual(res["sent"], 0)

    @override_settings(**_APNS_SETTINGS)
    def test_skips_when_no_tokens(self):
        from apps.common.apns import send_push

        res = send_push([], title="t", body="b")
        self.assertEqual(res["skipped"], "no_tokens")

    @override_settings(**_APNS_SETTINGS)
    @patch("apps.common.apns._provider_jwt", return_value="signed-jwt")
    @patch("apps.common.apns._http2_client")
    def test_sends_and_collects_unregistered(self, mock_client_factory, _jwt):
        # First token OK (200), second is stale (410) → reported unregistered.
        ok = MagicMock(status_code=200)
        gone = MagicMock(status_code=410, text="Unregistered")
        fake_client = MagicMock()
        fake_client.__enter__.return_value = fake_client
        fake_client.__exit__.return_value = False
        fake_client.post.side_effect = [ok, gone]
        mock_client_factory.return_value = fake_client

        from apps.common.apns import send_push

        res = send_push([_VALID_TOKEN, _VALID_TOKEN_2], title="NBHD", body="hi", thread_id="t1")
        self.assertEqual(res["sent"], 1)
        self.assertEqual(res["failed"], 1)
        self.assertEqual(res["unregistered"], [_VALID_TOKEN_2])
        # apns-topic + push-type headers carried.
        _args, kwargs = fake_client.post.call_args_list[0]
        self.assertEqual(kwargs["headers"]["apns-topic"], "org.hoodunited.nbhd")
        self.assertEqual(kwargs["headers"]["apns-push-type"], "alert")

    @override_settings(**_APNS_SETTINGS)
    @patch("apps.common.apns._http2_client", return_value=None)
    def test_skips_when_http2_unavailable(self, _factory):
        from apps.common.apns import send_push

        res = send_push([_VALID_TOKEN], title="t", body="b")
        self.assertEqual(res["skipped"], "http2_unavailable")

    @override_settings(**_APNS_SETTINGS)
    @patch("apps.common.apns._provider_jwt", return_value="jwt")
    @patch("apps.common.apns._http2_client")
    def test_sandbox_flag_selects_host(self, mock_factory, _jwt):
        fake = MagicMock()
        fake.__enter__.return_value = fake
        fake.__exit__.return_value = False
        fake.post.return_value = MagicMock(status_code=200)
        mock_factory.return_value = fake

        from apps.common.apns import send_push

        send_push([_VALID_TOKEN], title="t", body="b", sandbox=True)
        mock_factory.assert_called_with(True)  # → sandbox host
        send_push([_VALID_TOKEN], title="t", body="b", sandbox=False)
        mock_factory.assert_called_with(False)  # → production host


class NotifyReplyReadyTest(TestCase):
    def setUp(self):
        self.user = _make_user()
        self.tenant = _make_tenant(self.user)
        from apps.router.models import AppChatMessage, ChatThread

        self.thread = ChatThread.objects.create(tenant=self.tenant, user=self.user, is_main=True, title="Main")
        self.msg = AppChatMessage.objects.create(
            tenant=self.tenant,
            user=self.user,
            thread=self.thread,
            client_msg_id="r1",
            user_text="hi",
            reply_text="here you go",
            status=AppChatMessage.Status.READY,
        )

    def test_noop_when_not_configured(self):
        # No APNs settings → returns before touching the DB / sender.
        from apps.router.push_views import notify_app_reply_ready

        with patch("apps.common.apns.send_push") as mock_send:
            notify_app_reply_ready(self.tenant, ["r1"], "here you go")
        mock_send.assert_not_called()

    @override_settings(**_APNS_SETTINGS)
    def test_sends_and_prunes_unregistered(self):
        DeviceToken.objects.create(user=self.user, tenant=self.tenant, token=_VALID_TOKEN)
        from apps.router.push_views import notify_app_reply_ready

        with patch(
            "apps.common.apns.send_push",
            return_value={"sent": 0, "failed": 1, "unregistered": [_VALID_TOKEN], "skipped": None},
        ) as mock_send:
            notify_app_reply_ready(self.tenant, ["r1"], "here you go")
        mock_send.assert_called_once()
        # The stale token was pruned.
        self.assertFalse(DeviceToken.objects.filter(token=_VALID_TOKEN).exists())

    @override_settings(**_APNS_SETTINGS)
    def test_noop_when_no_device_tokens(self):
        from apps.router.push_views import notify_app_reply_ready

        with patch("apps.common.apns.send_push") as mock_send:
            notify_app_reply_ready(self.tenant, ["r1"], "here you go")
        mock_send.assert_not_called()

    @override_settings(**_APNS_SETTINGS)
    def test_routes_each_environment_to_its_host(self):
        # A sandbox (Debug) device and a production (App Store) device for the same
        # user → one send per environment, each with the matching sandbox flag.
        DeviceToken.objects.create(user=self.user, tenant=self.tenant, token=_VALID_TOKEN, environment="sandbox")
        DeviceToken.objects.create(user=self.user, tenant=self.tenant, token=_VALID_TOKEN_2, environment="production")
        from apps.router.push_views import notify_app_reply_ready

        calls = []

        def _capture(tokens, **kw):
            calls.append((sorted(tokens), kw.get("sandbox")))
            return {"sent": len(tokens), "failed": 0, "unregistered": [], "skipped": None}

        with patch("apps.common.apns.send_push", side_effect=_capture):
            notify_app_reply_ready(self.tenant, ["r1"], "hi")

        self.assertEqual(len(calls), 2)
        by_sandbox = {sandbox: tokens for tokens, sandbox in calls}
        self.assertEqual(by_sandbox[True], [_VALID_TOKEN])  # sandbox token → sandbox host
        self.assertEqual(by_sandbox[False], [_VALID_TOKEN_2])  # production token → prod host
