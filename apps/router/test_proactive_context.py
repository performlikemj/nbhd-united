"""Tests for proactive-outbound capture + envelope injection.

Covers:
* ``parse_markdown_items`` — bullet / numbered / single / mixed input.
* ``record_proactive_outbound`` — row write, parsed_items population.
* ``surface_proactive_context`` — empty case, ordering, consumption,
  follow-up-window semantics, and the split window (unconsumed rows
  surface for 7 days; consumed rows keep the tight 24h window).
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
from apps.tenants.test_utils import seed_internal_key


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
        seed_internal_key(self.tenant, key="test-key")


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
        block = surface_proactive_context(tenant=self.tenant)
        self.assertEqual(block, "")

    def test_surfaces_recent_row_and_marks_consumed(self):
        row = record_proactive_outbound(
            tenant=self.tenant,
            channel="line",
            channel_user_id="Utest",
            message_text="proactive message body",
            job_name="Evening Check-in",
        )
        block = surface_proactive_context(tenant=self.tenant)
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
        block = surface_proactive_context(tenant=self.tenant)
        self.assertIn("thread-rule", block)
        self.assertIn("[1] alpha", block)
        self.assertIn("[2] beta", block)
        self.assertIn("[3] gamma", block)

    def test_surfaced_tenant_wide_across_channels(self):
        # Surfacing is now TENANT-scoped, transport-agnostic (one tenant = one
        # human). A row recorded under ANY channel/channel_user_id surfaces on
        # the tenant's next inbound regardless of the reply transport — this is
        # the production fix (rows recorded channel='telegram'/'line' must
        # surface when the user replies from the iOS app). This inverts the old
        # ``test_scoped_per_channel_user`` assertion, which required a different
        # channel_user_id row to be EXCLUDED; that channel-scoping was the bug.
        record_proactive_outbound(
            tenant=self.tenant, channel="telegram", channel_user_id="55", message_text="from telegram cron"
        )
        record_proactive_outbound(
            tenant=self.tenant, channel="line", channel_user_id="Uxyz", message_text="from line cron"
        )
        record_proactive_outbound(
            tenant=self.tenant, channel="app", channel_user_id="u-app", message_text="from app cron"
        )
        block = surface_proactive_context(tenant=self.tenant)
        self.assertIn("from telegram cron", block)
        self.assertIn("from line cron", block)
        self.assertIn("from app cron", block)

    def test_other_tenant_rows_not_surfaced(self):
        # Tenant-wide scoping stops at the tenant boundary — a different
        # tenant's proactive rows must never leak into this tenant's turn.
        from django.contrib.auth import get_user_model

        User = get_user_model()
        other_user = User.objects.create_user(username="proactive_other", password="pw")
        other_tenant = Tenant.objects.create(user=other_user, status=Tenant.Status.ACTIVE)
        record_proactive_outbound(
            tenant=other_tenant, channel="telegram", channel_user_id="999", message_text="someone else's cron"
        )
        block = surface_proactive_context(tenant=self.tenant)
        self.assertEqual(block, "")

    def test_unconsumed_row_aged_three_days_surfaces_and_consumes(self):
        # The fix: an unconsumed proactive question that sat unanswered past
        # the old 24h window still surfaces when the user finally replies —
        # otherwise the agent has no idea what it asked (the exact amnesia
        # this module exists to prevent). It is threaded (consumed) exactly
        # once, just like a fresh row.
        row = record_proactive_outbound(
            tenant=self.tenant,
            channel="telegram",
            channel_user_id="123",
            message_text="what did you decide about the trip?",
        )
        assert row is not None
        ProactiveOutbound.objects.filter(id=row.id).update(created_at=timezone.now() - timedelta(days=3))
        block = surface_proactive_context(tenant=self.tenant)
        self.assertIn("what did you decide about the trip?", block)
        row.refresh_from_db()
        self.assertIsNotNone(row.consumed_at)

    def test_unconsumed_row_aged_eight_days_dropped(self):
        # Even the long unconsumed window has an outer bound (7 days): a
        # question that old is genuinely stale and must not resurface.
        row = record_proactive_outbound(
            tenant=self.tenant,
            channel="telegram",
            channel_user_id="123",
            message_text="ancient",
        )
        assert row is not None
        ProactiveOutbound.objects.filter(id=row.id).update(created_at=timezone.now() - timedelta(days=8))
        block = surface_proactive_context(tenant=self.tenant)
        self.assertEqual(block, "")

    def test_consumed_row_aged_two_days_not_resurfaced(self):
        # Consumed semantics are unchanged: an already-threaded message
        # aged past the tight 24h window must NOT resurface, even though an
        # UNCONSUMED row of the same age would. Re-surfacing old threaded
        # messages every turn would spam the conversation.
        row = record_proactive_outbound(
            tenant=self.tenant,
            channel="telegram",
            channel_user_id="123",
            message_text="already answered days ago",
        )
        assert row is not None
        two_days_ago = timezone.now() - timedelta(days=2)
        ProactiveOutbound.objects.filter(id=row.id).update(created_at=two_days_ago, consumed_at=two_days_ago)
        block = surface_proactive_context(tenant=self.tenant)
        self.assertEqual(block, "")

    def test_limit_respected_across_mixed_old_unconsumed_and_fresh(self):
        # The limit is shared across the union (old-unconsumed + fresh),
        # newest first: only the 3 newest surfaceable rows appear, oldest
        # of those first.
        specs = [
            ("D6", timezone.now() - timedelta(days=6)),
            ("D5", timezone.now() - timedelta(days=5)),
            ("D4", timezone.now() - timedelta(days=4)),
            ("H2", timezone.now() - timedelta(hours=2)),
            ("NOW", timezone.now() - timedelta(minutes=1)),
        ]
        for text, created in specs:
            row = record_proactive_outbound(
                tenant=self.tenant,
                channel="telegram",
                channel_user_id="123",
                message_text=text,
            )
            assert row is not None
            ProactiveOutbound.objects.filter(id=row.id).update(created_at=created)
        block = surface_proactive_context(tenant=self.tenant)
        # Only the newest 3 (D4, H2, NOW) surface; older ones are capped out.
        self.assertNotIn("D6", block)
        self.assertNotIn("D5", block)
        self.assertIn("D4", block)
        self.assertIn("H2", block)
        self.assertIn("NOW", block)
        # Rendered oldest-first.
        self.assertLess(block.index("D4"), block.index("H2"))
        self.assertLess(block.index("H2"), block.index("NOW"))

    def test_consumed_row_resurfaces_within_followup_window(self):
        row = record_proactive_outbound(
            tenant=self.tenant,
            channel="line",
            channel_user_id="Utest",
            message_text="first reach-out",
        )
        # First inbound surfaces and consumes.
        first = surface_proactive_context(tenant=self.tenant)
        self.assertNotEqual(first, "")
        # Second inbound, same minute, still sees it (follow-up window).
        second = surface_proactive_context(tenant=self.tenant)
        self.assertIn("first reach-out", second)
        # But once we push consumption past the 5-min window…
        ProactiveOutbound.objects.filter(id=row.id).update(consumed_at=timezone.now() - timedelta(minutes=10))
        third = surface_proactive_context(tenant=self.tenant)
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
        block = surface_proactive_context(tenant=self.tenant)
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
