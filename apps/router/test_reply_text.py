"""Assistant reply storage bounds across client-history write paths."""

from __future__ import annotations

from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from apps.router.models import AppChatMessage, ChatThread, PendingMessage
from apps.router.pending_queue import _store_ios_turn_reply
from apps.router.proactive_context import record_proactive_outbound
from apps.router.reply_text import (
    DEFAULT_REPLY_TEXT_MAX_CHARS,
    REPLY_TEXT_TRUNCATION_SUFFIX,
    clamp_reply_text,
)
from apps.tenants.models import Tenant, User


class ClampReplyTextTest(SimpleTestCase):
    def test_boundary_is_unchanged(self):
        text = "x" * DEFAULT_REPLY_TEXT_MAX_CHARS

        self.assertEqual(clamp_reply_text(text), text)

    def test_overflow_includes_suffix_within_limit(self):
        result = clamp_reply_text("x" * (DEFAULT_REPLY_TEXT_MAX_CHARS + 1))

        self.assertEqual(len(result), DEFAULT_REPLY_TEXT_MAX_CHARS)
        self.assertTrue(result.endswith(REPLY_TEXT_TRUNCATION_SUFFIX))


class ReplyTextStorePathTest(TestCase):
    def setUp(self):
        from apps.journal.models import Document

        self.user = User.objects.create_user(username="reply_clamp", password="pw")
        self.tenant = Tenant.objects.create(user=self.user, status=Tenant.Status.ACTIVE)
        Document.objects.bulk_create(
            [
                Document(
                    tenant=self.tenant,
                    kind=Document.Kind.DAILY,
                    slug="2026-07-15",
                    title="Daily Note",
                    markdown="# 2026-07-15",
                )
            ]
        )

    @patch("apps.router.push_views.notify_app_reply_ready")
    def test_ios_drain_extracts_trailing_markers_before_clamp(self, _mock_notify):
        thread = ChatThread.objects.create(tenant=self.tenant, user=self.user, is_main=True)
        AppChatMessage.objects.create(
            tenant=self.tenant,
            user=self.user,
            thread=thread,
            client_msg_id="huge-reply",
            user_text="show me the table",
            status=AppChatMessage.Status.PENDING,
        )
        pending = PendingMessage.objects.create(
            tenant=self.tenant,
            channel=PendingMessage.Channel.IOS,
            channel_user_id=str(thread.id),
            payload={"client_msg_id": "huge-reply"},
            user_text="show me the table",
        )
        reply = (
            "x" * (DEFAULT_REPLY_TEXT_MAX_CHARS - 5)
            + "[[chart:bar]]"
            + "\nMEDIA:/workspace/chart.png"
            + "\n[[insight:journal/test]]Visible insight[[/insight]]"
            + ("y" * 500)
            + "\n[[journal-link: daily|2026-07-15|Daily Note]]"
            + "\n[[quick-replies: Open note | Done]]"
        )

        _store_ios_turn_reply(self.tenant, [pending], reply)

        stored = AppChatMessage.objects.get(tenant=self.tenant, client_msg_id="huge-reply")
        self.assertEqual(len(stored.reply_text), DEFAULT_REPLY_TEXT_MAX_CHARS)
        self.assertTrue(stored.reply_text.endswith(REPLY_TEXT_TRUNCATION_SUFFIX))
        self.assertNotIn("[[cha", stored.reply_text)
        self.assertNotIn("MEDIA:", stored.reply_text)
        self.assertNotIn("[[insight:", stored.reply_text)
        self.assertNotIn("[[journal-link:", stored.reply_text)
        self.assertNotIn("[[quick-replies:", stored.reply_text)
        self.assertEqual(stored.quick_replies, ["Open note", "Done"])
        self.assertEqual(
            stored.journal_link,
            {"kind": "daily", "slug": "2026-07-15", "title": "Daily Note"},
        )

    @patch("apps.router.proactive_context._dispatch_ios_push")
    def test_proactive_outbound_is_clamped_before_store(self, mock_dispatch):
        row = record_proactive_outbound(
            tenant=self.tenant,
            channel="app",
            channel_user_id=str(self.user.id),
            message_text="p" * (DEFAULT_REPLY_TEXT_MAX_CHARS + 500),
        )

        self.assertIsNotNone(row)
        self.assertEqual(len(row.message_text), DEFAULT_REPLY_TEXT_MAX_CHARS)
        self.assertTrue(row.message_text.endswith(REPLY_TEXT_TRUNCATION_SUFFIX))
        self.assertEqual(mock_dispatch.call_args.args[2], row.message_text)
