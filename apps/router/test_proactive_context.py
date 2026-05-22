"""Tests for proactive-outbound capture + envelope injection.

Covers:
* ``parse_markdown_items`` — bullet / numbered / single / mixed input.
* ``record_proactive_outbound`` — row write, parsed_items population.
* ``surface_proactive_context`` — empty case, ordering, consumption,
  follow-up-window semantics.
* ``CronDeliveryView`` — happy-path Telegram + LINE send now produces
  a ``ProactiveOutbound`` row with job_name from the header.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.router.cron_delivery import _rate_counts
from apps.router.models import ProactiveOutbound
from apps.router.proactive_context import (
    parse_markdown_items,
    record_proactive_outbound,
    surface_proactive_context,
)
from apps.tenants.models import Tenant


class ParseMarkdownItemsTest(TestCase):
    def test_dash_bullets(self):
        items = parse_markdown_items("- one\n- two\n- three")
        self.assertEqual(items, ["one", "two", "three"])

    def test_asterisk_bullets(self):
        items = parse_markdown_items("* alpha\n* beta")
        self.assertEqual(items, ["alpha", "beta"])

    def test_numbered_dot(self):
        items = parse_markdown_items("1. first\n2. second\n3. third")
        self.assertEqual(items, ["first", "second", "third"])

    def test_numbered_paren(self):
        items = parse_markdown_items("1) a\n2) b")
        self.assertEqual(items, ["a", "b"])

    def test_unicode_bullet(self):
        items = parse_markdown_items("• one\n• two")
        self.assertEqual(items, ["one", "two"])

    def test_indented_items_extracted_as_top_level(self):
        # The simple parser intentionally doesn't model nesting; both
        # outer and inner items show up. This keeps anchors flat for
        # the agent to map paragraphs against.
        items = parse_markdown_items("- outer\n  - inner")
        self.assertEqual(items, ["outer", "inner"])

    def test_single_item_returns_empty(self):
        # A single item isn't a "structure" the agent should map against.
        items = parse_markdown_items("- just one")
        self.assertEqual(items, [])

    def test_no_list_returns_empty(self):
        items = parse_markdown_items("just a paragraph with no bullets")
        self.assertEqual(items, [])

    def test_mixed_prose_and_bullets(self):
        text = (
            "A few things have been quiet for a bit:\n\n"
            "- Yard Talk presentation prep\n"
            "- Security Champions training data\n"
            "- Hugging Face POC\n\n"
            "No pressure if you're focused on other things."
        )
        self.assertEqual(
            parse_markdown_items(text),
            [
                "Yard Talk presentation prep",
                "Security Champions training data",
                "Hugging Face POC",
            ],
        )


class _TenantFixture(TestCase):
    """Shared tenant + user setup."""

    def setUp(self):
        from django.contrib.auth import get_user_model

        User = get_user_model()
        self.user = User.objects.create_user(username="proactive_test", password="pw")
        self.user.telegram_chat_id = 99999
        self.user.line_user_id = "Utestuserabc123"
        self.user.save()
        self.tenant = Tenant.objects.create(user=self.user, status=Tenant.Status.ACTIVE)


class RecordProactiveOutboundTest(_TenantFixture):
    def test_writes_row_with_parsed_items(self):
        row = record_proactive_outbound(
            tenant=self.tenant,
            channel="line",
            channel_user_id="Utest",
            message_text="Hey:\n- one\n- two",
            job_name="Morning Briefing",
        )
        assert row is not None
        self.assertEqual(row.channel, "line")
        self.assertEqual(row.job_name, "Morning Briefing")
        self.assertEqual(row.parsed_items, ["one", "two"])

    def test_empty_parsed_items_when_no_list(self):
        row = record_proactive_outbound(
            tenant=self.tenant,
            channel="telegram",
            channel_user_id="123",
            message_text="just a plain message",
        )
        assert row is not None
        self.assertEqual(row.parsed_items, [])

    def test_job_name_truncated(self):
        row = record_proactive_outbound(
            tenant=self.tenant,
            channel="telegram",
            channel_user_id="123",
            message_text="x",
            job_name="X" * 200,
        )
        assert row is not None
        self.assertEqual(len(row.job_name), 64)


class SurfaceProactiveContextTest(_TenantFixture):
    def test_empty_when_no_rows(self):
        block = surface_proactive_context(tenant=self.tenant, channel="line", channel_user_id="Unone")
        self.assertEqual(block, "")

    def test_surfaces_recent_row_and_marks_consumed(self):
        row = record_proactive_outbound(
            tenant=self.tenant,
            channel="line",
            channel_user_id="Utest",
            message_text="proactive message body",
            job_name="Evening Check-in",
        )
        block = surface_proactive_context(tenant=self.tenant, channel="line", channel_user_id="Utest")
        self.assertIn("earlier-from-you", block)
        self.assertIn("proactive message body", block)
        self.assertIn("Evening Check-in", block)
        row.refresh_from_db()
        self.assertIsNotNone(row.consumed_at)

    def test_structured_items_render_with_anchors_and_guidance(self):
        record_proactive_outbound(
            tenant=self.tenant,
            channel="line",
            channel_user_id="Utest",
            message_text="things:\n- alpha\n- beta\n- gamma",
        )
        block = surface_proactive_context(tenant=self.tenant, channel="line", channel_user_id="Utest")
        self.assertIn("thread-rule", block)
        self.assertIn("[1] alpha", block)
        self.assertIn("[2] beta", block)
        self.assertIn("[3] gamma", block)

    def test_scoped_per_channel_user(self):
        record_proactive_outbound(
            tenant=self.tenant,
            channel="line",
            channel_user_id="UserA",
            message_text="for A",
        )
        block_b = surface_proactive_context(tenant=self.tenant, channel="line", channel_user_id="UserB")
        self.assertEqual(block_b, "")

    def test_stale_row_outside_window_dropped(self):
        row = record_proactive_outbound(
            tenant=self.tenant,
            channel="telegram",
            channel_user_id="123",
            message_text="ancient",
        )
        assert row is not None
        ProactiveOutbound.objects.filter(id=row.id).update(created_at=timezone.now() - timedelta(hours=48))
        block = surface_proactive_context(tenant=self.tenant, channel="telegram", channel_user_id="123")
        self.assertEqual(block, "")

    def test_consumed_row_resurfaces_within_followup_window(self):
        row = record_proactive_outbound(
            tenant=self.tenant,
            channel="line",
            channel_user_id="Utest",
            message_text="first reach-out",
        )
        # First inbound surfaces and consumes.
        first = surface_proactive_context(tenant=self.tenant, channel="line", channel_user_id="Utest")
        self.assertNotEqual(first, "")
        # Second inbound, same minute, still sees it (follow-up window).
        second = surface_proactive_context(tenant=self.tenant, channel="line", channel_user_id="Utest")
        self.assertIn("first reach-out", second)
        # But once we push consumption past the 5-min window…
        ProactiveOutbound.objects.filter(id=row.id).update(consumed_at=timezone.now() - timedelta(minutes=10))
        third = surface_proactive_context(tenant=self.tenant, channel="line", channel_user_id="Utest")
        self.assertEqual(third, "")

    def test_multiple_rows_ordered_oldest_first(self):
        first = record_proactive_outbound(
            tenant=self.tenant,
            channel="telegram",
            channel_user_id="123",
            message_text="OLDER",
        )
        assert first is not None
        ProactiveOutbound.objects.filter(id=first.id).update(created_at=timezone.now() - timedelta(hours=3))
        record_proactive_outbound(
            tenant=self.tenant,
            channel="telegram",
            channel_user_id="123",
            message_text="NEWER",
        )
        block = surface_proactive_context(tenant=self.tenant, channel="telegram", channel_user_id="123")
        # Oldest first so the agent reads in conversational order.
        self.assertLess(block.index("OLDER"), block.index("NEWER"))


@override_settings(
    TELEGRAM_BOT_TOKEN="test-token",
    NBHD_INTERNAL_API_KEY="test-key",
)
class CronDeliveryRecordsProactiveOutboundTest(_TenantFixture):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.url = f"/api/v1/integrations/runtime/{self.tenant.id}/send-to-user/"
        _rate_counts.clear()

    def _headers(self, job_name: str | None = None):
        h = {
            "HTTP_X_NBHD_INTERNAL_KEY": "test-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }
        if job_name:
            h["HTTP_X_NBHD_JOB_NAME"] = job_name
        return h

    @patch("apps.router.cron_delivery.httpx.Client")
    def test_telegram_send_records_outbound_with_job_name(self, mock_client_cls):
        mock_http = MagicMock()
        mock_resp = MagicMock()
        mock_resp.is_success = True
        mock_resp.status_code = 200
        mock_http.post.return_value = mock_resp
        mock_http.__enter__ = MagicMock(return_value=mock_http)
        mock_http.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_http

        resp = self.client.post(
            self.url,
            {"message": "Hi:\n- one\n- two"},
            format="json",
            **self._headers(job_name="Morning Briefing"),
        )
        self.assertEqual(resp.status_code, 200)

        rows = list(ProactiveOutbound.objects.filter(tenant=self.tenant))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.channel, "telegram")
        self.assertEqual(row.channel_user_id, str(self.user.telegram_chat_id))
        self.assertEqual(row.job_name, "Morning Briefing")
        self.assertEqual(row.parsed_items, ["one", "two"])

    @patch("apps.router.cron_delivery.httpx.Client")
    def test_failed_send_does_not_record(self, mock_client_cls):
        mock_http = MagicMock()
        mock_resp = MagicMock()
        mock_resp.is_success = False
        mock_resp.status_code = 502
        mock_resp.text = "boom"
        mock_http.post.return_value = mock_resp
        mock_http.__enter__ = MagicMock(return_value=mock_http)
        mock_http.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_http

        self.client.post(self.url, {"message": "anything"}, format="json", **self._headers())
        self.assertEqual(ProactiveOutbound.objects.filter(tenant=self.tenant).count(), 0)
