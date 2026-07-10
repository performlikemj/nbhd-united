"""Pseudonymize-at-rest: assistant-authored copies are stored in PII-placeholder
space and rehydrated ONLY at owner-facing reads.

The governing invariant (encryption-at-rest directive §5/§7, Phase 0 PR-4):

  * every seam serving the OWNER (iOS ``?since=`` feed, the poll serializer,
    LINE/Telegram sends, the iOS push body) rehydrates placeholders → real names;
  * every seam feeding the MODEL / container (the USER.md conversation digest,
    the proactive ``[earlier-from-you]`` block, the LINE quote-reply context)
    stays placeholder-space.

Rehydration is a no-op on text with no placeholders, so LEGACY rows (stored with
real names before this change) read back unchanged — the dual-read is
transparent in both directions.

``AppChatMessage.reply_text`` is exercised end-to-end in
``test_chat_redaction_metadata``; this module covers the other three converted
columns and the model-facing seams that must NOT rehydrate.
"""

from __future__ import annotations

import secrets

from django.test import TestCase, override_settings

from apps.common.tenant_tz import tenant_today
from apps.router.models import (
    AppChatMessage,
    ChatThread,
    ConversationTurn,
    LineOutboundMessage,
    ProactiveOutbound,
)
from apps.tenants.models import Tenant, User

_NAME = "Sautai"
_PH = "[PERSON_5]"


def _mk_tenant() -> tuple[Tenant, User]:
    user = User.objects.create_user(
        username=f"u_{secrets.token_hex(4)}",
        email=f"{secrets.token_hex(4)}@example.com",
    )
    tenant = Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE,
        container_fqdn="oc-x.example.com",
    )
    tenant.pii_entity_map = {_PH: _NAME}
    tenant.save(update_fields=["pii_entity_map"])
    return tenant, user


@override_settings(NBHD_DISABLE_BACKGROUND_THREADS=True)
class ConversationTurnPseudonymizeTest(TestCase):
    def setUp(self):
        self.tenant, self.user = _mk_tenant()

    def _turn(self, *, user_text: str, reply_text: str) -> ConversationTurn:
        return ConversationTurn.objects.create(
            tenant=self.tenant,
            channel="telegram",
            channel_user_id="1",
            local_date=tenant_today(self.tenant),
            user_text=user_text,
            reply_text=reply_text,
        )

    def test_clean_reply_for_capture_keeps_placeholders(self):
        from apps.router.conversation_capture import clean_reply_for_capture

        out = clean_reply_for_capture(self.tenant, "Tell [PERSON_5] hi [[chart:x]]\nMEDIA:/w/x.png")
        self.assertIn("[PERSON_5]", out)  # placeholder-space at rest
        self.assertNotIn("Sautai", out)  # NOT rehydrated at store time
        self.assertNotIn("[[chart", out)  # markers still stripped
        self.assertNotIn("MEDIA:", out)

    def test_digest_stays_placeholder_space_model_facing(self):
        # THE LIVE-LEAK FIX: the digest renders into USER.md, which the container
        # loads on every turn — a MODEL-facing seam. A new placeholder-space reply
        # must NOT be rehydrated into real names here (it used to be).
        from apps.router.conversation_capture import build_conversation_digest

        self._turn(user_text="ping [PERSON_5]", reply_text="Told [PERSON_5] you said hi.")
        digest = build_conversation_digest(self.tenant)
        self.assertIn("[PERSON_5]", digest)
        self.assertNotIn(_NAME, digest)

    def test_since_feed_rehydrates_reply_owner_facing(self):
        from apps.router.chat_history import build_since_page

        self._turn(user_text="ping [PERSON_5]", reply_text="Told [PERSON_5] you said hi.")
        msgs, _ = build_since_page(self.tenant, "t-main", cursor=None, limit=100)
        asst = [m for m in msgs if m["role"] == "assistant"]
        self.assertEqual(len(asst), 1)
        self.assertEqual(asst[0]["text"], "Told Sautai you said hi.")

    def test_legacy_realname_reply_reads_back_unchanged(self):
        # A pre-change row stored with the real name has no placeholder, so the
        # feed's rehydration is a no-op and the digest keeps it verbatim.
        from apps.router.chat_history import build_since_page
        from apps.router.conversation_capture import build_conversation_digest

        self._turn(user_text="ping", reply_text="Told Sautai you said hi.")
        msgs, _ = build_since_page(self.tenant, "t-main", cursor=None, limit=100)
        asst = [m for m in msgs if m["role"] == "assistant"]
        self.assertEqual(asst[0]["text"], "Told Sautai you said hi.")
        self.assertIn(_NAME, build_conversation_digest(self.tenant))


@override_settings(NBHD_DISABLE_BACKGROUND_THREADS=True)
class ProactiveOutboundPseudonymizeTest(TestCase):
    def setUp(self):
        self.tenant, self.user = _mk_tenant()

    def test_record_stores_placeholder_message_and_parsed_items(self):
        from apps.router.proactive_context import record_proactive_outbound

        row = record_proactive_outbound(
            tenant=self.tenant,
            channel="app",
            channel_user_id="u1",
            message_text="For [PERSON_5]:\n- call [PERSON_5]\n- email [PERSON_5]",
        )
        row.refresh_from_db()
        self.assertIn("[PERSON_5]", row.message_text)
        self.assertNotIn(_NAME, row.message_text)
        # parse_markdown_items ran on the placeholder-space body.
        self.assertEqual(row.parsed_items, ["call [PERSON_5]", "email [PERSON_5]"])

    def test_since_feed_rehydrates_message_owner_facing(self):
        from apps.router.chat_history import build_since_page
        from apps.router.proactive_context import record_proactive_outbound

        record_proactive_outbound(
            tenant=self.tenant, channel="app", channel_user_id="u1", message_text="Ping [PERSON_5] tonight."
        )
        msgs, _ = build_since_page(self.tenant, "t-main", cursor=None, limit=100)
        cron = [m for m in msgs if m["source"] == "cron"]
        self.assertEqual(len(cron), 1)
        self.assertEqual(cron[0]["text"], "Ping Sautai tonight.")

    def test_surface_context_stays_placeholder_model_facing(self):
        # The [earlier-from-you] block is prepended to the INBOUND agent turn —
        # placeholder-space is now correct (it used to leak real names to model).
        from apps.router.proactive_context import record_proactive_outbound, surface_proactive_context

        record_proactive_outbound(
            tenant=self.tenant, channel="telegram", channel_user_id="42", message_text="Did you reach [PERSON_5]?"
        )
        block = surface_proactive_context(tenant=self.tenant)
        self.assertIn("[PERSON_5]", block)
        self.assertNotIn(_NAME, block)

    def test_legacy_realname_message_reads_back_unchanged(self):
        from apps.router.chat_history import build_since_page

        ProactiveOutbound.objects.create(
            tenant=self.tenant, channel="app", channel_user_id="u1", message_text="Ping Sautai tonight."
        )
        msgs, _ = build_since_page(self.tenant, "t-main", cursor=None, limit=100)
        cron = [m for m in msgs if m["source"] == "cron"]
        self.assertEqual(cron[0]["text"], "Ping Sautai tonight.")


@override_settings(NBHD_DISABLE_BACKGROUND_THREADS=True)
class LineOutboundPseudonymizeTest(TestCase):
    def setUp(self):
        self.tenant, self.user = _mk_tenant()

    def test_excerpt_override_stores_placeholder_for_prose(self):
        from apps.router.line_webhook import _record_line_outbound

        _record_line_outbound(
            self.tenant,
            "U_abc",
            [{"id": "m1"}],
            [{"type": "text", "text": "Tell Sautai hi"}],  # rehydrated body actually sent
            excerpt_override="Tell [PERSON_5] hi",  # placeholder-space copy
        )
        row = LineOutboundMessage.objects.get(line_message_id="m1")
        self.assertEqual(row.text_excerpt, "Tell [PERSON_5] hi")

    def test_media_message_keeps_its_tag_excerpt(self):
        # An image/sticker excerpt is a non-PII "[image]"-style tag — the override
        # (prose) must not overwrite it.
        from apps.router.line_webhook import _record_line_outbound

        _record_line_outbound(
            self.tenant,
            "U_abc",
            [{"id": "img1"}],
            [{"type": "image"}],
            excerpt_override="Tell [PERSON_5] hi",
        )
        row = LineOutboundMessage.objects.get(line_message_id="img1")
        self.assertEqual(row.text_excerpt, "[image]")

    def test_quote_reply_context_stays_placeholder_model_facing(self):
        # _extract_line_reply_context inlines the excerpt into the INBOUND agent
        # turn (model-facing), so placeholder-space is correct there.
        from apps.router.line_webhook import _extract_line_reply_context

        LineOutboundMessage.objects.create(
            tenant=self.tenant,
            line_user_id="U_abc",
            line_message_id="q1",
            text_excerpt="What is [PERSON_5] into these days?",
        )
        ctx = _extract_line_reply_context(self.tenant, {"quotedMessageId": "q1"})
        self.assertIn("[PERSON_5]", ctx)
        self.assertNotIn(_NAME, ctx)

    def test_no_override_falls_back_to_sent_message_excerpt(self):
        # Callers that pass no override (nothing to pseudonymize) behave exactly
        # as before — the excerpt is pulled from the sent message dict.
        from apps.router.line_webhook import _record_line_outbound

        _record_line_outbound(
            self.tenant,
            "U_abc",
            [{"id": "n1"}],
            [{"type": "text", "text": "plain body"}],
        )
        row = LineOutboundMessage.objects.get(line_message_id="n1")
        self.assertEqual(row.text_excerpt, "plain body")


@override_settings(NBHD_DISABLE_BACKGROUND_THREADS=True)
class AppChatMessageLegacyPassthroughTest(TestCase):
    def setUp(self):
        self.tenant, self.user = _mk_tenant()

    def test_legacy_realname_reply_reads_back_unchanged(self):
        from apps.router.chat_history import build_since_page

        thread = ChatThread.objects.create(tenant=self.tenant, user=self.user, is_main=True)
        AppChatMessage.objects.create(
            tenant=self.tenant,
            user=self.user,
            thread=thread,
            client_msg_id="legacy1",
            user_text="hi",
            reply_text="Told Sautai you said hi.",  # legacy real-name storage
            status=AppChatMessage.Status.READY,
        )
        msgs, _ = build_since_page(self.tenant, str(thread.id), cursor=None, limit=100)
        asst = [m for m in msgs if m["role"] == "assistant"]
        self.assertEqual(len(asst), 1)
        self.assertEqual(asst[0]["text"], "Told Sautai you said hi.")
