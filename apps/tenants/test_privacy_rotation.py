"""Tests for the June 2026 privacy-rotation campaign.

Covers:
  - User.set_password / set_unusable_password bump password_last_changed_at
  - JWT pw_iat claim is set on issue
  - JWTAuthenticationWithRLS rejects tokens minted before rotation
  - rotate_all_passwords idempotency + platform-owner exemption
  - Promo HMAC sign + verify round-trip + tamper detection
  - Promo redemption: trial extension math, idempotency, audience guards
  - Email rendering with sample context
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.tenants.models import Tenant, User
from apps.tenants.promo_models import PromoCampaign, PromoRedemption
from apps.tenants.promo_signing import make_promo_token, verify_promo_token
from apps.tenants.serializers import EmailTokenObtainPairSerializer


def _make_user(
    *,
    email="u@test.com",
    is_trial=True,
    status=Tenant.Status.ACTIVE,
    stripe_sub="",
    trial_ends_at=None,
) -> tuple[User, Tenant]:
    user = User.objects.create(
        username=email,
        email=email,
        display_name="Test",
    )
    user.set_password("pw-initial")
    user.save()
    tenant = Tenant.objects.create(
        user=user,
        status=status,
        is_trial=is_trial,
        stripe_subscription_id=stripe_sub,
        trial_ends_at=trial_ends_at or (timezone.now() + timedelta(days=3)),
    )
    return user, tenant


# ─────────────────────────────────────────────────────────────────────
# set_password / password_last_changed_at
# ─────────────────────────────────────────────────────────────────────


class SetPasswordStampTest(TestCase):
    def test_set_password_bumps_stamp(self):
        user = User.objects.create(username="a@test.com", email="a@test.com")
        self.assertIsNone(user.password_last_changed_at)

        user.set_password("hunter2")
        self.assertIsNotNone(user.password_last_changed_at)

    def test_set_password_advances_on_second_call(self):
        user = User.objects.create(username="b@test.com", email="b@test.com")
        user.set_password("first")
        first_stamp = user.password_last_changed_at

        # Simulate clock advance.
        with patch("django.utils.timezone.now", return_value=first_stamp + timedelta(seconds=5)):
            user.set_password("second")
        self.assertGreater(user.password_last_changed_at, first_stamp)

    def test_set_unusable_password_also_bumps(self):
        user = User.objects.create(username="c@test.com", email="c@test.com")
        user.set_password("hunter2")
        before = user.password_last_changed_at

        with patch("django.utils.timezone.now", return_value=before + timedelta(seconds=5)):
            user.set_unusable_password()
        self.assertGreater(user.password_last_changed_at, before)


# ─────────────────────────────────────────────────────────────────────
# JWT pw_iat force-logout
# ─────────────────────────────────────────────────────────────────────


class JWTForceLogoutTest(TestCase):
    def setUp(self):
        super().setUp()
        self.user, _ = _make_user(email="jwt@test.com")

    def test_token_carries_pw_iat_claim(self):
        token = EmailTokenObtainPairSerializer.get_token(self.user)
        self.assertEqual(int(token["pw_iat"]), int(self.user.password_last_changed_at.timestamp()))

    def test_token_with_zero_pw_iat_for_legacy_user(self):
        legacy = User.objects.create(username="legacy@test.com", email="legacy@test.com")
        token = EmailTokenObtainPairSerializer.get_token(legacy)
        self.assertEqual(int(token["pw_iat"]), 0)

    def test_pre_rotation_token_rejected_after_rotation(self):
        # Mint a token with the user's current password stamp.
        refresh = EmailTokenObtainPairSerializer.get_token(self.user)
        access = str(refresh.access_token)

        # Hit a protected endpoint — should succeed.
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        resp = client.get("/api/v1/tenants/profile/")
        self.assertEqual(resp.status_code, 200)

        # Rotate password — bumps stamp past the token's pw_iat.
        with patch(
            "django.utils.timezone.now",
            return_value=self.user.password_last_changed_at + timedelta(seconds=10),
        ):
            self.user.set_password("rotated")
            self.user.save()

        # Same token now should be rejected.
        resp = client.get("/api/v1/tenants/profile/")
        self.assertEqual(resp.status_code, 401)

    def test_post_rotation_token_accepted(self):
        # Rotate password first.
        self.user.set_password("rotated")
        self.user.save()
        refresh = EmailTokenObtainPairSerializer.get_token(self.user)
        access = str(refresh.access_token)

        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        resp = client.get("/api/v1/tenants/profile/")
        self.assertEqual(resp.status_code, 200)

    def test_legacy_user_with_no_stamp_accepts_any_token(self):
        """A user who's never had set_password called (legacy or
        seeded directly) has password_last_changed_at=None — the
        validator should not reject any token for them."""
        legacy = User.objects.create(username="leg@test.com", email="leg@test.com", is_active=True)
        refresh = RefreshToken.for_user(legacy)  # no pw_iat claim
        access = str(refresh.access_token)

        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        resp = client.get("/api/v1/tenants/profile/")
        # 200 if profile view permits no-tenant user; otherwise just
        # confirm we got past auth (any 2xx/4xx other than 401).
        self.assertNotEqual(resp.status_code, 401)


# ─────────────────────────────────────────────────────────────────────
# rotate_all_passwords command
# ─────────────────────────────────────────────────────────────────────


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="NBHD <noreply@test>",
    FRONTEND_URL="https://nbhd.test",
    PLATFORM_OWNER_EMAIL="owner@nbhd.test",
)
class RotateAllPasswordsTest(TestCase):
    def setUp(self):
        super().setUp()
        mail.outbox = []
        self.alice, _ = _make_user(email="alice@test.com")
        self.bob, _ = _make_user(email="bob@test.com")
        self.owner, _ = _make_user(email="owner@nbhd.test")

    def test_rotates_all_non_owners(self):
        call_command("rotate_all_passwords", reason="test")

        self.alice.refresh_from_db()
        self.bob.refresh_from_db()
        self.owner.refresh_from_db()

        # Alice + Bob: password unusable, stamp bumped.
        self.assertFalse(self.alice.has_usable_password())
        self.assertFalse(self.bob.has_usable_password())
        # Owner: unchanged.
        self.assertTrue(self.owner.has_usable_password())

        # Two emails sent.
        self.assertEqual(len(mail.outbox), 2)
        recipients = {m.to[0] for m in mail.outbox}
        self.assertEqual(recipients, {"alice@test.com", "bob@test.com"})

    def test_idempotent_on_rerun(self):
        cutoff = timezone.now()
        call_command("rotate_all_passwords", reason="first", since=cutoff.isoformat())
        first_sent = len(mail.outbox)

        # Second run with the same --since should be a no-op for users
        # already rotated above the cutoff.
        call_command("rotate_all_passwords", reason="second", since=cutoff.isoformat())
        self.assertEqual(len(mail.outbox), first_sent)  # no new emails

    def test_dry_run_changes_nothing(self):
        call_command("rotate_all_passwords", reason="test", dry_run=True)

        self.alice.refresh_from_db()
        self.assertTrue(self.alice.has_usable_password())
        self.assertEqual(len(mail.outbox), 0)

    def test_reset_token_in_email_validates(self):
        call_command("rotate_all_passwords", reason="test")
        alice_email = next(m for m in mail.outbox if m.to[0] == "alice@test.com")

        # Confirm the token in the URL validates against the user.
        self.alice.refresh_from_db()
        import re

        m = re.search(r"token=([^&\s]+)", alice_email.body)
        self.assertIsNotNone(m)
        token = m.group(1)
        self.assertTrue(default_token_generator.check_token(self.alice, token))


# ─────────────────────────────────────────────────────────────────────
# Promo HMAC signing
# ─────────────────────────────────────────────────────────────────────


class PromoSigningTest(TestCase):
    def test_round_trip(self):
        token = make_promo_token("camp-1", "user-abc")
        self.assertEqual(verify_promo_token("camp-1", token), "user-abc")

    def test_mismatched_campaign_rejected(self):
        token = make_promo_token("camp-1", "user-abc")
        self.assertIsNone(verify_promo_token("camp-2", token))

    def test_tampered_token_rejected(self):
        token = make_promo_token("camp-1", "user-abc")
        tampered = token[:-1] + ("0" if token[-1] != "0" else "1")
        self.assertIsNone(verify_promo_token("camp-1", tampered))

    def test_garbage_token_rejected(self):
        self.assertIsNone(verify_promo_token("camp-1", "not-a-real-token"))


# ─────────────────────────────────────────────────────────────────────
# Promo redemption view
# ─────────────────────────────────────────────────────────────────────


@override_settings(FRONTEND_URL="https://nbhd.test")
class PromoRedemptionTest(TestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.campaign = PromoCampaign.objects.create(
            code="june-2026",
            kind=PromoCampaign.Kind.TRIAL_EXTENSION,
            extension_days=14,
            valid_until=timezone.now() + timedelta(days=7),
        )
        self.user, self.tenant = _make_user(
            email="redeem@test.com",
            is_trial=True,
            status=Tenant.Status.ACTIVE,
            trial_ends_at=timezone.now() + timedelta(days=2),
        )

    def _redeem(self, code, token):
        return self.client.get(f"/api/v1/tenants/promos/redeem/?code={code}&token={token}")

    def test_success_extends_trial_by_14_days(self):
        token = make_promo_token(self.campaign.code, self.user.id)
        before_end = self.tenant.trial_ends_at

        resp = self._redeem(self.campaign.code, token)

        self.assertEqual(resp.status_code, 302)
        self.assertIn("status=success", resp["Location"])

        self.tenant.refresh_from_db()
        self.assertGreater(self.tenant.trial_ends_at, before_end)
        # 14 days added to (now or existing end, whichever is later)
        delta = self.tenant.trial_ends_at - before_end
        self.assertGreaterEqual(delta, timedelta(days=14) - timedelta(minutes=1))

    def test_extension_uses_now_when_trial_already_expired(self):
        # Move trial_ends_at into the past.
        self.tenant.trial_ends_at = timezone.now() - timedelta(days=5)
        self.tenant.save()

        token = make_promo_token(self.campaign.code, self.user.id)
        self._redeem(self.campaign.code, token)

        self.tenant.refresh_from_db()
        # New end should be roughly 14 days from now, not from the past
        # trial_ends_at (which would land at -5 + 14 = +9 days).
        delta = self.tenant.trial_ends_at - timezone.now()
        self.assertGreater(delta, timedelta(days=13, hours=23))

    def test_double_click_does_not_double_extend(self):
        token = make_promo_token(self.campaign.code, self.user.id)

        self._redeem(self.campaign.code, token)
        self.tenant.refresh_from_db()
        after_first = self.tenant.trial_ends_at

        resp2 = self._redeem(self.campaign.code, token)
        self.assertEqual(resp2.status_code, 302)
        self.assertIn("status=already", resp2["Location"])

        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.trial_ends_at, after_first)
        self.assertEqual(PromoRedemption.objects.filter(campaign=self.campaign, user=self.user).count(), 1)

    def test_tampered_token_rejected(self):
        token = make_promo_token(self.campaign.code, self.user.id)
        tampered = token[:-2] + "xx"
        resp = self._redeem(self.campaign.code, tampered)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("status=invalid", resp["Location"])
        self.assertEqual(PromoRedemption.objects.count(), 0)

    def test_expired_campaign_rejected(self):
        self.campaign.valid_until = timezone.now() - timedelta(seconds=10)
        self.campaign.save()

        token = make_promo_token(self.campaign.code, self.user.id)
        resp = self._redeem(self.campaign.code, token)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("status=expired", resp["Location"])

    def test_paying_subscriber_not_extended(self):
        # Realistic paying-subscriber state: stripe sub set, is_trial
        # already False (the trial-to-paid transition cleared it).
        self.tenant.stripe_subscription_id = "sub_123"
        self.tenant.is_trial = False
        original_trial_end = self.tenant.trial_ends_at
        self.tenant.save()

        token = make_promo_token(self.campaign.code, self.user.id)
        resp = self._redeem(self.campaign.code, token)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("status=active_subscription", resp["Location"])

        self.tenant.refresh_from_db()
        # is_trial stayed False (we didn't flip a paying subscriber back to trial).
        self.assertFalse(self.tenant.is_trial)
        # trial_ends_at unchanged.
        self.assertEqual(self.tenant.trial_ends_at, original_trial_end)
        # Redemption row recorded with the ALREADY_SUBSCRIBED outcome.
        red = PromoRedemption.objects.get(campaign=self.campaign, user=self.user)
        self.assertEqual(red.outcome, PromoRedemption.Outcome.ALREADY_SUBSCRIBED)

    def test_unknown_campaign_rejected(self):
        token = make_promo_token("unknown-campaign", self.user.id)
        resp = self._redeem("unknown-campaign", token)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("status=invalid", resp["Location"])

    def test_missing_params_rejected(self):
        resp = self.client.get("/api/v1/tenants/promos/redeem/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("status=invalid", resp["Location"])


# ─────────────────────────────────────────────────────────────────────
# send_promo_campaign command
# ─────────────────────────────────────────────────────────────────────


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="NBHD <noreply@test>",
    FRONTEND_URL="https://nbhd.test",
    PLATFORM_OWNER_EMAIL="owner@nbhd.test",
)
class SendPromoCampaignTest(TestCase):
    def setUp(self):
        super().setUp()
        mail.outbox = []

    def _run(self, **kwargs):
        defaults = {
            "code": "test-camp",
            "kind": "trial_extension",
            "days": 14,
            "valid_until": (timezone.now() + timedelta(days=7)).isoformat(),
        }
        defaults.update(kwargs)
        call_command("send_promo_campaign", **defaults)

    def test_active_trial_user_emailed(self):
        _make_user(email="a@test.com", is_trial=True, status=Tenant.Status.ACTIVE)
        self._run()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["a@test.com"])

    def test_suspended_never_subscribed_emailed(self):
        _make_user(
            email="b@test.com",
            is_trial=False,
            status=Tenant.Status.SUSPENDED,
            stripe_sub="",
        )
        self._run()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["b@test.com"])

    def test_paying_subscriber_skipped(self):
        _make_user(
            email="c@test.com",
            is_trial=False,
            status=Tenant.Status.ACTIVE,
            stripe_sub="sub_123",
        )
        self._run()
        self.assertEqual(len(mail.outbox), 0)

    def test_owner_skipped(self):
        _make_user(email="owner@nbhd.test", is_trial=True, status=Tenant.Status.ACTIVE)
        self._run()
        self.assertEqual(len(mail.outbox), 0)

    def test_campaign_row_created_with_audience_snapshot(self):
        _make_user(email="d@test.com", is_trial=True, status=Tenant.Status.ACTIVE)
        self._run(code="ccc")
        camp = PromoCampaign.objects.get(code="ccc")
        self.assertEqual(camp.extension_days, 14)
        self.assertEqual(camp.audience_snapshot.get("captured_at_count"), 1)

    def test_dry_run_sends_nothing(self):
        _make_user(email="e@test.com", is_trial=True, status=Tenant.Status.ACTIVE)
        self._run(dry_run=True)
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(PromoCampaign.objects.exists())

    def test_promo_url_in_email_redeems(self):
        user, _ = _make_user(email="f@test.com", is_trial=True, status=Tenant.Status.ACTIVE)
        self._run(code="round-trip")

        # Extract the promo URL from the email and verify it carries a
        # valid token for the user. urlencode percent-escapes ":" in the
        # token's timestamp prefix, so the token value may contain "%"
        # — match permissively up to whitespace/newline.
        import re

        body = mail.outbox[0].body
        m = re.search(r"code=([\w\-]+)&token=([^\s<>\"]+)", body)
        self.assertIsNotNone(m, f"no promo URL found in body:\n{body}")
        from urllib.parse import unquote

        code = m.group(1)
        token = unquote(m.group(2))
        self.assertEqual(verify_promo_token(code, token), str(user.id))


# ─────────────────────────────────────────────────────────────────────
# Email template rendering
# ─────────────────────────────────────────────────────────────────────


class EmailRenderingTest(TestCase):
    """Sanity check that the templates render without errors given
    sample context. Catches template-syntax breakage before it ships."""

    def test_email_1_renders(self):
        from django.template.loader import render_to_string

        ctx = {"display_name": "MJ", "reset_url": "https://nbhd.test/reset?t=x"}
        subject = render_to_string("email/privacy_rotation_2026/email_1_subject.txt", ctx)
        text = render_to_string("email/privacy_rotation_2026/email_1_body.txt", ctx)
        html = render_to_string("email/privacy_rotation_2026/email_1_body.html", ctx)

        self.assertIn("password", subject.lower())
        self.assertIn("MJ", text)
        self.assertIn("https://nbhd.test/reset?t=x", text)
        self.assertIn("https://nbhd.test/reset?t=x", html)

    def test_email_2_renders(self):
        from django.template.loader import render_to_string

        ctx = {
            "display_name": "MJ",
            "promo_url": "https://nbhd.test/promo/redeem?code=x&token=y",
        }
        subject = render_to_string("email/privacy_rotation_2026/email_2_subject.txt", ctx)
        text = render_to_string("email/privacy_rotation_2026/email_2_body.txt", ctx)
        html = render_to_string("email/privacy_rotation_2026/email_2_body.html", ctx)

        self.assertIn("14 days", subject)
        self.assertIn("MJ", text)
        self.assertIn("https://nbhd.test/promo/redeem", html)
