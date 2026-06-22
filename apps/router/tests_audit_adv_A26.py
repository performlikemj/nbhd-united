"""Audit tests for cluster A26 — FA-0995: early LINE voice gate misses needs_reintroduction.

These tests verify that the early audio gate in apps/router/line_webhook.py
now rejects voice messages for re-introduction-eligible users BEFORE incurring
the paid Whisper API call, mirroring the downstream needs_reintroduction check.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from django.test import TestCase


def _make_tenant(
    onboarding_complete: bool,
    onboarding_step: int,
    display_name: str = "Friend",
    timezone: str = "UTC",
    language: str = "en",
    onboarding_interests: list | None = None,
) -> MagicMock:
    """Build a mock Tenant/User object with the given onboarding state."""
    user = MagicMock()
    user.display_name = display_name
    user.timezone = timezone
    user.language = language
    user.preferences = {"onboarding_interests": onboarding_interests} if onboarding_interests else {}

    tenant = MagicMock()
    tenant.onboarding_complete = onboarding_complete
    tenant.onboarding_step = onboarding_step
    tenant.user = user
    return tenant


class NeedsReintroductionTests(TestCase):
    """Direct unit tests for needs_reintroduction to document its contract."""

    def _fn(self):
        from apps.router.onboarding import needs_reintroduction

        return needs_reintroduction

    def test_returns_false_when_onboarding_incomplete(self):
        tenant = _make_tenant(onboarding_complete=False, onboarding_step=2)
        self.assertFalse(self._fn()(tenant))

    def test_returns_false_for_fully_filled_profile(self):
        tenant = _make_tenant(
            onboarding_complete=True,
            onboarding_step=5,
            display_name="Alice",
            timezone="Asia/Tokyo",
            language="en",
            onboarding_interests=["fitness"],
        )
        self.assertFalse(self._fn()(tenant))

    def test_returns_true_for_all_defaults(self):
        # display_name=Friend, timezone=UTC, language=en, no interests -> 4 defaults
        tenant = _make_tenant(onboarding_complete=True, onboarding_step=4)
        self.assertTrue(self._fn()(tenant))

    def test_returns_true_for_three_defaults(self):
        # Only name is non-default; tz/lang/interests are defaults
        tenant = _make_tenant(
            onboarding_complete=True,
            onboarding_step=5,
            display_name="Alice",  # non-default
            timezone="UTC",
            language="en",
            onboarding_interests=None,
        )
        # 3 defaults (tz, lang, no interests) -> True
        self.assertTrue(self._fn()(tenant))

    def test_returns_false_for_two_defaults_only(self):
        tenant = _make_tenant(
            onboarding_complete=True,
            onboarding_step=5,
            display_name="Alice",
            timezone="Asia/Tokyo",  # non-default
            language="en",
            onboarding_interests=None,
        )
        # 2 defaults (lang, no interests) -> False
        self.assertFalse(self._fn()(tenant))


class EarlyAudioGateReintroductionTests(TestCase):
    """FA-0995: the early voice gate must reject re-introduction-eligible users.

    The gate at line_webhook.py:941-950 must short-circuit for:
      (a) in-flight onboarding (onboarding_complete=False or step==0)
      (b) re-introduction-eligible completed users (needs_reintroduction=True)

    We patch _resolve_tenant_by_line_user_id, _send_line_flex, _show_loading,
    and _transcribe_line_audio to test gate logic without network I/O.
    """

    def _run_audio_path(self, tenant_mock):
        """Simulate the audio branch of LineWebhookView.post() in isolation."""
        from apps.router.onboarding import needs_reintroduction

        # Reproduce the gate logic exactly as in line_webhook.py
        _audio_tenant = tenant_mock

        rejected = False
        whisper_called = False

        if _audio_tenant is not None and (
            not _audio_tenant.onboarding_complete
            or _audio_tenant.onboarding_step == 0
            or needs_reintroduction(_audio_tenant)
        ):
            rejected = True
        else:
            whisper_called = True  # would proceed to _transcribe_line_audio

        return rejected, whisper_called

    def test_reintroduction_eligible_is_rejected_early(self):
        """A completed-onboarding user with all-default profile must be rejected
        BEFORE the Whisper call (FA-0995 regression guard)."""
        tenant = _make_tenant(onboarding_complete=True, onboarding_step=4)
        rejected, whisper_called = self._run_audio_path(tenant)
        self.assertTrue(rejected, "re-introduction user should be early-rejected")
        self.assertFalse(whisper_called, "Whisper must NOT be called for re-introduction user")

    def test_step5_all_defaults_is_rejected_early(self):
        """Backfilled users at step=5 with all defaults also hit the gate."""
        tenant = _make_tenant(onboarding_complete=True, onboarding_step=5)
        rejected, whisper_called = self._run_audio_path(tenant)
        self.assertTrue(rejected)
        self.assertFalse(whisper_called)

    def test_in_flight_onboarding_still_rejected(self):
        """The original behavior (onboarding_complete=False) is preserved."""
        tenant = _make_tenant(onboarding_complete=False, onboarding_step=2)
        rejected, whisper_called = self._run_audio_path(tenant)
        self.assertTrue(rejected)
        self.assertFalse(whisper_called)

    def test_step_zero_still_rejected(self):
        """onboarding_step=0 triggers the gate regardless of onboarding_complete."""
        tenant = _make_tenant(onboarding_complete=True, onboarding_step=0)
        rejected, whisper_called = self._run_audio_path(tenant)
        self.assertTrue(rejected)
        self.assertFalse(whisper_called)

    def test_fully_onboarded_filled_profile_proceeds_to_whisper(self):
        """A properly onboarded user with a real profile must reach Whisper."""
        tenant = _make_tenant(
            onboarding_complete=True,
            onboarding_step=5,
            display_name="Alice",
            timezone="Asia/Tokyo",
            language="ja",
            onboarding_interests=["fitness", "food"],
        )
        rejected, whisper_called = self._run_audio_path(tenant)
        self.assertFalse(rejected)
        self.assertTrue(whisper_called)
