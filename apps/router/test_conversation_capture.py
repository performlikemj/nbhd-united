"""Tests for deterministic conversation capture + the USER.md digest.

Covers:
* ``record_conversation_turn`` — row write, tenant-local date stamp, clipping,
  empty no-op, fail-open on bad input.
* ``clean_reply_for_capture`` — marker/MEDIA stripping.
* ``join_user_texts`` — coalesced-batch join.
* ``build_conversation_digest`` — empty case, today rendering, iOS
  (AppChatMessage) merge, previous-days rollup, tenant-local "today".
* envelope section wiring — ``render_conversation_digest`` returns the digest.
* drain wiring — ``_capture_conversation_turn`` records from a batch.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from django.test import TestCase
from django.utils import timezone

from apps.common.tenant_tz import tenant_today
from apps.router.conversation_capture import (
    build_conversation_digest,
    clean_reply_for_capture,
    join_user_texts,
    record_conversation_turn,
)
from apps.router.models import AppChatMessage, ChatThread, ConversationTurn
from apps.tenants.models import Tenant, User


class ConversationCaptureTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="convo_test", password="pw")
        self.user.timezone = "Asia/Tokyo"
        self.user.telegram_chat_id = 12345
        self.user.save()
        self.tenant = Tenant.objects.create(user=self.user, status=Tenant.Status.ACTIVE)

    # ── record_conversation_turn ───────────────────────────────────────────
    def test_record_writes_row_with_local_date(self):
        row = record_conversation_turn(
            tenant=self.tenant,
            channel="telegram",
            channel_user_id="12345",
            user_text="I have a job interview tomorrow",
            reply_text="Let's prep some STAR answers.",
        )
        self.assertIsNotNone(row)
        self.assertEqual(row.channel, "telegram")
        self.assertEqual(row.user_text, "I have a job interview tomorrow")
        self.assertEqual(row.reply_text, "Let's prep some STAR answers.")
        self.assertEqual(row.local_date, tenant_today(self.tenant))

    def test_record_empty_is_noop(self):
        self.assertIsNone(
            record_conversation_turn(
                tenant=self.tenant, channel="telegram", channel_user_id="1", user_text="  ", reply_text=""
            )
        )
        self.assertEqual(ConversationTurn.objects.count(), 0)

    def test_record_reply_only_is_captured(self):
        row = record_conversation_turn(
            tenant=self.tenant, channel="telegram", channel_user_id="1", user_text="", reply_text="proactive nudge"
        )
        self.assertIsNotNone(row)
        self.assertEqual(row.user_text, "")
        self.assertEqual(row.reply_text, "proactive nudge")

    def test_record_fail_open_on_bad_tenant(self):
        # A tenant-shaped object that explodes inside the try must yield None,
        # never raise into the drain.
        self.assertIsNone(
            record_conversation_turn(tenant=object(), channel="telegram", channel_user_id="1", user_text="hi there")
        )

    def test_text_and_id_clipped(self):
        row = record_conversation_turn(
            tenant=self.tenant,
            channel="line",
            channel_user_id="U" * 300,
            user_text="x" * 5000,
            reply_text="y" * 5000,
        )
        self.assertLessEqual(len(row.user_text), 2000)
        self.assertLessEqual(len(row.reply_text), 800)
        self.assertLessEqual(len(row.channel_user_id), 128)

    # ── helpers ────────────────────────────────────────────────────────────
    def test_clean_reply_strips_markers_and_media(self):
        out = clean_reply_for_capture(
            self.tenant, "Here is a chart [[chart:abc]] and a button [[button:Yes]]\nMEDIA:/ws/x.png\nDone"
        )
        self.assertNotIn("[[chart", out)
        self.assertNotIn("[[button", out)
        self.assertNotIn("MEDIA:", out)
        self.assertIn("Done", out)

    def test_clean_reply_empty(self):
        self.assertEqual(clean_reply_for_capture(self.tenant, None), "")
        self.assertEqual(clean_reply_for_capture(self.tenant, "   "), "")

    def test_join_user_texts_skips_blanks(self):
        batch = [
            SimpleNamespace(user_text="first message"),
            SimpleNamespace(user_text="   "),
            SimpleNamespace(user_text="second message"),
        ]
        self.assertEqual(join_user_texts(batch), "first message\nsecond message")

    # ── digest ─────────────────────────────────────────────────────────────
    def test_digest_empty_when_no_turns(self):
        self.assertEqual(build_conversation_digest(self.tenant), "")

    def test_digest_renders_today_with_guidance(self):
        record_conversation_turn(
            tenant=self.tenant,
            channel="telegram",
            channel_user_id="1",
            user_text="job interview prep please",
            reply_text="sure, let's do it",
        )
        digest = build_conversation_digest(self.tenant)
        self.assertIn("Today", digest)
        self.assertIn("job interview prep", digest)
        self.assertIn("NOT quiet", digest)  # the anti-"quiet day" guidance

    def test_digest_includes_ios_app_chat(self):
        thread = ChatThread.objects.create(tenant=self.tenant, user=self.user, is_main=True)
        AppChatMessage.objects.create(
            tenant=self.tenant,
            user=self.user,
            thread=thread,
            client_msg_id="c1",
            user_text="ios question about my taxes",
            reply_text="here's the answer",
            status=AppChatMessage.Status.READY,
        )
        digest = build_conversation_digest(self.tenant)
        self.assertIn("ios question about my taxes", digest)

    def test_digest_previous_days_rollup(self):
        yesterday = tenant_today(self.tenant) - timedelta(days=1)
        row = ConversationTurn.objects.create(
            tenant=self.tenant,
            channel="telegram",
            channel_user_id="1",
            local_date=yesterday,
            user_text="yesterday we discussed the customs paperwork",
            reply_text="ok",
        )
        # Backdate created_at so it falls in the window but not "today".
        ConversationTurn.objects.filter(id=row.id).update(created_at=timezone.now() - timedelta(days=1))
        digest = build_conversation_digest(self.tenant)
        self.assertIn("Earlier this week", digest)
        self.assertIn(yesterday.isoformat(), digest)

    def test_digest_excludes_beyond_window(self):
        old = tenant_today(self.tenant) - timedelta(days=10)
        row = ConversationTurn.objects.create(
            tenant=self.tenant,
            channel="telegram",
            channel_user_id="1",
            local_date=old,
            user_text="ancient history conversation",
            reply_text="ok",
        )
        ConversationTurn.objects.filter(id=row.id).update(created_at=timezone.now() - timedelta(days=10))
        self.assertEqual(build_conversation_digest(self.tenant), "")

    # ── envelope section wiring ────────────────────────────────────────────
    def test_envelope_section_renders(self):
        record_conversation_turn(
            tenant=self.tenant,
            channel="telegram",
            channel_user_id="1",
            user_text="hello there friend",
            reply_text="hi",
        )
        from apps.journal.envelope import render_conversation_digest

        out = render_conversation_digest(self.tenant)
        self.assertIn("hello there friend", out)

    def test_envelope_section_empty_when_quiet(self):
        from apps.journal.envelope import render_conversation_digest

        self.assertEqual(render_conversation_digest(self.tenant), "")

    # ── drain wiring ───────────────────────────────────────────────────────
    def test_capture_from_drain_batch(self):
        from apps.router.pending_queue import _capture_conversation_turn

        batch = [SimpleNamespace(user_text="first half"), SimpleNamespace(user_text="second half")]
        _capture_conversation_turn(self.tenant, "telegram", "12345", batch, "the assistant reply")
        rows = list(ConversationTurn.objects.filter(tenant=self.tenant))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].user_text, "first half\nsecond half")
        self.assertEqual(rows[0].reply_text, "the assistant reply")
        self.assertEqual(rows[0].channel, "telegram")
