"""Tests for proactive-outbound capture + envelope injection.

Covers:
* ``parse_markdown_items`` — bullet / numbered / single / mixed input.
* ``record_proactive_outbound`` — row write, parsed_items population.
* ``surface_proactive_context`` — empty case, ordering, consumption,
  follow-up-window semantics, the split window (unconsumed rows surface
  for 7 days; consumed rows keep the tight 24h window),
  unconsumed-first prioritization under the limit, and the always-on
  ``[answer-binding]`` guidance appended to every surfaced block.
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


def _large_table(rows: int = 26) -> str:
    lines = ["| Name | Value |", "| --- | --- |"]
    lines.extend(f"| row {index} | value {index} |" for index in range(rows))
    return "\n".join(lines)


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

    def test_flag_on_stores_summary_chip_and_parses_shortened_text(self):
        from apps.journal.models import Document

        self.tenant.experimental_reply_artifacts_to_journal = True
        self.tenant.save(update_fields=["experimental_reply_artifacts_to_journal"])
        with patch("apps.router.proactive_context.parse_markdown_items", wraps=parse_markdown_items) as parser:
            row = record_proactive_outbound(
                tenant=self.tenant,
                channel="app",
                channel_user_id="app-user",
                message_text=_large_table() + "\n\n- keep one\n- keep two",
                job_name="Morning Briefing",
                artifact_dedup_key="delivery-123",
            )
        assert row is not None
        self.assertIn("Saved the full table (26 rows)", row.message_text)
        self.assertIn("| Name | Value |", row.message_text)
        self.assertIn("| row 2 | value 2 |", row.message_text)
        self.assertNotIn("| row 3 | value 3 |", row.message_text)
        self.assertEqual(row.parsed_items, ["keep one", "keep two"])
        parser.assert_called_once_with(row.message_text)
        self.assertEqual(row.journal_link["kind"], "project")
        doc = Document.objects.get(tenant=self.tenant, slug=row.journal_link["slug"])
        self.assertIn("| row 25 | value 25 |", doc.markdown)

    def test_stable_key_converges_artifact_across_proactive_retries(self):
        from apps.journal.models import Document

        self.tenant.experimental_reply_artifacts_to_journal = True
        self.tenant.save(update_fields=["experimental_reply_artifacts_to_journal"])
        for _ in range(2):
            record_proactive_outbound(
                tenant=self.tenant,
                channel="telegram",
                channel_user_id="123",
                message_text=_large_table(),
                job_name="Report",
                artifact_dedup_key="stable-delivery-key",
            )
        self.assertEqual(Document.objects.filter(tenant=self.tenant).count(), 1)
        self.assertEqual(ProactiveOutbound.objects.filter(tenant=self.tenant).count(), 2)

    def test_content_date_fallback_converges_identical_proactive_retries(self):
        from apps.journal.models import Document

        self.tenant.experimental_reply_artifacts_to_journal = True
        self.tenant.save(update_fields=["experimental_reply_artifacts_to_journal"])
        for _ in range(2):
            record_proactive_outbound(
                tenant=self.tenant,
                channel="telegram",
                channel_user_id="123",
                message_text=_large_table(),
                job_name="Report without a delivery id",
            )
        self.assertEqual(Document.objects.filter(tenant=self.tenant).count(), 1)

    @patch("apps.journal.reply_artifacts.upsert_reply_artifact", side_effect=RuntimeError("db down"))
    def test_artifact_failure_falls_back_to_inline_clamp(self, _artifact_write):
        from apps.journal.models import Document

        self.tenant.experimental_reply_artifacts_to_journal = True
        self.tenant.save(update_fields=["experimental_reply_artifacts_to_journal"])
        row = record_proactive_outbound(
            tenant=self.tenant,
            channel="telegram",
            channel_user_id="123",
            message_text=_large_table() + ("x" * 17000),
            journal_link={"kind": "daily", "slug": "2026-07-15", "title": "Original"},
        )
        assert row is not None
        self.assertEqual(len(row.message_text), 16000)
        self.assertIn("| Name | Value |", row.message_text)
        self.assertEqual(
            row.journal_link,
            {"kind": "daily", "slug": "2026-07-15", "title": "Original"},
        )
        self.assertFalse(Document.objects.filter(tenant=self.tenant).exists())


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

    def test_journal_reference_is_model_only_and_stays_placeholder_space(self):
        record_proactive_outbound(
            tenant=self.tenant,
            channel="app",
            channel_user_id="app-user",
            message_text="Saved report",
            journal_link={
                "kind": "project",
                "slug": "assistant-table-abc",
                "title": "Table for [PERSON_1]",
            },
        )
        block = surface_proactive_context(tenant=self.tenant)
        self.assertIn(
            "[journal-ref: project|assistant-table-abc|Table for [PERSON_1]; retrieve with nbhd_document_get]",
            block,
        )

    def test_answer_binding_guidance_present_when_any_row_surfaces(self):
        # Always-on binding rule (2026-07-11 canary incident: "energy 1-10?"
        # answered "6.5 today" got logged as SLEEP HOURS even though the
        # [earlier-from-you] block was surfaced). Any surfaced turn must
        # carry the [answer-binding] guidance — including plain, unstructured
        # messages that don't trigger the thread-rule.
        record_proactive_outbound(
            tenant=self.tenant,
            channel="app",
            channel_user_id="u-app",
            message_text="where did energy land today, 1-10?",
        )
        block = surface_proactive_context(tenant=self.tenant)
        self.assertIn("[answer-binding:", block)
        self.assertIn("scale and units", block)
        # Plain message: no structured thread-rule, but binding rule anyway.
        self.assertNotIn("thread-rule", block)

    def test_answer_binding_guidance_absent_when_nothing_surfaces(self):
        # The guidance ships inside the block, so a turn with nothing to
        # surface carries no guidance text at all.
        block = surface_proactive_context(tenant=self.tenant)
        self.assertEqual(block, "")
        self.assertNotIn("answer-binding", block)

    def test_answer_binding_coexists_with_thread_rule_pinned_order(self):
        # Structured message → BOTH guidances render. Pinned order:
        # thread-rule prefix first, then the [earlier-from-you] message
        # parts, then [answer-binding] LAST — adjacent to the user's reply,
        # so its "the messages above" phrasing reads literally.
        record_proactive_outbound(
            tenant=self.tenant,
            channel="line",
            channel_user_id="Utest",
            message_text="check-in:\n- energy 1-10?\n- sleep hours?",
        )
        block = surface_proactive_context(tenant=self.tenant)
        self.assertIn("thread-rule", block)
        self.assertIn("[answer-binding:", block)
        self.assertLess(block.index("thread-rule"), block.index("earlier-from-you"))
        self.assertLess(block.index("earlier-from-you"), block.index("[answer-binding:"))

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

    def test_old_unconsumed_not_crowded_out_by_consumed_followups(self):
        # The crowding scenario the prioritization exists for: at real
        # volume (~3-4 proactive sends/day), pure newest-first selection
        # across the union would let fresh rows push a days-old unanswered
        # question out of the cap — recreating the amnesia despite the
        # 7-day window. Unconsumed rows claim the limit first; consumed
        # follow-ups only fill the slots left over (here: none).
        now = timezone.now()
        for text, created in [
            ("OLD_QUESTION", now - timedelta(days=3)),
            ("FRESH_U1", now - timedelta(hours=2)),
            ("FRESH_U2", now - timedelta(hours=1)),
        ]:
            row = record_proactive_outbound(
                tenant=self.tenant,
                channel="telegram",
                channel_user_id="123",
                message_text=text,
            )
            assert row is not None
            ProactiveOutbound.objects.filter(id=row.id).update(created_at=created)
        for text in ("FOLLOWUP_C1", "FOLLOWUP_C2"):
            row = record_proactive_outbound(
                tenant=self.tenant,
                channel="telegram",
                channel_user_id="123",
                message_text=text,
            )
            assert row is not None
            # Newer than every unconsumed row AND inside the 5-min
            # follow-up window — pure recency would have picked these.
            ProactiveOutbound.objects.filter(id=row.id).update(
                created_at=now - timedelta(minutes=30),
                consumed_at=now - timedelta(minutes=1),
            )
        block = surface_proactive_context(tenant=self.tenant)
        # All three unconsumed rows surface — including the 3-day-old one.
        self.assertIn("OLD_QUESTION", block)
        self.assertIn("FRESH_U1", block)
        self.assertIn("FRESH_U2", block)
        # Consumed follow-ups are trimmed: no slots left after unconsumed.
        self.assertNotIn("FOLLOWUP_C1", block)
        self.assertNotIn("FOLLOWUP_C2", block)

    def test_four_unconsumed_rows_newest_three_win(self):
        # Residual edge, documented honestly: when MORE than ``limit``
        # unconsumed questions are pending, the newest ones win and the
        # oldest is still crowded out. Prioritization protects unconsumed
        # rows from consumed follow-ups, not from each other.
        specs = [
            ("U_D6", timezone.now() - timedelta(days=6)),
            ("U_D4", timezone.now() - timedelta(days=4)),
            ("U_H2", timezone.now() - timedelta(hours=2)),
            ("U_NOW", timezone.now() - timedelta(minutes=1)),
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
        # Only the newest 3 unconsumed rows surface; the oldest is capped out.
        self.assertNotIn("U_D6", block)
        self.assertIn("U_D4", block)
        self.assertIn("U_H2", block)
        self.assertIn("U_NOW", block)
        # Rendered oldest-first.
        self.assertLess(block.index("U_D4"), block.index("U_H2"))
        self.assertLess(block.index("U_H2"), block.index("U_NOW"))

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

    @override_settings(LINE_CHANNEL_ACCESS_TOKEN="line-token")
    @patch("apps.router.cron_delivery.httpx.Client")
    def test_line_send_records_outbound_for_line_only_user(self, mock_client_cls):
        # LINE-ONLY user (fixture links both; drop Telegram here). The resolver is
        # app → telegram → line, so the cron LINE leg is reached by LINKAGE alone.
        # Pins _send_via_line end-to-end: without this, a telegram-first fallback
        # can shadow the LINE send out of coverage entirely and nothing fails.
        self.user.telegram_chat_id = None
        self.user.save(update_fields=["telegram_chat_id"])

        mock_http = MagicMock()
        mock_resp = MagicMock()
        mock_resp.is_success = True
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"sentMessages": [{"id": "1"}]}
        mock_http.post.return_value = mock_resp
        mock_http.__enter__ = MagicMock(return_value=mock_http)
        mock_http.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_http

        resp = self.client.post(
            self.url,
            {"message": "Evening:\n- one\n- two"},
            format="json",
            **self._headers(job_name="Evening Digest"),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("api.line.me", mock_http.post.call_args.args[0])

        rows = list(ProactiveOutbound.objects.filter(tenant=self.tenant))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.channel, "line")
        self.assertEqual(row.channel_user_id, self.user.line_user_id)
        self.assertEqual(row.job_name, "Evening Digest")
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


@override_settings(
    NBHD_INTERNAL_API_KEY="test-key",
    NBHD_DISABLE_BACKGROUND_THREADS=True,
)
class CronDeliveryAppChannelIsDurableTest(_TenantFixture):
    """On the app channel the ProactiveOutbound row IS the delivery (it produces
    the APNs push and the ``?since=`` feed entry) — not an audit trail of a send
    that already happened elsewhere.

    ``record_proactive_outbound`` swallows write failures and returns None BY
    DESIGN ("losing the audit row is a smaller wrong than 500ing the cron tool
    call"), which is correct for telegram/line. On app that same best-effort
    write would let the view answer 200 "sent" while delivering NOTHING, and the
    cron would never retry. So the row is persisted BEFORE the response and the
    response is gated on it.
    """

    def setUp(self):
        super().setUp()
        from apps.router.models import DeviceToken

        # iOS-only user: drop both messaging links and register a device so
        # resolve_user_channel (app → telegram → line) picks "app".
        self.user.telegram_chat_id = None
        self.user.line_user_id = None
        self.user.save(update_fields=["telegram_chat_id", "line_user_id"])
        DeviceToken.objects.create(tenant=self.tenant, user=self.user, token="d" * 64)
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

    @patch("apps.router.proactive_context._dispatch_ios_push")
    def test_app_send_persists_row_then_returns_200(self, _push):
        resp = self.client.post(
            self.url,
            {"message": "Morning:\n- one\n- two\n[[journal-link: daily|2026-07-14|Morning Report]]"},
            format="json",
            **self._headers(job_name="Morning Briefing"),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["channel"], "app")

        rows = list(ProactiveOutbound.objects.filter(tenant=self.tenant))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.channel, "app")
        self.assertEqual(row.channel_user_id, str(self.user.id))
        self.assertEqual(row.job_name, "Morning Briefing")
        self.assertEqual(row.parsed_items, ["one", "two"])
        # The journal deep-link still rides the app row (the chip the ?since=
        # feed renders) — it must survive the record-before-respond reorder.
        self.assertEqual(
            row.journal_link,
            {"kind": "daily", "slug": "2026-07-14", "title": "Morning Report"},
        )
        self.assertNotIn("journal-link", row.message_text)
        # Counted against the runaway-cron cap exactly once.
        self.assertEqual(len(_rate_counts.get(str(self.tenant.id), [])), 1)

    @patch("apps.router.proactive_context.record_proactive_outbound", return_value=None)
    def test_app_row_write_failure_returns_retryable_5xx_not_200(self, mock_record):
        # The row write lost, so NOTHING was delivered. The view must not claim
        # success — it must hand QStash a retryable 5xx so the cron runs again.
        resp = self.client.post(self.url, {"message": "anything"}, format="json", **self._headers())

        mock_record.assert_called_once()
        self.assertGreaterEqual(resp.status_code, 500)
        self.assertEqual(resp.json()["error"], "app_delivery_not_recorded")
        # A delivery that never happened must not burn the hourly budget.
        self.assertEqual(_rate_counts.get(str(self.tenant.id), []), [])
