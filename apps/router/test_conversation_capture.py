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
    _LOCATION_COORDINATE_PAIR_RE,
    build_conversation_digest,
    clean_reply_for_capture,
    join_user_texts,
    record_conversation_turn,
)
from apps.router.models import AppChatMessage, ChatThread, ConversationTurn
from apps.router.reply_text import REPLY_TEXT_TRUNCATION_SUFFIX
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
        self.assertTrue(row.reply_text.endswith(REPLY_TEXT_TRUNCATION_SUFFIX))
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

    def test_digest_normalizes_multiline_ios_pin_and_preserves_trailing_words(self):
        thread = ChatThread.objects.create(tenant=self.tenant, user=self.user, is_main=True)
        raw = (
            "📍 Current location: 33.59211, 130.39594 (±20m)\n"
            "https://maps.apple.com/?ll=33.59211,130.39594&q=My+Location\n"
            "going to the beach today?"
        )
        message = AppChatMessage.objects.create(
            tenant=self.tenant,
            user=self.user,
            thread=thread,
            client_msg_id="ios-location-pin",
            user_text=raw,
            reply_text="got it",
            status=AppChatMessage.Status.READY,
        )
        digest = build_conversation_digest(self.tenant)
        self.assertIsNone(_LOCATION_COORDINATE_PAIR_RE.search(digest))
        self.assertIn("https://maps.apple.com/?ll=…&q=My+Location", digest)
        self.assertIn("going to the beach today?", digest)
        message.refresh_from_db()
        self.assertEqual(message.user_text, raw)

    def test_digest_normalizes_single_line_mixed_pin_and_preserves_user_words(self):
        raw = "📍 Current location: 33.59, 130.40 (±20m) meeting friends after lunch"
        ConversationTurn.objects.create(
            tenant=self.tenant,
            channel="line",
            channel_user_id="1",
            local_date=tenant_today(self.tenant),
            user_text=raw,
            reply_text="",
        )
        digest = build_conversation_digest(self.tenant)
        self.assertIsNone(_LOCATION_COORDINATE_PAIR_RE.search(digest))
        self.assertIn("📍 Current location: … (±20m) meeting friends after lunch", digest)

    def test_digest_normalizes_telegram_coordinate_pin_without_dropping_url(self):
        raw = "📍 User shared their location: 33.59,130.40 https://maps.example/secret"
        turn = ConversationTurn.objects.create(
            tenant=self.tenant,
            channel="telegram",
            channel_user_id="1",
            local_date=tenant_today(self.tenant),
            user_text=raw,
            reply_text="",
        )
        digest = build_conversation_digest(self.tenant)
        self.assertIsNone(_LOCATION_COORDINATE_PAIR_RE.search(digest))
        self.assertIn("📍 User shared their location: … https://maps.example/secret", digest)
        turn.refresh_from_db()
        self.assertEqual(turn.user_text, raw)

    def test_digest_leaves_non_pin_numeric_line_untouched(self):
        raw = "the odds were 33.5, 130.4 against us"
        ConversationTurn.objects.create(
            tenant=self.tenant,
            channel="line",
            channel_user_id="1",
            local_date=tenant_today(self.tenant),
            user_text=raw,
            reply_text="",
        )
        digest = build_conversation_digest(self.tenant)
        self.assertIn(raw, digest)

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

    # ── D2: recent proactive-sends dedup block ─────────────────────────────
    def test_digest_renders_recent_proactive_sends(self):
        """Proactive-only digest (no captured turns) still renders the dedup
        block — crons fire precisely when the user was quiet, and a sibling cron
        must see what was already sent."""
        from apps.router.models import ProactiveOutbound

        ProactiveOutbound.objects.create(
            tenant=self.tenant,
            channel="telegram",
            channel_user_id="1",
            message_text="Did the budget meeting go ok?",
            job_name="morning_briefing",
        )
        digest = build_conversation_digest(self.tenant)
        self.assertIn("Already sent to the user proactively", digest)
        self.assertIn("Did the budget meeting go ok?", digest)
        self.assertIn("morning_briefing", digest)

    def test_digest_proactive_block_caps_at_limit(self):
        from apps.router.models import ProactiveOutbound
        from apps.router.proactive_context import DEFAULT_LIMIT

        for i in range(DEFAULT_LIMIT + 2):
            ProactiveOutbound.objects.create(
                tenant=self.tenant,
                channel="telegram",
                channel_user_id="1",
                message_text=f"proactive number {i}",
                job_name="heartbeat_checkin",
            )
        digest = build_conversation_digest(self.tenant)
        rendered = [ln for ln in digest.splitlines() if "heartbeat_checkin" in ln]
        self.assertEqual(len(rendered), DEFAULT_LIMIT)

    def test_digest_omits_proactive_block_when_none(self):
        record_conversation_turn(
            tenant=self.tenant,
            channel="telegram",
            channel_user_id="1",
            user_text="just chatting",
            reply_text="hi",
        )
        digest = build_conversation_digest(self.tenant)
        self.assertNotIn("Already sent to the user proactively", digest)

    def test_proactive_message_text_stays_placeholder_in_digest(self):
        """message_text is placeholder-space at rest → rendered unchanged, never
        rehydrated into the model-facing digest."""
        from apps.router.models import ProactiveOutbound

        self.tenant.pii_entity_map = {"[PERSON_1]": "Alice Smith"}
        self.tenant.save(update_fields=["pii_entity_map"])
        ProactiveOutbound.objects.create(
            tenant=self.tenant,
            channel="telegram",
            channel_user_id="1",
            message_text="Have you heard back from [PERSON_1]?",
            job_name="evening_checkin",
        )
        digest = build_conversation_digest(self.tenant)
        self.assertIn("[PERSON_1]", digest)
        self.assertNotIn("Alice Smith", digest)

    # ── D3: iOS user_text scrub (verbatim → placeholder, reuse-only) ────────
    def test_ios_user_text_with_binding_scrubbed_to_placeholder(self):
        """A real name in verbatim iOS user_text that HAS a tenant-map binding
        renders as its placeholder in the model-facing digest — closing the
        pre-existing leak of the user's own typed third-party names."""
        self.tenant.pii_entity_map = {"[PERSON_1]": "Alice Smith"}
        self.tenant.save(update_fields=["pii_entity_map"])
        thread = ChatThread.objects.create(tenant=self.tenant, user=self.user, is_main=True)
        AppChatMessage.objects.create(
            tenant=self.tenant,
            user=self.user,
            thread=thread,
            client_msg_id="scrub1",
            user_text="Alice Smith emailed me about the meeting",
            reply_text="got it",
            status=AppChatMessage.Status.READY,
        )
        digest = build_conversation_digest(self.tenant)
        self.assertIn("[PERSON_1]", digest)
        self.assertNotIn("Alice Smith", digest)

    def test_ios_user_text_without_binding_mints_nothing(self):
        """A name with NO binding is not coined into a junk placeholder on this
        render path — reuse-only, the tenant map is left unchanged."""
        self.tenant.pii_entity_map = {"[PERSON_1]": "Alice Smith"}
        self.tenant.save(update_fields=["pii_entity_map"])
        before = dict(self.tenant.pii_entity_map)
        thread = ChatThread.objects.create(tenant=self.tenant, user=self.user, is_main=True)
        AppChatMessage.objects.create(
            tenant=self.tenant,
            user=self.user,
            thread=thread,
            client_msg_id="scrub2",
            user_text="Bob Jones has no binding in the map",
            reply_text="ok",
            status=AppChatMessage.Status.READY,
        )
        digest = build_conversation_digest(self.tenant)
        # Nothing minted: the map is byte-for-byte unchanged and no fresh
        # [PERSON_2] placeholder was coined from the render path.
        self.tenant.refresh_from_db(fields=["pii_entity_map"])
        self.assertEqual(self.tenant.pii_entity_map, before)
        self.assertNotIn("[PERSON_2]", digest)

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
