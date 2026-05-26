"""Tests for the LINE Push monthly-quota state machine.

Covers:
  - State refresh + transition detection (entered_pre_warn, exhausted, recovered)
  - 429 tripwire flips state without a poll
  - Exhaustion handler: flips telegram-linked tenants, emails appropriately
  - Recovery handler: emails flipped tenants, clears flags, idempotent
  - Idempotency: re-running each handler is a no-op for the same event
  - Server-side gate: line_set_preferred_channel rejects 'line' when exhausted
"""

from __future__ import annotations

from unittest.mock import patch

from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.router import line_quota
from apps.router.line_quota import (
    USER_PREF_FLIPPED_BY_QUOTA,
    is_monthly_limit_429,
    mark_quota_exhausted_from_429,
    refresh_quota_state,
)
from apps.router.line_quota_handlers import (
    handle_exhausted,
    handle_pre_warn,
    handle_recovered,
)
from apps.router.models import LineQuotaState
from apps.tenants.models import Tenant, User


def _make_user_and_tenant(
    *,
    email: str = "u@test.com",
    preferred_channel: str = "line",
    line_user_id: str | None = "Uline",
    telegram_chat_id: int | None = 99,
) -> tuple[User, Tenant]:
    user = User.objects.create(
        username=email,
        email=email,
        display_name="Test",
        preferred_channel=preferred_channel,
        line_user_id=line_user_id,
        telegram_chat_id=telegram_chat_id,
    )
    tenant = Tenant.objects.create(user=user, status=Tenant.Status.ACTIVE)
    return user, tenant


class MonthlyLimit429DetectionTest(TestCase):
    def test_detects_monthly_limit_body(self):
        self.assertTrue(is_monthly_limit_429(429, '{"message":"You have reached your monthly limit."}'))

    def test_other_429_bodies_are_not_monthly_limit(self):
        # Generic rate-limit should NOT trigger the quota gate.
        self.assertFalse(is_monthly_limit_429(429, '{"message":"Rate limit exceeded"}'))
        self.assertFalse(is_monthly_limit_429(429, ""))

    def test_non_429_status_not_monthly_limit(self):
        self.assertFalse(is_monthly_limit_429(401, "monthly limit"))
        self.assertFalse(is_monthly_limit_429(200, "monthly limit"))


class TripwireTest(TestCase):
    def test_flips_state_on_first_call(self):
        self.assertFalse(LineQuotaState.get().is_exhausted)

        flipped = mark_quota_exhausted_from_429()

        self.assertTrue(flipped)
        self.assertTrue(LineQuotaState.get().is_exhausted)

    def test_idempotent_on_second_call(self):
        mark_quota_exhausted_from_429()
        flipped_again = mark_quota_exhausted_from_429()

        self.assertFalse(flipped_again)


class RefreshQuotaStateTest(TestCase):
    @patch("apps.router.line_quota.fetch_line_quota", return_value=(1000, 100))
    def test_steady_state_no_transitions(self, _mock):
        result = refresh_quota_state()
        self.assertEqual(result.transitions, [])
        self.assertTrue(result.polled)

    @patch("apps.router.line_quota.fetch_line_quota", return_value=(1000, 950))
    def test_entered_pre_warn(self, _mock):
        result = refresh_quota_state()
        self.assertIn("entered_pre_warn", result.transitions)
        # Not exhausted yet.
        self.assertNotIn("exhausted", result.transitions)

    @patch("apps.router.line_quota.fetch_line_quota", return_value=(1000, 1000))
    def test_exhausted_transition_fires_once(self, _mock):
        result1 = refresh_quota_state()
        self.assertIn("exhausted", result1.transitions)
        # Second poll: still exhausted but no re-transition.
        result2 = refresh_quota_state()
        self.assertNotIn("exhausted", result2.transitions)

    def test_recovered_transition(self):
        # Manually put us in exhausted state.
        state = LineQuotaState.get()
        state.line_quota_limit = 1000
        state.line_quota_used = 1000
        state.line_quota_exhausted_at = timezone.now()
        state.save()

        with patch("apps.router.line_quota.fetch_line_quota", return_value=(1000, 50)):
            result = refresh_quota_state()

        self.assertIn("recovered", result.transitions)
        state.refresh_from_db()
        self.assertFalse(state.is_exhausted)

    @patch("apps.router.line_quota.fetch_line_quota", return_value=None)
    def test_failed_poll_no_transitions_no_polled(self, _mock):
        result = refresh_quota_state()
        self.assertFalse(result.polled)
        self.assertEqual(result.transitions, [])


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="NBHD <noreply@test>",
    PLATFORM_OWNER_EMAIL="owner@nbhd.test",
    FRONTEND_URL="https://nbhd.test",
)
class HandlerExhaustedTest(TestCase):
    def setUp(self):
        super().setUp()
        mail.outbox = []
        # Pre-establish exhausted state.
        state = LineQuotaState.get()
        state.line_quota_limit = 1000
        state.line_quota_used = 1000
        state.line_quota_exhausted_at = timezone.now()
        state.save()

    def test_flips_telegram_linked_user(self):
        user, _ = _make_user_and_tenant(preferred_channel="line", telegram_chat_id=123)

        result = handle_exhausted()

        user.refresh_from_db()
        self.assertEqual(user.preferred_channel, "telegram")
        self.assertTrue(user.preferences.get(USER_PREF_FLIPPED_BY_QUOTA))
        self.assertEqual(result["flipped"], 1)
        self.assertEqual(result["emailed_line_only"], 0)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Telegram", mail.outbox[0].subject)

    def test_line_only_user_gets_connect_telegram_email(self):
        user, _ = _make_user_and_tenant(preferred_channel="line", telegram_chat_id=None)

        result = handle_exhausted()

        user.refresh_from_db()
        # Preference NOT flipped — no Telegram to flip to.
        self.assertEqual(user.preferred_channel, "line")
        self.assertFalse(user.preferences.get(USER_PREF_FLIPPED_BY_QUOTA, False))
        self.assertEqual(result["flipped"], 0)
        self.assertEqual(result["emailed_line_only"], 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_telegram_preferring_user_unaffected(self):
        # User already on Telegram — not in scope.
        user, _ = _make_user_and_tenant(
            preferred_channel="telegram",
            line_user_id="Uignored",
            telegram_chat_id=123,
        )

        result = handle_exhausted()

        user.refresh_from_db()
        self.assertEqual(user.preferred_channel, "telegram")
        self.assertEqual(result["flipped"], 0)
        self.assertEqual(result["emailed_line_only"], 0)

    def test_idempotent_on_second_call(self):
        _make_user_and_tenant(preferred_channel="line", telegram_chat_id=123)

        first = handle_exhausted()
        second = handle_exhausted()

        self.assertEqual(first["flipped"], 1)
        # Second call bails — exhausted_notified_at is set.
        self.assertEqual(second.get("skipped"), "already_notified")
        self.assertEqual(len(mail.outbox), 1)

    def test_bails_when_not_exhausted(self):
        # Reset to non-exhausted.
        state = LineQuotaState.get()
        state.line_quota_exhausted_at = None
        state.save()

        result = handle_exhausted()
        self.assertEqual(result.get("skipped"), "not_exhausted")


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="NBHD <noreply@test>",
    FRONTEND_URL="https://nbhd.test",
)
class HandlerRecoveredTest(TestCase):
    def setUp(self):
        super().setUp()
        mail.outbox = []

    def test_emails_flipped_users_and_clears_flag(self):
        # Set up: user was flipped, state is no longer exhausted, but we
        # remember a prior exhaustion event.
        user, _ = _make_user_and_tenant(preferred_channel="telegram", telegram_chat_id=123)
        line_quota.mark_user_flipped_by_quota(user)

        state = LineQuotaState.get()
        state.line_quota_exhausted_notified_at = timezone.now()  # prior event
        state.save()

        result = handle_recovered()

        user.refresh_from_db()
        # Preference stays on Telegram — recovery does NOT silently flip back.
        self.assertEqual(user.preferred_channel, "telegram")
        # Flag cleared.
        self.assertFalse(user.preferences.get(USER_PREF_FLIPPED_BY_QUOTA, False))
        self.assertEqual(result["emailed"], 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("LINE is back", mail.outbox[0].subject)

    def test_skip_when_still_exhausted(self):
        state = LineQuotaState.get()
        state.line_quota_exhausted_at = timezone.now()
        state.save()

        result = handle_recovered()
        self.assertEqual(result.get("skipped"), "still_exhausted")

    def test_skip_when_no_prior_exhaustion(self):
        # No exhausted_notified_at — never exhausted. Recovery handler
        # has nothing to recover from.
        result = handle_recovered()
        self.assertEqual(result.get("skipped"), "no_prior_exhaustion")

    def test_idempotent_on_second_call(self):
        user, _ = _make_user_and_tenant(preferred_channel="telegram", telegram_chat_id=123)
        line_quota.mark_user_flipped_by_quota(user)

        state = LineQuotaState.get()
        state.line_quota_exhausted_notified_at = timezone.now()
        state.save()

        first = handle_recovered()
        second = handle_recovered()

        self.assertEqual(first["emailed"], 1)
        self.assertEqual(second.get("skipped"), "already_notified")
        self.assertEqual(len(mail.outbox), 1)


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="NBHD <noreply@test>",
    PLATFORM_OWNER_EMAIL="owner@nbhd.test",
)
class HandlerPreWarnTest(TestCase):
    def setUp(self):
        super().setUp()
        mail.outbox = []
        state = LineQuotaState.get()
        state.line_quota_limit = 1000
        state.line_quota_used = 920  # >= 90%
        state.save()

    def test_fires_owner_email_once(self):
        first = handle_pre_warn()
        second = handle_pre_warn()

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["owner@nbhd.test"])
        self.assertIn("92%", mail.outbox[0].subject)

    @override_settings(PLATFORM_OWNER_EMAIL="")
    def test_skips_when_owner_not_set(self):
        self.assertFalse(handle_pre_warn())
        self.assertEqual(len(mail.outbox), 0)


class ServerSideGateTest(TestCase):
    """Server-side enforcement of the LINE-disabled gate in
    line_set_preferred_channel — frontend may bypass the disabled
    button via direct API call."""

    def setUp(self):
        super().setUp()
        self.user, _ = _make_user_and_tenant(preferred_channel="telegram", line_user_id="Uxxx", telegram_chat_id=42)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_rejects_line_when_exhausted(self):
        # Trip quota exhausted.
        mark_quota_exhausted_from_429()

        resp = self.client.patch(
            "/api/v1/tenants/line/preferred-channel/",
            {"preferred_channel": "line"},
            format="json",
        )

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json().get("code"), "line_quota_exhausted")

        # Preference unchanged.
        self.user.refresh_from_db()
        self.assertEqual(self.user.preferred_channel, "telegram")

    def test_accepts_telegram_when_line_exhausted(self):
        # Telegram still selectable even when LINE is gated.
        mark_quota_exhausted_from_429()

        resp = self.client.patch(
            "/api/v1/tenants/line/preferred-channel/",
            {"preferred_channel": "telegram"},
            format="json",
        )

        self.assertEqual(resp.status_code, 200)

    def test_accepts_line_when_not_exhausted(self):
        resp = self.client.patch(
            "/api/v1/tenants/line/preferred-channel/",
            {"preferred_channel": "line"},
            format="json",
        )

        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.preferred_channel, "line")


class HeartbeatGateTest(TestCase):
    """Built-in heartbeat is force-disabled for LINE-preferring tenants
    so its every-1h Push traffic doesn't keep burning the quota."""

    def test_line_preferring_tenant_gets_no_builtin_heartbeat(self):
        from apps.orchestrator.config_generator import _build_heartbeat_defaults

        user = User.objects.create(
            username="hb-line",
            email="hb-line@test.com",
            display_name="HB Line",
            preferred_channel="line",
            line_user_id="Uhb",
        )
        tenant = Tenant.objects.create(user=user, status=Tenant.Status.ACTIVE)
        tenant.experimental_built_in_heartbeat = True
        tenant.heartbeat_start_hour = 7
        tenant.heartbeat_window_hours = 14
        tenant.save()

        result = _build_heartbeat_defaults(tenant)
        # Disabled despite the flag being on.
        self.assertEqual(result, {"every": "0m"})

    def test_telegram_tenant_with_flag_gets_heartbeat(self):
        from apps.orchestrator.config_generator import _build_heartbeat_defaults

        user = User.objects.create(
            username="hb-tg",
            email="hb-tg@test.com",
            display_name="HB TG",
            preferred_channel="telegram",
            telegram_chat_id=555,
        )
        tenant = Tenant.objects.create(user=user, status=Tenant.Status.ACTIVE)
        tenant.experimental_built_in_heartbeat = True
        tenant.heartbeat_start_hour = 7
        tenant.heartbeat_window_hours = 14
        tenant.save()

        result = _build_heartbeat_defaults(tenant)
        self.assertEqual(result.get("every"), "1h")
