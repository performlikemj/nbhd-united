"""Tests for the July 2026 comeback win-back campaign.

Covers the machinery added on top of the existing promo/campaign flow:
  - ``--audience comeback`` selection (includes paid-then-lapsed SUSPENDED
    and hibernated ACTIVE; excludes owner / never-onboarded / never-messaged
    / opted-out / DELETED)
  - audience-snapshot freeze records the new mode
  - redemption restores runtime for a reactivated SUSPENDED tenant (Azure
    mocked) and survives an Azure failure via the hibernated_at fallback
  - one-click unsubscribe GET + POST set the flag idempotently; bad token 404
  - the send loop sets List-Unsubscribe headers and skips opted-out users
  - the comeback template renders (placeholders intact) given full context
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.tenants.models import Tenant, User
from apps.tenants.promo_models import PromoCampaign
from apps.tenants.promo_signing import make_promo_token
from apps.tenants.unsubscribe_signing import make_unsubscribe_token, verify_unsubscribe_token


def _make_user(
    *,
    email: str,
    status=Tenant.Status.ACTIVE,
    is_trial=True,
    stripe_sub="",
    onboarding_complete=True,
    has_messaged=True,
    hibernated=False,
    opted_out=False,
    container_id="oc-test",
    trial_ends_at=None,
) -> tuple[User, Tenant]:
    user = User.objects.create(
        username=email,
        email=email,
        display_name="Test",
        email_opt_out=opted_out,
        email_opt_out_at=(timezone.now() if opted_out else None),
    )
    user.set_password("pw-initial")
    user.save()
    tenant = Tenant.objects.create(
        user=user,
        status=status,
        is_trial=is_trial,
        stripe_subscription_id=stripe_sub,
        onboarding_complete=onboarding_complete,
        last_message_at=(timezone.now() if has_messaged else None),
        hibernated_at=(timezone.now() if hibernated else None),
        container_id=container_id,
        trial_ends_at=trial_ends_at or (timezone.now() + timedelta(days=2)),
    )
    return user, tenant


# ─────────────────────────────────────────────────────────────────────
# Comeback audience selection
# ─────────────────────────────────────────────────────────────────────


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="NBHD <noreply@test>",
    FRONTEND_URL="https://nbhd.test",
    API_BASE_URL="https://api.nbhd.test",
    PLATFORM_OWNER_EMAIL="owner@nbhd.test",
)
class ComebackAudienceTest(TestCase):
    def setUp(self):
        super().setUp()
        mail.outbox = []

    def _run(self, **kwargs):
        defaults = {
            "code": "comeback-test",
            "kind": "trial_extension",
            "days": 14,
            "valid_until": (timezone.now() + timedelta(days=7)).isoformat(),
            "template_base": "email/comeback_2026_07/email",
            "audience": "comeback",
        }
        defaults.update(kwargs)
        call_command("send_promo_campaign", **defaults)

    def _recipients(self) -> set[str]:
        return {m.to[0] for m in mail.outbox}

    def test_includes_paid_then_lapsed_suspended(self):
        # SUSPENDED with a RETAINED stripe_subscription_id — the paid-then-lapsed
        # cohort the default filter drops but comeback deliberately keeps.
        _make_user(email="lapsed@test.com", status=Tenant.Status.SUSPENDED, is_trial=False, stripe_sub="sub_123")
        self._run()
        self.assertEqual(self._recipients(), {"lapsed@test.com"})

    def test_includes_hibernated_active(self):
        _make_user(email="hib@test.com", status=Tenant.Status.ACTIVE, hibernated=True)
        self._run()
        self.assertEqual(self._recipients(), {"hib@test.com"})

    def test_excludes_never_onboarded(self):
        _make_user(email="raw@test.com", onboarding_complete=False)
        self._run()
        self.assertEqual(self._recipients(), set())

    def test_excludes_never_messaged(self):
        _make_user(email="quiet@test.com", has_messaged=False)
        self._run()
        self.assertEqual(self._recipients(), set())

    def test_excludes_opted_out(self):
        _make_user(email="gone@test.com", opted_out=True)
        self._run()
        self.assertEqual(self._recipients(), set())

    def test_excludes_owner(self):
        _make_user(email="owner@nbhd.test")
        self._run()
        self.assertEqual(self._recipients(), set())

    def test_excludes_deleted(self):
        _make_user(email="del@test.com", status=Tenant.Status.DELETED)
        self._run()
        self.assertEqual(self._recipients(), set())

    def test_mixed_population(self):
        _make_user(email="lapsed@test.com", status=Tenant.Status.SUSPENDED, is_trial=False, stripe_sub="sub_1")
        _make_user(email="hib@test.com", status=Tenant.Status.ACTIVE, hibernated=True)
        _make_user(email="raw@test.com", onboarding_complete=False)
        _make_user(email="quiet@test.com", has_messaged=False)
        _make_user(email="gone@test.com", opted_out=True)
        _make_user(email="owner@nbhd.test")
        self._run()
        self.assertEqual(self._recipients(), {"lapsed@test.com", "hib@test.com"})

    def test_snapshot_freeze_records_mode(self):
        _make_user(email="a@test.com")
        self._run(code="snap-camp")
        camp = PromoCampaign.objects.get(code="snap-camp")
        self.assertEqual(camp.audience_snapshot.get("captured_at_count"), 1)
        self.assertEqual(camp.audience_snapshot.get("audience_mode"), "comeback")
        # Re-run does NOT widen the frozen snapshot.
        _make_user(email="b@test.com")
        self._run(code="snap-camp")
        camp.refresh_from_db()
        self.assertEqual(camp.audience_snapshot.get("captured_at_count"), 1)

    def test_default_audience_unchanged_and_excludes_opted_out(self):
        # Default mode: active-trial in, opted-out out.
        _make_user(email="trial@test.com", status=Tenant.Status.ACTIVE, is_trial=True)
        _make_user(email="trialout@test.com", status=Tenant.Status.ACTIVE, is_trial=True, opted_out=True)
        # Paid-then-lapsed is NOT in the default audience.
        _make_user(email="lapsed@test.com", status=Tenant.Status.SUSPENDED, is_trial=False, stripe_sub="sub_1")
        self._run(code="default-camp", template_base="email/ios_relaunch_2026/email", audience="default")
        self.assertEqual(self._recipients(), {"trial@test.com"})


# ─────────────────────────────────────────────────────────────────────
# List-Unsubscribe headers on the send
# ─────────────────────────────────────────────────────────────────────


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="NBHD <noreply@test>",
    FRONTEND_URL="https://nbhd.test",
    API_BASE_URL="https://api.nbhd.test",
    PLATFORM_OWNER_EMAIL="owner@nbhd.test",
)
class SendHeadersTest(TestCase):
    def setUp(self):
        super().setUp()
        mail.outbox = []

    def test_list_unsubscribe_headers_present(self):
        _make_user(email="a@test.com")
        call_command(
            "send_promo_campaign",
            code="hdr-camp",
            kind="trial_extension",
            days=14,
            valid_until=(timezone.now() + timedelta(days=7)).isoformat(),
            template_base="email/comeback_2026_07/email",
            audience="comeback",
        )
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        headers = msg.extra_headers
        self.assertIn("List-Unsubscribe", headers)
        self.assertEqual(headers["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click")
        # The header points at the backend unsubscribe endpoint, angle-bracketed.
        self.assertTrue(headers["List-Unsubscribe"].startswith("<https://api.nbhd.test/api/v1/tenants/unsubscribe/"))
        self.assertTrue(headers["List-Unsubscribe"].endswith(">"))
        # HTML alternative is still attached.
        self.assertTrue(any(mime == "text/html" for _, mime in msg.alternatives))

    def test_opted_out_user_not_sent(self):
        _make_user(email="in@test.com")
        _make_user(email="out@test.com", opted_out=True)
        call_command(
            "send_promo_campaign",
            code="skip-camp",
            kind="trial_extension",
            days=14,
            valid_until=(timezone.now() + timedelta(days=7)).isoformat(),
            template_base="email/comeback_2026_07/email",
            audience="comeback",
        )
        self.assertEqual({m.to[0] for m in mail.outbox}, {"in@test.com"})


# ─────────────────────────────────────────────────────────────────────
# Redemption → runtime restore for SUSPENDED tenants
# ─────────────────────────────────────────────────────────────────────


@override_settings(FRONTEND_URL="https://nbhd.test")
class RedemptionRestoreTest(TestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.campaign = PromoCampaign.objects.create(
            code="comeback-2026-07",
            kind=PromoCampaign.Kind.TRIAL_EXTENSION,
            extension_days=14,
            valid_until=timezone.now() + timedelta(days=7),
        )

    def _redeem(self, user):
        token = make_promo_token(self.campaign.code, user.id)
        return self.client.get(f"/api/v1/tenants/promos/redeem/?code={self.campaign.code}&token={token}")

    @patch("apps.orchestrator.services.refresh_system_cron_rows_from_seed")
    @patch("apps.cron.publish.publish_task")
    @patch("apps.orchestrator.azure_client.scale_container_app")
    def test_suspended_redeem_restores_runtime(self, mock_scale, mock_publish, mock_refresh):
        mock_refresh.return_value = {"created": 0, "updated": 0, "preserved_custom": 0}
        user, tenant = _make_user(
            email="lapsed@test.com",
            status=Tenant.Status.SUSPENDED,
            is_trial=False,
            stripe_sub="sub_lapsed",
            container_id="oc-lapsed",
        )

        resp = self._redeem(user)

        self.assertEqual(resp.status_code, 302)
        self.assertIn("status=success", resp["Location"])
        # Runtime restore scaled the container back to a single replica.
        mock_scale.assert_called_once_with("oc-lapsed", min_replicas=1, max_replicas=1)
        # Cron resume enqueued.
        resume = [c for c in mock_publish.call_args_list if c.args and c.args[0] == "resume_tenant_crons"]
        self.assertEqual(len(resume), 1)

        tenant.refresh_from_db()
        self.assertEqual(tenant.status, Tenant.Status.ACTIVE)
        self.assertTrue(tenant.is_trial)
        # Restore succeeded → no hibernated_at fallback needed.
        self.assertIsNone(tenant.hibernated_at)
        # The dead subscription id is cleared so trial expiry can enforce later.
        self.assertEqual(tenant.stripe_subscription_id, "")

    @patch("apps.orchestrator.services.refresh_system_cron_rows_from_seed")
    @patch("apps.cron.publish.publish_task")
    @patch("apps.orchestrator.azure_client.scale_container_app")
    def test_lapsed_redeemer_re_suspendable_after_trial(self, mock_scale, mock_publish, mock_refresh):
        """Regression: a paid-then-lapsed SUSPENDED tenant that redeems must
        become re-suspendable once the granted trial expires. If the dead
        stripe_subscription_id were left set, ``_unentitled_active_tenants()``
        would exclude it forever (permanent free service). This test fails on
        the pre-fix code (sub id retained)."""
        from apps.cron.views import _unentitled_active_tenants

        mock_refresh.return_value = {"created": 0, "updated": 0, "preserved_custom": 0}
        user, tenant = _make_user(
            email="lapsed3@test.com",
            status=Tenant.Status.SUSPENDED,
            is_trial=False,
            stripe_sub="sub_lapsed3",
            container_id="oc-lapsed3",
        )

        self._redeem(user)
        tenant.refresh_from_db()
        self.assertEqual(tenant.stripe_subscription_id, "")

        # Before expiry the tenant is on a valid trial → NOT swept.
        self.assertNotIn(tenant, list(_unentitled_active_tenants()))

        # Advance past the granted trial window.
        tenant.trial_ends_at = timezone.now() - timedelta(minutes=1)
        tenant.save(update_fields=["trial_ends_at"])

        # Now the ACTIVE, sub-less, expired-trial tenant IS eligible for the
        # expire-trials sweep to re-suspend.
        self.assertIn(tenant, list(_unentitled_active_tenants()))

    @patch("apps.orchestrator.services.refresh_system_cron_rows_from_seed")
    @patch("apps.cron.publish.publish_task")
    @patch("apps.orchestrator.azure_client.scale_container_app")
    def test_subscription_deleted_after_redeem_is_safe_noop(self, mock_scale, mock_publish, mock_refresh):
        """A subscription.deleted webhook for the now-cleared sub id must not
        deprovision the freshly-granted trial tenant — with the id cleared,
        _find_tenant_for_stripe_event returns None, so it's a no-op."""
        from apps.billing.services import handle_subscription_deleted

        mock_refresh.return_value = {"created": 0, "updated": 0, "preserved_custom": 0}
        user, tenant = _make_user(
            email="lapsed4@test.com",
            status=Tenant.Status.SUSPENDED,
            is_trial=False,
            stripe_sub="sub_lapsed4",
            container_id="oc-lapsed4",
        )

        self._redeem(user)

        # Stripe finally fires subscription.deleted for the OLD id.
        handle_subscription_deleted({"id": "sub_lapsed4", "status": "canceled"})

        tenant.refresh_from_db()
        # Tenant stays a live trial — not deprovisioned/deleted.
        self.assertEqual(tenant.status, Tenant.Status.ACTIVE)
        self.assertTrue(tenant.is_trial)

    @patch("apps.orchestrator.azure_client.scale_container_app", side_effect=RuntimeError("azure down"))
    def test_restore_failure_falls_back_to_hibernated(self, mock_scale):
        user, tenant = _make_user(
            email="lapsed2@test.com",
            status=Tenant.Status.SUSPENDED,
            is_trial=False,
            stripe_sub="sub_lapsed2",
            container_id="oc-lapsed2",
        )

        resp = self._redeem(user)

        # The page still succeeds even though Azure hiccuped.
        self.assertEqual(resp.status_code, 302)
        self.assertIn("status=success", resp["Location"])

        tenant.refresh_from_db()
        self.assertEqual(tenant.status, Tenant.Status.ACTIVE)
        # Fallback: hibernated_at is stamped so the next inbound message
        # self-heals the container via wake_hibernated_tenant.
        self.assertIsNotNone(tenant.hibernated_at)

    @patch("apps.billing.services.restore_tenant_runtime")
    def test_active_never_subscribed_does_not_restore(self, mock_restore):
        # A plain ACTIVE trial tenant (not suspended) extends without any
        # runtime restore.
        user, tenant = _make_user(email="act@test.com", status=Tenant.Status.ACTIVE, is_trial=True)
        resp = self._redeem(user)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("status=success", resp["Location"])
        mock_restore.assert_not_called()

    def test_active_paying_subscriber_still_rejected(self):
        # Regression guard: an ACTIVE tenant with a live subscription is a
        # real payer — must NOT be flipped to trial by the comeback change.
        user, tenant = _make_user(
            email="payer@test.com",
            status=Tenant.Status.ACTIVE,
            is_trial=False,
            stripe_sub="sub_active",
        )
        resp = self._redeem(user)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("status=active_subscription", resp["Location"])
        tenant.refresh_from_db()
        self.assertFalse(tenant.is_trial)


# ─────────────────────────────────────────────────────────────────────
# One-click unsubscribe
# ─────────────────────────────────────────────────────────────────────


class UnsubscribeSigningTest(TestCase):
    def test_round_trip(self):
        token = make_unsubscribe_token("user-abc")
        self.assertEqual(verify_unsubscribe_token(token), "user-abc")

    def test_tampered_rejected(self):
        token = make_unsubscribe_token("user-abc")
        tampered = token[:-1] + ("0" if token[-1] != "0" else "1")
        self.assertIsNone(verify_unsubscribe_token(tampered))

    def test_garbage_rejected(self):
        self.assertIsNone(verify_unsubscribe_token("not-a-token"))


class UnsubscribeViewTest(TestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.user, _ = _make_user(email="sub@test.com")

    def _url(self, token):
        return f"/api/v1/tenants/unsubscribe/{token}/"

    def test_get_does_not_mutate_and_shows_form(self):
        # GET is read-only: a mail scanner (SafeLinks/Proofpoint) GETting the
        # link must NOT opt the user out. The page offers a POST form instead.
        token = make_unsubscribe_token(self.user.id)
        resp = self.client.get(self._url(token))
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"<form", resp.content.lower())
        self.assertIn(b'method="post"', resp.content.lower())
        self.user.refresh_from_db()
        self.assertFalse(self.user.email_opt_out)
        self.assertIsNone(self.user.email_opt_out_at)

    def test_form_post_sets_flag(self):
        token = make_unsubscribe_token(self.user.id)
        resp = self.client.post(self._url(token))
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"unsubscribed", resp.content.lower())
        self.user.refresh_from_db()
        self.assertTrue(self.user.email_opt_out)
        self.assertIsNotNone(self.user.email_opt_out_at)

    def test_one_click_post_sets_flag(self):
        # RFC 8058 one-click: the mail client auto-POSTs this body.
        token = make_unsubscribe_token(self.user.id)
        resp = self.client.post(
            self._url(token),
            data="List-Unsubscribe=One-Click",
            content_type="application/x-www-form-urlencoded",
        )
        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.email_opt_out)

    def test_idempotent_post_preserves_original_timestamp(self):
        token = make_unsubscribe_token(self.user.id)
        self.client.post(self._url(token))
        self.user.refresh_from_db()
        first_stamp = self.user.email_opt_out_at
        self.assertIsNotNone(first_stamp)

        # Second POST must not move the original opt-out timestamp.
        self.client.post(self._url(token))
        self.user.refresh_from_db()
        self.assertTrue(self.user.email_opt_out)
        self.assertEqual(self.user.email_opt_out_at, first_stamp)

    def test_invalid_token_404_on_get_and_post(self):
        self.assertEqual(self.client.get(self._url("not-a-real-token")).status_code, 404)
        self.assertEqual(self.client.post(self._url("not-a-real-token")).status_code, 404)
        # A bad-token POST leaves the real user untouched.
        self.user.refresh_from_db()
        self.assertFalse(self.user.email_opt_out)

    def test_unknown_user_404(self):
        import uuid

        token = make_unsubscribe_token(uuid.uuid4())
        self.assertEqual(self.client.get(self._url(token)).status_code, 404)
        self.assertEqual(self.client.post(self._url(token)).status_code, 404)


# ─────────────────────────────────────────────────────────────────────
# Template rendering (final copy spliced)
# ─────────────────────────────────────────────────────────────────────


class ComebackTemplateRenderTest(TestCase):
    def _ctx(self):
        from datetime import UTC, datetime

        return {
            "display_name": "MJ",
            "promo_url": "https://nbhd.test/promo/redeem?code=x&token=y",
            "unsubscribe_url": "https://api.nbhd.test/api/v1/tenants/unsubscribe/tok/",
            "valid_until": datetime(2026, 7, 20, tzinfo=UTC),
        }

    def test_renders_with_context(self):
        from django.template.loader import render_to_string

        ctx = self._ctx()
        text = render_to_string("email/comeback_2026_07/email_body.txt", ctx)
        html = render_to_string("email/comeback_2026_07/email_body.html", ctx)

        # Django variables are substituted.
        self.assertIn("MJ", html)  # greeting "Hi MJ," in the HTML hero
        self.assertIn("https://nbhd.test/promo/redeem", html)
        self.assertIn("https://nbhd.test/promo/redeem", text)
        self.assertIn("https://api.nbhd.test/api/v1/tenants/unsubscribe/tok/", html)
        self.assertIn("https://api.nbhd.test/api/v1/tenants/unsubscribe/tok/", text)
        # Deadline renders from the date, not hardcoded.
        self.assertIn("July 20, 2026", html)
        self.assertIn("July 20, 2026", text)
        # App Store link + app-icon artwork present in the HTML.
        self.assertIn("apps.apple.com/us/app/nbhd/id6779158519", html)
        self.assertIn("images/icon-textured.png", html)

    def test_final_copy_spliced_no_markers(self):
        from django.template.loader import render_to_string

        ctx = self._ctx()
        subject = render_to_string("email/comeback_2026_07/email_subject.txt", ctx)
        text = render_to_string("email/comeback_2026_07/email_body.txt", ctx)
        html = render_to_string("email/comeback_2026_07/email_body.html", ctx)

        # No unspliced copy placeholders remain anywhere.
        for rendered in (subject, text, html):
            self.assertNotIn("[[COPY:", rendered)

        # Final approved copy landed (spot-check across sections + subject).
        self.assertIn("two weeks on us", subject.lower())
        self.assertIn("Two weeks free. One tap.", html)
        self.assertIn("Now on your iPhone", html)
        self.assertIn("An assistant that grows with you", html)
        self.assertIn("The privacy story, told straight", html)
        self.assertIn("benched 225", text)
