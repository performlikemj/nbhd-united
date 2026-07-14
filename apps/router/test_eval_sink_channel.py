"""The ``eval`` sink channel, and the USER.md digest suppression that goes with it.

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

THE SAFETY PROPERTY, and the reason every test below leans on it: the sink is gated on
``Tenant.is_synthetic``. A REAL user with no delivery surface still gets a 422, because
that is a genuine error and must stay loud — and because a real user's message must
NEVER be capturable by an eval sink.
"""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.router.cron_delivery import _rate_counts, resolve_user_channel
from apps.router.models import DeviceToken, ProactiveOutbound
from apps.tenants.models import Tenant
from apps.tenants.test_utils import seed_internal_key

User = get_user_model()


def _make(*, synthetic: bool, username: str, telegram: int | None = None) -> Tenant:
    user = User.objects.create_user(username=username, password="x")
    if telegram is not None:
        user.telegram_chat_id = telegram
        user.save(update_fields=["telegram_chat_id"])
    return Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE,
        is_synthetic=synthetic,
    )


class ResolveUserChannelSinkTest(TestCase):
    def test_synthetic_tenant_with_no_channel_resolves_to_the_sink(self):
        tenant = _make(synthetic=True, username="synth-nochan")
        self.assertEqual(resolve_user_channel(tenant.user), "eval")

    def test_REAL_tenant_with_no_channel_still_has_none(self):
        """The safety gate. A real user with no delivery surface is a genuine 422 —
        it must stay loud, and their content must never be capturable by an eval sink.
        If this ever returns "eval", a real subscriber's proactive messages start
        landing in eval storage instead of on their phone."""
        tenant = _make(synthetic=False, username="real-nochan")
        self.assertIsNone(resolve_user_channel(tenant.user))

    def test_the_sink_is_a_LAST_resort_not_a_hijack(self):
        # A synthetic tenant that DOES have a real channel keeps using it — the sink
        # only catches the no-surface case.
        tenant = _make(synthetic=True, username="synth-tg", telegram=4242)
        self.assertEqual(resolve_user_channel(tenant.user), "telegram")

    def test_the_sink_does_not_preempt_a_registered_device(self):
        tenant = _make(synthetic=True, username="synth-ios")
        DeviceToken.objects.create(
            tenant=tenant, user=tenant.user, token="a" * 64, environment="sandbox", bundle_id="x.y.z"
        )
        self.assertEqual(resolve_user_channel(tenant.user), "app")


@override_settings(TELEGRAM_BOT_TOKEN="test-token", NBHD_INTERNAL_API_KEY="test-key")
class CronDeliveryToTheSinkTest(TestCase):
    def setUp(self):
        self.tenant = _make(synthetic=True, username="synth-delivery")
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

    def test_the_sink_sends_NOTHING_anywhere(self):
        """A sink that leaks is worse than no sink. No Telegram, no LINE, no APNs."""
        with (
            patch("apps.router.cron_delivery.CronDeliveryView._send_via_telegram") as tg,
            patch("apps.router.cron_delivery.CronDeliveryView._send_via_line") as line,
            patch("apps.router.proactive_context._dispatch_ios_push") as push,
        ):
            resp = self.client.post(self.url, {"message": "hydrate"}, format="json", **self._headers())

        self.assertEqual(resp.status_code, 200)
        tg.assert_not_called()
        line.assert_not_called()
        # No device tokens exist, so the push dispatcher is reached but finds nothing.
        # What matters is that no external transport was invoked.
        self.assertFalse(tg.called or line.called)
        self.assertLessEqual(push.call_count, 1)

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


class SyntheticTenantsGetNoProactiveContextTest(TestCase):
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

    def test_a_synthetic_tenants_sink_rows_never_reach_the_model(self):
        from apps.router.proactive_context import record_proactive_outbound, surface_proactive_context

        tenant = _make(synthetic=True, username="synth-ctx")
        record_proactive_outbound(
            tenant=tenant,
            channel=ProactiveOutbound.Channel.EVAL,
            channel_user_id=str(tenant.user_id),
            message_text="Morning Briefing: here is your day",
            job_name="morning_briefing",
        )
        self.assertTrue(ProactiveOutbound.objects.filter(tenant=tenant).exists())
        self.assertEqual(surface_proactive_context(tenant=tenant), "")

    def test_a_real_tenant_still_gets_its_proactive_context(self):
        """The suppression is scoped to synthetic tenants — this block is a real product
        feature (it stops the assistant being amnesiac about its own proactive sends)."""
        from apps.router.proactive_context import record_proactive_outbound, surface_proactive_context

        tenant = _make(synthetic=False, username="real-ctx", telegram=777)
        record_proactive_outbound(
            tenant=tenant,
            channel=ProactiveOutbound.Channel.TELEGRAM,
            channel_user_id="777",
            message_text="Did you book the dentist?",
            job_name="nudge",
        )
        self.assertNotEqual(surface_proactive_context(tenant=tenant), "")


class SyntheticTenantsGetNoConversationDigestTest(TestCase):
    """The USER.md "Conversation so far" block is what silently defeated the behavior
    suite's per-scenario isolation: the transport opens a fresh thread per scenario, and
    the platform then handed that thread the last turns from EVERY thread. Scenarios read
    each other's conversations — including the assistant's own prior REFUSALS, which is
    how one bad turn became self-reinforcing for a whole day."""

    def test_digest_is_empty_for_a_synthetic_tenant(self):
        from apps.router.conversation_capture import build_conversation_digest, record_conversation_turn

        tenant = _make(synthetic=True, username="synth-digest")
        record_conversation_turn(
            tenant=tenant,
            channel="app",
            channel_user_id=str(tenant.user_id),
            user_text="hello",
            reply_text="hi there",
        )
        self.assertEqual(build_conversation_digest(tenant), "")

    def test_digest_still_renders_for_a_real_tenant(self):
        """The suppression must be scoped to synthetic tenants — this block is a real
        product feature (it grounds proactive turns), not eval scaffolding."""
        from apps.router.conversation_capture import build_conversation_digest, record_conversation_turn

        tenant = _make(synthetic=False, username="real-digest")
        record_conversation_turn(
            tenant=tenant,
            channel="app",
            channel_user_id=str(tenant.user_id),
            user_text="hello",
            reply_text="hi there",
        )
        self.assertNotEqual(build_conversation_digest(tenant), "")
