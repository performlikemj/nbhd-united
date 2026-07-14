"""The explicit ``eval`` sink channel and its operational isolation.

WHY THE SINK EXISTS. A synthetic eval tenant has no phone and no chat account, so
``resolve_user_channel`` returned None → ``CronDeliveryView`` 422'd → it returned
BEFORE ``record_proactive_outbound`` and nothing was ever written. The eval-behavior
tenant has ZERO ProactiveOutbound rows to this day, and even its one PASSING reminder
scenario delivered nothing a user would ever have seen: the cron fired, 422'd, and no
assertion could have caught it. That is green theater by the directive's own §1.3.

The journey probe worked around it by planting a FAKE APNs DeviceToken before every
arm — a hack that DESTROYED ITSELF on success (APNs rejects the fabricated token,
push_views prunes the row), so a daily schedule alternated pass/fail forever. The sink
retires that entirely.

The sink is gated on ``Tenant.is_eval_sink``, not ``is_synthetic``. That preserves
normal assistant behavior for synthetic demo accounts while making eval targets
explicit. A user without a delivery surface still gets a loud 422 unless that
dedicated flag has been set.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.router.cron_delivery import _rate_counts, resolve_user_channel
from apps.router.models import ChatThread, DeviceToken, ProactiveOutbound
from apps.tenants.models import Tenant
from apps.tenants.test_utils import seed_internal_key

User = get_user_model()


def _make(*, synthetic: bool, username: str, telegram: int | None = None, eval_sink: bool = False) -> Tenant:
    user = User.objects.create_user(username=username, password="x")
    if telegram is not None:
        user.telegram_chat_id = telegram
        user.save(update_fields=["telegram_chat_id"])
    return Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE,
        is_synthetic=synthetic,
        is_eval_sink=eval_sink,
    )


class ResolveUserChannelSinkTest(TestCase):
    def test_eval_sink_tenant_with_no_channel_resolves_to_the_sink(self):
        tenant = _make(synthetic=True, eval_sink=True, username="eval-nochan")
        self.assertEqual(resolve_user_channel(tenant.user), "eval")

    def test_real_tenant_with_no_channel_still_has_none(self):
        tenant = _make(synthetic=False, username="real-nochan")
        self.assertIsNone(resolve_user_channel(tenant.user))

    def test_synthetic_demo_tenant_keeps_normal_channel_behavior(self):
        tenant = _make(synthetic=True, username="app-store-demo", telegram=4242)
        self.assertEqual(resolve_user_channel(tenant.user), "telegram")

    def test_synthetic_demo_without_a_channel_is_not_an_eval_sink(self):
        tenant = _make(synthetic=True, username="app-store-demo-nochan")
        self.assertIsNone(resolve_user_channel(tenant.user))

    def test_eval_sink_preempts_a_linked_channel(self):
        tenant = _make(synthetic=True, eval_sink=True, username="eval-tg", telegram=4242)
        self.assertEqual(resolve_user_channel(tenant.user), "eval")

    def test_eval_sink_preempts_a_registered_device(self):
        tenant = _make(synthetic=True, eval_sink=True, username="eval-ios")
        DeviceToken.objects.create(
            tenant=tenant, user=tenant.user, token="a" * 64, environment="sandbox", bundle_id="x.y.z"
        )
        self.assertEqual(resolve_user_channel(tenant.user), "eval")


@override_settings(TELEGRAM_BOT_TOKEN="test-token", NBHD_INTERNAL_API_KEY="test-key")
class CronDeliveryToTheSinkTest(TestCase):
    def setUp(self):
        self.tenant = _make(synthetic=True, eval_sink=True, username="eval-delivery")
        seed_internal_key(self.tenant)
        self.client = APIClient()
        self.url = f"/api/v1/integrations/runtime/{self.tenant.id}/send-to-user/"
        _rate_counts.clear()

    def _headers(self):
        return {
            "HTTP_X_NBHD_INTERNAL_KEY": "test-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }

    def test_delivery_succeeds_and_writes_the_row_the_evals_assert_on(self):
        resp = self.client.post(self.url, {"message": "hydrate"}, format="json", **self._headers())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["channel"], "eval")

        row = ProactiveOutbound.objects.get(tenant=self.tenant)
        self.assertEqual(row.channel, ProactiveOutbound.Channel.EVAL)
        self.assertEqual(row.message_text, "hydrate")

    def test_sink_invokes_no_external_transport_or_push_dispatch(self):
        """The evidence row is written without invoking Telegram, LINE, or APNs."""
        with (
            patch("apps.router.cron_delivery.CronDeliveryView._send_via_telegram") as tg,
            patch("apps.router.cron_delivery.CronDeliveryView._send_via_line") as line,
            patch("apps.router.proactive_context._dispatch_ios_push") as push,
        ):
            resp = self.client.post(self.url, {"message": "hydrate"}, format="json", **self._headers())

        self.assertEqual(resp.status_code, 200)
        tg.assert_not_called()
        line.assert_not_called()
        push.assert_not_called()

    def test_a_REAL_tenant_with_no_channel_still_422s(self):
        """Regression guard for the safety property. Left unguarded, a bug here routes
        a paying subscriber's proactive messages into eval storage instead of to them."""
        real = _make(synthetic=False, username="real-delivery")
        seed_internal_key(real)
        resp = self.client.post(
            f"/api/v1/integrations/runtime/{real.id}/send-to-user/",
            {"message": "hi"},
            format="json",
            HTTP_X_NBHD_INTERNAL_KEY="test-key",
            HTTP_X_NBHD_TENANT_ID=str(real.id),
        )
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.data["error"], "no_channel_linked")
        self.assertFalse(ProactiveOutbound.objects.filter(tenant=real).exists())


class EvalSinkTenantsGetNoProactiveContextTest(TestCase):
    """The door the sink opened, closed.

    ``surface_proactive_context`` is the SECOND model-facing consumer of
    ProactiveOutbound — it prepends "[earlier-from-you]: <text>" onto inbound turns on
    every ingress path, including the one the behavior transport drives. It was
    harmless only because a synthetic tenant had no rows (everything 422'd).

    The sink creates them. The eval tenant's daily system crons (Morning Briefing,
    Evening Check-in) call ``nbhd_send_to_user`` and now record a row every day — so
    without this gate the FIRST scenario turn of every nightly would drain yesterday's
    briefing into its prompt, and the reminder scenario could read back its own previous
    water-nudge, all while the run stamped ``isolated: True``. That is the exact
    contamination this PR exists to end, re-entering through the PR's own door.
    """

    def test_an_eval_sink_tenant_rows_do_not_reach_the_model(self):
        from apps.router.proactive_context import record_proactive_outbound, surface_proactive_context

        tenant = _make(synthetic=True, eval_sink=True, username="eval-ctx")
        record_proactive_outbound(
            tenant=tenant,
            channel=ProactiveOutbound.Channel.EVAL,
            channel_user_id=str(tenant.user_id),
            message_text="Morning Briefing: here is your day",
            job_name="morning_briefing",
        )
        self.assertTrue(ProactiveOutbound.objects.filter(tenant=tenant).exists())
        self.assertEqual(surface_proactive_context(tenant=tenant), "")

    def test_a_synthetic_demo_tenant_still_gets_proactive_context(self):
        from apps.router.proactive_context import record_proactive_outbound, surface_proactive_context

        tenant = _make(synthetic=True, username="demo-ctx", telegram=777)
        record_proactive_outbound(
            tenant=tenant,
            channel=ProactiveOutbound.Channel.TELEGRAM,
            channel_user_id="777",
            message_text="Did you book the dentist?",
            job_name="nudge",
        )
        self.assertNotEqual(surface_proactive_context(tenant=tenant), "")


class EvalSinkTenantsGetNoConversationDigestTest(TestCase):
    """The USER.md "Conversation so far" block is what silently defeated the behavior
    suite's per-scenario isolation: the transport opens a fresh thread per scenario, and
    the platform then handed that thread the last turns from EVERY thread. Scenarios read
    each other's conversations — including the assistant's own prior REFUSALS, which is
    how one bad turn became self-reinforcing for a whole day."""

    def test_digest_is_empty_for_an_eval_sink_tenant(self):
        from apps.router.conversation_capture import build_conversation_digest, record_conversation_turn

        tenant = _make(synthetic=True, eval_sink=True, username="eval-digest")
        record_conversation_turn(
            tenant=tenant,
            channel="app",
            channel_user_id=str(tenant.user_id),
            user_text="hello",
            reply_text="hi there",
        )
        self.assertEqual(build_conversation_digest(tenant), "")

    def test_digest_still_renders_for_a_synthetic_demo_tenant(self):
        from apps.router.conversation_capture import build_conversation_digest, record_conversation_turn

        tenant = _make(synthetic=True, username="demo-digest")
        record_conversation_turn(
            tenant=tenant,
            channel="app",
            channel_user_id=str(tenant.user_id),
            user_text="hello",
            reply_text="hi there",
        )
        self.assertNotEqual(build_conversation_digest(tenant), "")


class EvalSinkRowIsolationTest(TestCase):
    def setUp(self):
        self.tenant = _make(synthetic=True, eval_sink=True, username="eval-history")
        self.thread = ChatThread.objects.create(tenant=self.tenant, user=self.tenant.user, is_main=True, title="Main")
        self.row = ProactiveOutbound.objects.create(
            tenant=self.tenant,
            channel=ProactiveOutbound.Channel.EVAL,
            channel_user_id=str(self.tenant.user_id),
            message_text="internal eval evidence",
            notified_at=timezone.now(),
        )

    def test_old_sink_row_does_not_appear_after_sink_flag_is_cleared(self):
        from apps.router.chat_history import build_since_page
        from apps.router.proactive_context import surface_proactive_context

        self.tenant.is_eval_sink = False
        self.tenant.save(update_fields=["is_eval_sink"])

        messages, _ = build_since_page(self.tenant, str(self.thread.id), cursor=None, limit=100)
        self.assertEqual(messages, [])
        self.assertEqual(surface_proactive_context(tenant=self.tenant), "")

    def test_sink_row_does_not_increment_unread_badge(self):
        from apps.router.push_views import _compute_unread_count

        self.tenant.user.chat_last_read_at = timezone.now() - timedelta(hours=1)
        self.tenant.user.save(update_fields=["chat_last_read_at"])
        self.assertEqual(_compute_unread_count(self.tenant.user), 0)

    @override_settings(APNS_KEY_ID="test", APNS_TEAM_ID="test", APNS_BUNDLE_ID="test")
    def test_push_claim_rejects_eval_row_even_if_called_directly(self):
        from apps.router.push_views import notify_proactive_ready

        ProactiveOutbound.objects.filter(pk=self.row.pk).update(notified_at=None)
        with (
            patch("apps.common.apns.apns_configured", return_value=True),
            patch("apps.router.push_views._push_to_user_devices") as push,
        ):
            notify_proactive_ready(self.tenant, str(self.row.id), "body")

        push.assert_not_called()
        self.row.refresh_from_db()
        self.assertIsNone(self.row.notified_at)
