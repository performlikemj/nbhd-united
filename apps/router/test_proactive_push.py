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
from apps.router.models import ChatThread, DeliveryAttempt, DeviceToken, ProactiveOutbound
from apps.tenants.models import Tenant, User
from apps.tenants.test_utils import seed_internal_key

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
        # Legacy rows without a stored destination still map to the main thread.
        self.assertEqual(captured["thread_id"], str(self.thread.id))
        row.refresh_from_db()
        self.assertIsNotNone(row.notified_at)

    @override_settings(**_APNS_SETTINGS)
    def test_push_uses_proactive_row_thread(self):
        DeviceToken.objects.create(user=self.user, tenant=self.tenant, token=_VALID_TOKEN, environment="sandbox")
        from apps.router.push_views import notify_proactive_ready

        thread = ChatThread.objects.create(tenant=self.tenant, user=self.user, title="Research")
        row = self._row()
        row.thread_id = thread.id
        row.save(update_fields=["thread_id"])
        captured = {}

        def _capture(tokens, **kw):
            captured.update(kw)
            return _ok(tokens, **kw)

        with patch("apps.common.apns.send_push", side_effect=_capture):
            notify_proactive_ready(self.tenant, str(row.id), row.message_text)

        self.assertEqual(captured["thread_id"], str(thread.id))

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
    def test_revoked_token_gets_zero_proactive_sends(self):
        from django.utils import timezone

        DeviceToken.objects.create(
            user=self.user,
            tenant=self.tenant,
            token=_VALID_TOKEN,
            revoked_at=timezone.now(),
        )
        from apps.router.push_views import notify_proactive_ready

        row = self._row()
        with patch("apps.common.apns.send_push") as mock_send:
            notify_proactive_ready(self.tenant, str(row.id), row.message_text)

        mock_send.assert_not_called()

    @override_settings(**_APNS_SETTINGS)
    def test_inactive_user_gets_zero_proactive_sends(self):
        DeviceToken.objects.create(user=self.user, tenant=self.tenant, token=_VALID_TOKEN)
        User.objects.filter(pk=self.user.pk).update(is_active=False)
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
        self.main = ChatThread.objects.create(tenant=self.tenant, user=self.user, is_main=True, title="Main")
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
        seed_internal_key(self.tenant)
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


class ResolveUserChannelTest(TestCase):
    """Outbound channel resolution order (MJ direction — prefer the app when an
    iOS device token exists; Telegram/LINE preserved for linked users without
    one): app → telegram → line → None. ``preferred_channel`` is not consulted.
    Telegram before LINE preserves the delivery surface for both-linked users
    (the old resolver's telegram default) — see ``resolve_user_channel``."""

    def setUp(self):
        self.user = _make_user()
        self.tenant = _make_tenant(self.user)

    def _add_token(self):
        DeviceToken.objects.create(user=self.user, tenant=self.tenant, token=_VALID_TOKEN, environment="sandbox")

    def test_token_beats_telegram_and_line(self):
        # token + tg + line → "app": the device token wins outright.
        self.user.telegram_chat_id = 999
        self.user.line_user_id = "U" + "1" * 32
        self.user.save()
        self._add_token()
        from apps.router.cron_delivery import resolve_user_channel

        self.assertEqual(resolve_user_channel(self.user), "app")

    def test_app_token_wins_over_telegram(self):
        # Behaviour flip vs the old resolver: a user with BOTH Telegram and the
        # app now routes to "app" (was "telegram"). The content lands in the app
        # feed as primary and no longer ALSO arrives in Telegram. They still get
        # the APNs push via record_proactive_outbound (as they always did).
        self.user.telegram_chat_id = 999
        self.user.save()
        self._add_token()
        from apps.router.cron_delivery import resolve_user_channel

        self.assertEqual(resolve_user_channel(self.user), "app")

    def test_token_only_resolves_to_app(self):
        self._add_token()
        from apps.router.cron_delivery import resolve_user_channel

        self.assertEqual(resolve_user_channel(self.user), "app")

    def test_telegram_only_resolves_to_telegram(self):
        # No token: a linked Telegram user keeps full two-way delivery.
        self.user.telegram_chat_id = 999
        self.user.save()
        from apps.router.cron_delivery import resolve_user_channel

        self.assertEqual(resolve_user_channel(self.user), "telegram")

    def test_line_only_resolves_to_line(self):
        self.user.line_user_id = "U" + "1" * 32
        self.user.save()
        from apps.router.cron_delivery import resolve_user_channel

        self.assertEqual(resolve_user_channel(self.user), "line")

    def test_telegram_beats_line_without_token(self):
        # Both messaging channels linked, no token → TELEGRAM (step 2 before
        # step 3). The old resolver honoured preferred_channel (universally the
        # "telegram" default), so both-linked users have always been delivered on
        # Telegram; a line-first fallback would silently move them to LINE and
        # split them from where their gates land (_resolve_gate_channel).
        self.user.telegram_chat_id = 999
        self.user.line_user_id = "U" + "1" * 32
        self.user.save()
        from apps.router.cron_delivery import resolve_user_channel

        self.assertEqual(resolve_user_channel(self.user), "telegram")

    def test_no_surface_resolves_to_none(self):
        from apps.router.cron_delivery import resolve_user_channel

        # No Telegram, no LINE, no device → genuinely nowhere to deliver.
        self.assertIsNone(resolve_user_channel(self.user))


@override_settings(
    NBHD_DISABLE_BACKGROUND_THREADS=True,
    TELEGRAM_BOT_TOKEN="test-token",
    LINE_CHANNEL_ACCESS_TOKEN="test-line-token",
    NBHD_INTERNAL_API_KEY="test-key",
    **_APNS_SETTINGS,
)
class CronDeliveryAppOnlyTest(TestCase):
    """An iOS-only user (no Telegram/LINE) gets crons delivered straight to the
    app — a push + a ?since= feed row — instead of the old 422 drop."""

    def setUp(self):
        self.user = _make_user()  # NB: no telegram_chat_id / line_user_id
        self.tenant = _make_tenant(self.user)
        seed_internal_key(self.tenant)
        self.main = ChatThread.objects.create(tenant=self.tenant, user=self.user, is_main=True, title="Main")
        self.client = APIClient()
        self.url = f"/api/v1/integrations/runtime/{self.tenant.id}/send-to-user/"
        _rate_counts.clear()

    def _headers(self):
        return {"HTTP_X_NBHD_INTERNAL_KEY": "test-key", "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id)}

    @patch("apps.router.cron_delivery.httpx.Client")
    def test_app_only_user_delivered_via_push_no_channel_send(self, mock_client_cls):
        DeviceToken.objects.create(user=self.user, tenant=self.tenant, token=_VALID_TOKEN, environment="sandbox")

        with patch("apps.common.apns.send_push", side_effect=_ok) as mock_send:
            resp = self.client.post(
                self.url, {"message": "Heads up — check-in time."}, format="json", **self._headers()
            )

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json().get("channel"), "app")
        # No Telegram/LINE HTTP send was attempted — the app IS the delivery.
        mock_client_cls.assert_not_called()
        # The push fired and the row is recorded as the 'app' channel + claimed.
        mock_send.assert_called_once()
        row = ProactiveOutbound.objects.get(tenant=self.tenant)
        self.assertEqual(row.channel, "app")
        self.assertEqual(row.channel_user_id, str(self.user.id))
        self.assertIsNone(row.thread_id)
        self.assertIsNotNone(row.notified_at)
        self.assertEqual(mock_send.call_args.kwargs["thread_id"], str(self.main.id))

    def test_subagent_result_routes_to_owned_thread_and_deduplicates_occurrence(self):
        DeviceToken.objects.create(user=self.user, tenant=self.tenant, token=_VALID_TOKEN, environment="sandbox")
        thread = ChatThread.objects.create(tenant=self.tenant, user=self.user, title="Research")
        headers = {
            **self._headers(),
            "HTTP_X_NBHD_OCCURRENCE_KEY": "subagent:announce-run-1",
            "HTTP_X_NBHD_JOB_NAME": "_subagent_result",
        }

        with patch("apps.common.apns.send_push", side_effect=_ok) as mock_send:
            first = self.client.post(
                self.url,
                {"message": "Research is ready.", "thread_id": str(thread.id)},
                format="json",
                **headers,
            )
            second = self.client.post(
                self.url,
                {"message": "Research is ready.", "thread_id": str(thread.id)},
                format="json",
                **headers,
            )

        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(second.json()["status"], "duplicate_suppressed")
        row = ProactiveOutbound.objects.get(tenant=self.tenant)
        self.assertEqual(row.thread_id, thread.id)
        self.assertEqual(mock_send.call_args.kwargs["thread_id"], str(thread.id))
        self.assertEqual(
            DeliveryAttempt.objects.get(tenant=self.tenant).occurrence_key,
            "subagent:announce-run-1",
        )
        mock_send.assert_called_once()

    def test_foreign_thread_falls_back_to_main(self):
        DeviceToken.objects.create(user=self.user, tenant=self.tenant, token=_VALID_TOKEN, environment="sandbox")
        other_user = _make_user()
        other_tenant = _make_tenant(other_user)
        foreign = ChatThread.objects.create(tenant=other_tenant, user=other_user, is_main=True, title="Other")

        with patch("apps.common.apns.send_push", side_effect=_ok) as mock_send:
            response = self.client.post(
                self.url,
                {"message": "Research is ready.", "thread_id": str(foreign.id)},
                format="json",
                **self._headers(),
            )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertIsNone(ProactiveOutbound.objects.get(tenant=self.tenant).thread_id)
        self.assertEqual(mock_send.call_args.kwargs["thread_id"], str(self.main.id))

    def test_unknown_thread_falls_back_to_main_without_persisting_destination(self):
        DeviceToken.objects.create(user=self.user, tenant=self.tenant, token=_VALID_TOKEN, environment="sandbox")
        unknown = "00000000-0000-4000-8000-000000000999"

        with patch("apps.common.apns.send_push", side_effect=_ok) as mock_send:
            response = self.client.post(
                self.url,
                {"message": "Research is ready.", "thread_id": unknown},
                format="json",
                **self._headers(),
            )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertIsNone(ProactiveOutbound.objects.get(tenant=self.tenant).thread_id)
        self.assertEqual(mock_send.call_args.kwargs["thread_id"], str(self.main.id))

    def test_malformed_thread_falls_back_to_main(self):
        DeviceToken.objects.create(user=self.user, tenant=self.tenant, token=_VALID_TOKEN, environment="sandbox")

        with patch("apps.common.apns.send_push", side_effect=_ok) as mock_send:
            response = self.client.post(
                self.url,
                {"message": "Research is ready.", "thread_id": "not-a-uuid"},
                format="json",
                **self._headers(),
            )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertIsNone(ProactiveOutbound.objects.get(tenant=self.tenant).thread_id)
        self.assertEqual(mock_send.call_args.kwargs["thread_id"], str(self.main.id))

    def test_no_surface_at_all_still_422(self):
        # No Telegram, no LINE, AND no device token → nothing to deliver to.
        with patch("apps.common.apns.send_push") as mock_send:
            resp = self.client.post(self.url, {"message": "hello"}, format="json", **self._headers())
        self.assertEqual(resp.status_code, 422)
        mock_send.assert_not_called()
        self.assertFalse(ProactiveOutbound.objects.filter(tenant=self.tenant).exists())


class AppChannelReachesSinceFeedTest(TestCase):
    """Closing the loop: an 'app'-channel ProactiveOutbound must surface in the
    GET /chat/messages/?since= feed (as a source='cron' assistant row) — that
    feed, not the push payload, is how the iOS app actually shows the text. If
    this regressed, an app-only user would get a buzz but never see the message."""

    def test_app_channel_row_appears_as_cron_in_since_feed(self):
        from apps.router.chat_history import build_since_page

        user = _make_user()
        tenant = _make_tenant(user)
        main_thread = ChatThread.objects.create(tenant=tenant, user=user, is_main=True, title="Main")
        row = ProactiveOutbound.objects.create(
            tenant=tenant,
            channel="app",  # the iOS-only delivery target
            channel_user_id=str(user.id),
            message_text="Heads up — check-in time.",
        )

        messages, _cursor = build_since_page(tenant, str(main_thread.id), cursor=None, limit=100)

        cron_rows = [m for m in messages if m["source"] == "cron"]
        self.assertEqual(len(cron_rows), 1)
        self.assertEqual(cron_rows[0]["id"], f"cron:{row.id}")
        self.assertEqual(cron_rows[0]["role"], "assistant")
        self.assertEqual(cron_rows[0]["text"], "Heads up — check-in time.")
        self.assertEqual(cron_rows[0]["thread_id"], str(main_thread.id))

    def test_app_channel_row_preserves_non_main_thread_in_since_feed(self):
        from apps.router.chat_history import build_since_page

        user = _make_user()
        tenant = _make_tenant(user)
        main_thread = ChatThread.objects.create(tenant=tenant, user=user, is_main=True, title="Main")
        research_thread = ChatThread.objects.create(tenant=tenant, user=user, title="Research")
        ProactiveOutbound.objects.create(
            tenant=tenant,
            channel="app",
            channel_user_id=str(user.id),
            message_text="Research is ready.",
            thread_id=research_thread.id,
        )

        messages, _cursor = build_since_page(tenant, str(main_thread.id), cursor=None, limit=100)

        cron_row = next(message for message in messages if message["source"] == "cron")
        self.assertEqual(cron_row["thread_id"], str(research_thread.id))
