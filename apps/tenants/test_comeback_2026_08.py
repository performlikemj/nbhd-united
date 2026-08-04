"""Tests for the targeted August 2026 comeback campaign."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from django.core import mail
from django.template.loader import render_to_string
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.tenants.models import Tenant, User
from apps.tenants.promo_models import PromoCampaign, PromoCampaignSend
from apps.tenants.tasks import (
    COMEBACK_2026_08_CODE,
    COMEBACK_2026_08_EXTENSION_DAYS,
    COMEBACK_2026_08_VALID_UNTIL,
    send_comeback_2026_08_campaign_task,
)


def _make_tenant(
    email: str,
    *,
    status=Tenant.Status.SUSPENDED,
    stripe_subscription_id="",
    is_synthetic=False,
    is_eval_sink=False,
    pending_deletion=False,
    email_opt_out=False,
) -> tuple[User, Tenant]:
    user = User.objects.create_user(
        username=email,
        email=email,
        password="test-password",
        display_name="Test",
        email_opt_out=email_opt_out,
        email_opt_out_at=timezone.now() if email_opt_out else None,
    )
    tenant = Tenant.objects.create(
        user=user,
        status=status,
        stripe_subscription_id=stripe_subscription_id,
        is_synthetic=is_synthetic,
        is_eval_sink=is_eval_sink,
        pending_deletion=pending_deletion,
        is_trial=False,
        trial_ends_at=timezone.now() - timedelta(days=1),
    )
    return user, tenant


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="NBHD <noreply@test>",
    FRONTEND_URL="https://nbhd.test",
    API_BASE_URL="https://api.nbhd.test",
)
class TargetedComebackTaskTest(TestCase):
    def setUp(self):
        super().setUp()
        mail.outbox = []

    def test_sends_only_to_supplied_tenant_ids(self):
        target_user, target = _make_tenant("target@test.com")
        _make_tenant("not-targeted@test.com")

        result = send_comeback_2026_08_campaign_task([str(target.id)])

        self.assertEqual([message.to for message in mail.outbox], [[target_user.email]])
        self.assertEqual(result["targeted"], 1)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["skipped"], 0)
        message = mail.outbox[0]
        self.assertEqual(message.subject, "Come back — a month on us, no card")
        self.assertEqual(message.extra_headers["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click")
        self.assertIn("/api/v1/tenants/unsubscribe/", message.extra_headers["List-Unsubscribe"])

        campaign = PromoCampaign.objects.get(code=COMEBACK_2026_08_CODE)
        self.assertEqual(campaign.extension_days, COMEBACK_2026_08_EXTENSION_DAYS)
        self.assertEqual(campaign.valid_until, COMEBACK_2026_08_VALID_UNTIL)
        self.assertEqual(campaign.audience_snapshot["tenant_ids"], [str(target.id)])
        self.assertTrue(PromoCampaignSend.objects.filter(campaign=campaign, user=target_user).exists())

    def test_every_defense_in_depth_skip_is_applied(self):
        eligible_user, eligible = _make_tenant("eligible@test.com", status=Tenant.Status.ACTIVE)
        _, stripe = _make_tenant("stripe@test.com", stripe_subscription_id="sub_live")
        _, synthetic = _make_tenant("synthetic@test.com", is_synthetic=True)
        _, eval_sink = _make_tenant("eval@test.com", is_eval_sink=True)
        _, deleting = _make_tenant("deleting@test.com", pending_deletion=True)
        _, wrong_status = _make_tenant("pending@test.com", status=Tenant.Status.PENDING)
        _, opted_out = _make_tenant("opted-out@test.com", email_opt_out=True)
        tenant_ids = [
            str(tenant.id) for tenant in (eligible, stripe, synthetic, eval_sink, deleting, wrong_status, opted_out)
        ]

        result = send_comeback_2026_08_campaign_task(tenant_ids)

        self.assertEqual([message.to for message in mail.outbox], [[eligible_user.email]])
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["skipped"], 6)
        self.assertEqual(
            result["skip_reasons"],
            {
                "email_opt_out": 1,
                "ineligible_status": 1,
                "is_eval_sink": 1,
                "is_synthetic": 1,
                "pending_deletion": 1,
                "stripe_subscription_id": 1,
            },
        )

    def test_refire_does_not_double_send(self):
        user, tenant = _make_tenant("once@test.com")

        first = send_comeback_2026_08_campaign_task([str(tenant.id)])
        second = send_comeback_2026_08_campaign_task([str(tenant.id)])

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(first["sent"], 1)
        self.assertEqual(second["sent"], 0)
        self.assertEqual(second["skip_reasons"], {"already_sent": 1})
        self.assertEqual(PromoCampaignSend.objects.filter(user=user).count(), 1)

    def test_campaign_deadline_is_end_of_august_31_utc(self):
        _, tenant = _make_tenant("deadline@test.com")

        send_comeback_2026_08_campaign_task([str(tenant.id)])

        campaign = PromoCampaign.objects.get(code=COMEBACK_2026_08_CODE)
        self.assertEqual(campaign.valid_until, datetime(2026, 8, 31, 23, 59, 59, tzinfo=UTC))


class ComebackTemplateRenderTest(TestCase):
    def test_text_and_html_render_all_campaign_urls_and_deadline(self):
        context = {
            "display_name": "MJ",
            "promo_url": "https://nbhd.test/promo/redeem?code=campaign&token=token",
            "unsubscribe_url": "https://api.nbhd.test/api/v1/tenants/unsubscribe/token/",
            "valid_until": datetime(2026, 8, 31, 23, 59, 59, tzinfo=UTC),
        }

        subject = render_to_string("email/comeback_2026_08/email_subject.txt", context).strip()
        text = render_to_string("email/comeback_2026_08/email_body.txt", context)
        html = render_to_string("email/comeback_2026_08/email_body.html", context)

        self.assertEqual(subject, "Come back — a month on us, no card")
        for rendered in (text, html):
            self.assertIn("https://nbhd.test/promo/redeem", rendered)
            self.assertIn(context["unsubscribe_url"], rendered)
            self.assertIn("August 31, 2026", rendered)
            self.assertIn("now on your iphone", rendered.lower())
            self.assertIn("an assistant that grows with you", rendered.lower())
            self.assertNotIn("the privacy story", rendered.lower())
        self.assertIn("A FULL MONTH FREE. ONE TAP.", text)
        self.assertIn("A full month free. One tap.", html)


class ComebackCronEndpointAuthTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.url = "/api/v1/cron/trigger/send_comeback_2026_08_campaign/"

    @patch("apps.tenants.tasks.send_comeback_2026_08_campaign_task", autospec=True)
    @patch("apps.cron.views.verify_qstash_signature", return_value=False)
    def test_rejects_unsigned_or_unauthorized_call(self, mock_verify, mock_task):
        tenant_ids = [str(uuid.uuid4())]
        response = self.client.post(
            self.url,
            data=json.dumps({"kwargs": {"tenant_ids": tenant_ids}}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"error": "Invalid signature"})
        mock_task.assert_not_called()

    @patch("apps.tenants.tasks.send_comeback_2026_08_campaign_task", autospec=True)
    @patch("apps.cron.views.verify_qstash_signature", return_value=True)
    def test_signed_call_passes_explicit_tenant_ids(self, mock_verify, mock_task):
        tenant_ids = [str(uuid.uuid4())]
        mock_task.return_value = {"sent": 1, "skipped": 0}

        response = self.client.post(
            self.url,
            data=json.dumps({"kwargs": {"tenant_ids": tenant_ids}}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        mock_task.assert_called_once_with(tenant_ids=tenant_ids)
