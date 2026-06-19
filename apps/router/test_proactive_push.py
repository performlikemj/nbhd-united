"""Tests for the cron / proactive → iOS APNs push.

Crons (and other ``nbhd_send_to_user`` proactive sends) were delivered over
Telegram/LINE and recorded as ``ProactiveOutbound`` rows that surfaced in the
``?since=`` feed, but never pinged the iOS app — the APNs push only ever fired
for app-originated turns (``AppChatMessage``). These cover the new
``notify_proactive_ready`` helper, the ``record_proactive_outbound`` chokepoint
that drives it, and the end-to-end ``CronDeliveryView`` path:

* the "no-op unless APNs configured" gate (no claim burned, no send),
* idempotency via the ``ProactiveOutbound.notified_at`` claim (no double-push),
* per-environment host routing + unregistered-token (410) pruning,
* the markdown-stripped, content-free-for-tables lock-screen body,
* the cron-specific payload (collapse_id / extra / content-available),
* that the row is still written + returned when the push fails (fail-open).
"""

from __future__ import annotations

import secrets
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.router.cron_delivery import _rate_counts
from apps.router.models import ChatThread, DeviceToken, ProactiveOutbound
from apps.tenants.models import Tenant, User

_VALID_TOKEN = "a" * 64
_VALID_TOKEN_2 = "b" * 64

_APNS_SETTINGS = dict(
    APNS_AUTH_KEY="-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----",
    APNS_KEY_ID="ABC1234567",
    APNS_TEAM_ID="TEAM123456",
    APNS_BUNDLE_ID="org.hoodunited.nbhd",
)


def _ok(tokens, **kw):
    return {"sent": len(tokens), "failed": 0, "unregistered": [], "skipped": None}


def _make_user() -> User:
    return User.objects.create_user(
        username=f"pro_{secrets.token_hex(4)}",
        email=f"{secrets.token_hex(4)}@example.com",
    )


def _make_tenant(user: User) -> Tenant:
    return Tenant.objects.create(user=user, status=Tenant.Status.ACTIVE, container_fqdn="oc-pro.example.com")


class NotifyProactiveReadyTest(TestCase):
    def setUp(self):
        self.user = _make_user()
        self.tenant = _make_tenant(self.user)
        self.thread = ChatThread.objects.create(tenant=self.tenant, user=self.user, is_main=True, title="Main")

    def _row(self, message_text="Good morning! Ready for today?", job_name="morning") -> ProactiveOutbound:
        return ProactiveOutbound.objects.create(
            tenant=self.tenant,
            channel="telegram",
            channel_user_id="12345",
            message_text=message_text,
            job_name=job_name,
        )

    def test_noop_when_not_configured(self):
        # No APNs settings → return before any DB work; the claim is NOT burned.
        from apps.router.push_views import notify_proactive_ready

        row = self._row()
        with patch("apps.common.apns.send_push") as mock_send:
            notify_proactive_ready(self.tenant, str(row.id), "Good morning!")
        mock_send.assert_not_called()
        row.refresh_from_db()
        self.assertIsNone(row.notified_at)

    @override_settings(**_APNS_SETTINGS)
    def test_sends_push_and_claims_notified_at(self):
        DeviceToken.objects.create(user=self.user, tenant=self.tenant, token=_VALID_TOKEN, environment="sandbox")
        from apps.router.push_views import notify_proactive_ready

        row = self._row()
        captured = {}

        def _capture(tokens, **kw):
            captured.update(kw)
            return _ok(tokens, **kw)

        with patch("apps.common.apns.send_push", side_effect=_capture) as mock_send:
            notify_proactive_ready(self.tenant, str(row.id), row.message_text)

        mock_send.assert_called_once()
        self.assertEqual(captured["collapse_id"], f"cron:{row.id}")
        self.assertEqual(captured["extra"], {"id": f"cron:{row.id}", "source": "cron"})
        self.assertTrue(captured["content_available"])
        # thread_id mirrors the ?since= feed's main-thread mapping.
        self.assertEqual(captured["thread_id"], str(self.thread.id))
        row.refresh_from_db()
        self.assertIsNotNone(row.notified_at)

    @override_settings(**_APNS_SETTINGS)
    def test_idempotent_no_double_push(self):
        # A second call for the same row (a future retry / reconcile path) is a
        # no-op — the notified_at claim returns rowcount 0.
        DeviceToken.objects.create(user=self.user, tenant=self.tenant, token=_VALID_TOKEN, environment="sandbox")
        from apps.router.push_views import notify_proactive_ready

        row = self._row()
        calls = []
        with patch("apps.common.apns.send_push", side_effect=lambda t, **kw: calls.append(kw) or _ok(t, **kw)):
            notify_proactive_ready(self.tenant, str(row.id), row.message_text)
            notify_proactive_ready(self.tenant, str(row.id), row.message_text)
        self.assertEqual(len(calls), 1)

    @override_settings(**_APNS_SETTINGS)
    def test_noop_when_no_device_tokens(self):
        # Telegram/LINE-only user (no iOS install) → nothing to push to.
        from apps.router.push_views import notify_proactive_ready

        row = self._row()
        with patch("apps.common.apns.send_push") as mock_send:
            notify_proactive_ready(self.tenant, str(row.id), row.message_text)
        mock_send.assert_not_called()

    @override_settings(**_APNS_SETTINGS)
    def test_routes_each_environment_to_its_host(self):
        DeviceToken.objects.create(user=self.user, tenant=self.tenant, token=_VALID_TOKEN, environment="sandbox")
        DeviceToken.objects.create(user=self.user, tenant=self.tenant, token=_VALID_TOKEN_2, environment="production")
        from apps.router.push_views import notify_proactive_ready

        row = self._row()
        calls = []
        with patch(
            "apps.common.apns.send_push",
            side_effect=lambda tokens, **kw: calls.append((sorted(tokens), kw.get("sandbox"))) or _ok(tokens),
        ):
            notify_proactive_ready(self.tenant, str(row.id), row.message_text)

        self.assertEqual(len(calls), 2)
        by_sandbox = {sandbox: tokens for tokens, sandbox in calls}
        self.assertEqual(by_sandbox[True], [_VALID_TOKEN])
        self.assertEqual(by_sandbox[False], [_VALID_TOKEN_2])

    @override_settings(**_APNS_SETTINGS)
    def test_markdown_body_is_stripped(self):
        # Cron prose routinely carries markdown; the lock-screen taste must be clean.
        DeviceToken.objects.create(user=self.user, tenant=self.tenant, token=_VALID_TOKEN, environment="sandbox")
        from apps.router.push_views import notify_proactive_ready

        md = "## Morning\n\n**Big day** — _hydrate_. See [plan](https://x.co)."
        captured = {}
        with patch(
            "apps.common.apns.send_push",
            side_effect=lambda t, **kw: captured.update(kw) or _ok(t),
        ):
            row = self._row(message_text=md)
            notify_proactive_ready(self.tenant, str(row.id), md)

        body = captured["body"]
        for sym in ("**", "##", "__", "](", "https://"):
            self.assertNotIn(sym, body)
        self.assertIn("Big day", body)

    @override_settings(**_APNS_SETTINGS)
    def test_table_body_falls_back_to_generic(self):
        DeviceToken.objects.create(user=self.user, tenant=self.tenant, token=_VALID_TOKEN, environment="sandbox")
        from apps.router.push_views import _GENERIC_BODY, notify_proactive_ready

        md = "Your week:\n\n| Day | Plan |\n| --- | --- |\n| Mon | Push |"
        captured = {}
        with patch(
            "apps.common.apns.send_push",
            side_effect=lambda t, **kw: captured.update(kw) or _ok(t),
        ):
            row = self._row(message_text=md)
            notify_proactive_ready(self.tenant, str(row.id), md)
        self.assertEqual(captured["body"], _GENERIC_BODY)

    @override_settings(**_APNS_SETTINGS)
    def test_prunes_unregistered_tokens(self):
        DeviceToken.objects.create(user=self.user, tenant=self.tenant, token=_VALID_TOKEN, environment="sandbox")
        from apps.router.push_views import notify_proactive_ready

        row = self._row()
        with patch(
            "apps.common.apns.send_push",
            return_value={"sent": 0, "failed": 1, "unregistered": [_VALID_TOKEN], "skipped": None},
        ):
            notify_proactive_ready(self.tenant, str(row.id), row.message_text)
        self.assertFalse(DeviceToken.objects.filter(token=_VALID_TOKEN).exists())

    @override_settings(**_APNS_SETTINGS)
    def test_thread_id_none_when_no_main_thread(self):
        self.thread.delete()
        DeviceToken.objects.create(user=self.user, tenant=self.tenant, token=_VALID_TOKEN, environment="sandbox")
        from apps.router.push_views import notify_proactive_ready

        row = self._row()
        captured = {}
        with patch(
            "apps.common.apns.send_push",
            side_effect=lambda t, **kw: captured.update(kw) or _ok(t),
        ):
            notify_proactive_ready(self.tenant, str(row.id), row.message_text)
        self.assertIsNone(captured["thread_id"])


@override_settings(NBHD_DISABLE_BACKGROUND_THREADS=True, **_APNS_SETTINGS)
class RecordProactiveOutboundPushTest(TestCase):
    """The single chokepoint: writing a ProactiveOutbound row drives the push
    (so both CronDeliveryView and core.services.notify_meditation_ready get it)."""

    def setUp(self):
        self.user = _make_user()
        self.tenant = _make_tenant(self.user)
        ChatThread.objects.create(tenant=self.tenant, user=self.user, is_main=True, title="Main")
        DeviceToken.objects.create(user=self.user, tenant=self.tenant, token=_VALID_TOKEN, environment="sandbox")

    def test_record_triggers_push_and_returns_row(self):
        from apps.router.proactive_context import record_proactive_outbound

        with patch("apps.common.apns.send_push", side_effect=_ok) as mock_send:
            row = record_proactive_outbound(
                tenant=self.tenant,
                channel="telegram",
                channel_user_id="12345",
                message_text="Heads up — check-in time.",
                job_name="evening",
            )
        self.assertIsNotNone(row)
        mock_send.assert_called_once()
        row.refresh_from_db()
        self.assertIsNotNone(row.notified_at)

    def test_row_written_and_returned_even_if_push_raises(self):
        # A push failure must never lose the already-delivered cron message.
        from apps.router.proactive_context import record_proactive_outbound

        with patch("apps.common.apns.send_push", side_effect=RuntimeError("apns down")):
            row = record_proactive_outbound(
                tenant=self.tenant,
                channel="telegram",
                channel_user_id="12345",
                message_text="still recorded",
                job_name="evening",
            )
        self.assertIsNotNone(row)
        self.assertTrue(ProactiveOutbound.objects.filter(id=row.id).exists())


@override_settings(
    NBHD_DISABLE_BACKGROUND_THREADS=True,
    TELEGRAM_BOT_TOKEN="test-token",
    NBHD_INTERNAL_API_KEY="test-key",
    **_APNS_SETTINGS,
)
class CronDeliveryEmitsPushTest(TestCase):
    """End-to-end: a cron tool-call POST that delivers over Telegram also pings
    the user's iPhone — the actual symptom ('crons aren't firing on iOS')."""

    def setUp(self):
        self.user = _make_user()
        self.user.telegram_chat_id = 12345
        self.user.save()
        self.tenant = _make_tenant(self.user)
        ChatThread.objects.create(tenant=self.tenant, user=self.user, is_main=True, title="Main")
        DeviceToken.objects.create(user=self.user, tenant=self.tenant, token=_VALID_TOKEN, environment="sandbox")
        self.client = APIClient()
        self.url = f"/api/v1/integrations/runtime/{self.tenant.id}/send-to-user/"
        _rate_counts.clear()

    def _headers(self):
        return {"HTTP_X_NBHD_INTERNAL_KEY": "test-key", "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id)}

    @patch("apps.router.cron_delivery.httpx.Client")
    def test_cron_send_emits_ios_push(self, mock_client_cls):
        mock_http = MagicMock()
        mock_resp = MagicMock(is_success=True, status_code=200)
        mock_http.post.return_value = mock_resp
        mock_http.__enter__ = MagicMock(return_value=mock_http)
        mock_http.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_http

        with patch("apps.common.apns.send_push", side_effect=_ok) as mock_send:
            resp = self.client.post(self.url, {"message": "Good morning!"}, format="json", **self._headers())

        self.assertEqual(resp.status_code, 200, resp.content)
        mock_send.assert_called_once()
        row = ProactiveOutbound.objects.get(tenant=self.tenant)
        self.assertIsNotNone(row.notified_at)
