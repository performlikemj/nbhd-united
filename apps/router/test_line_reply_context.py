"""Tests for LINE quote-reply context plumbing.

Covers:
- ``_record_line_outbound`` persists ``sentMessages[].id`` rows correctly,
  pulls human-readable excerpts from text / flex / sticker payloads, and is
  no-op safe when LINE returned no IDs.
- ``_extract_line_reply_context`` returns the canonical
  ``[Replying to: "..."]`` prefix when a row matches, falls back to a
  generic phrase when the row is missing, and returns empty when the
  inbound message isn't a quote-reply.
- End-to-end: an inbound text event with ``quotedMessageId`` prepends the
  prefix before forwarding to the container.
"""

from __future__ import annotations

import secrets
from unittest.mock import patch

from django.test import TestCase

from apps.router.line_webhook import (
    _extract_line_reply_context,
    _message_text_excerpt,
    _record_line_outbound,
)
from apps.router.models import LineOutboundMessage
from apps.tenants.models import Tenant, User


def _make_user() -> User:
    return User.objects.create_user(
        username=f"u_{secrets.token_hex(4)}",
        email=f"{secrets.token_hex(4)}@example.com",
    )


def _make_tenant() -> Tenant:
    user = _make_user()
    return Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE,
        container_fqdn="container.example.com",
    )


class MessageExcerptTests(TestCase):
    def test_text_message_returns_text(self):
        self.assertEqual(
            _message_text_excerpt({"type": "text", "text": "  hello world  "}),
            "hello world",
        )

    def test_flex_message_returns_alt_text(self):
        self.assertEqual(
            _message_text_excerpt({"type": "flex", "altText": "summary line"}),
            "summary line",
        )

    def test_sticker_returns_placeholder(self):
        self.assertEqual(_message_text_excerpt({"type": "sticker"}), "[sticker]")

    def test_image_returns_placeholder(self):
        self.assertEqual(_message_text_excerpt({"type": "image"}), "[image]")

    def test_unknown_type_falls_back_to_alt_text(self):
        self.assertEqual(
            _message_text_excerpt({"type": "weird", "altText": "fb"}),
            "fb",
        )

    def test_non_dict_returns_empty(self):
        self.assertEqual(_message_text_excerpt("not a dict"), "")


class RecordLineOutboundTests(TestCase):
    def test_persists_ids_with_excerpts_index_aligned(self):
        tenant = _make_tenant()
        sent = [{"id": "m1", "quoteToken": "qt1"}, {"id": "m2"}]
        messages = [
            {"type": "text", "text": "first"},
            {"type": "flex", "altText": "second-alt"},
        ]

        _record_line_outbound(tenant, "U_abc", sent, messages)

        rows = list(LineOutboundMessage.objects.filter(tenant=tenant).order_by("line_message_id"))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].line_message_id, "m1")
        self.assertEqual(rows[0].text_excerpt, "first")
        self.assertEqual(rows[0].line_user_id, "U_abc")
        self.assertEqual(rows[1].line_message_id, "m2")
        self.assertEqual(rows[1].text_excerpt, "second-alt")

    def test_skips_entries_missing_id(self):
        tenant = _make_tenant()
        sent = [{"id": "m1"}, {"quoteToken": "no-id-here"}, {"id": "m3"}]
        messages = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}, {"type": "text", "text": "c"}]

        _record_line_outbound(tenant, "U_abc", sent, messages)

        ids = sorted(LineOutboundMessage.objects.filter(tenant=tenant).values_list("line_message_id", flat=True))
        self.assertEqual(ids, ["m1", "m3"])

    def test_no_op_when_sent_messages_empty(self):
        tenant = _make_tenant()
        _record_line_outbound(tenant, "U_abc", [], [{"type": "text", "text": "x"}])
        self.assertEqual(LineOutboundMessage.objects.filter(tenant=tenant).count(), 0)

    def test_no_op_when_sent_messages_none(self):
        tenant = _make_tenant()
        _record_line_outbound(tenant, "U_abc", None, [])
        self.assertEqual(LineOutboundMessage.objects.filter(tenant=tenant).count(), 0)

    def test_truncates_long_excerpts(self):
        tenant = _make_tenant()
        long_text = "x" * 1200
        _record_line_outbound(tenant, "U_abc", [{"id": "m1"}], [{"type": "text", "text": long_text}])
        row = LineOutboundMessage.objects.get(tenant=tenant)
        self.assertEqual(len(row.text_excerpt), 500)

    def test_duplicate_ids_handled_gracefully(self):
        tenant = _make_tenant()
        _record_line_outbound(tenant, "U_abc", [{"id": "dup"}], [{"type": "text", "text": "first"}])
        # Second insert with same ID is silently dropped by ignore_conflicts.
        _record_line_outbound(tenant, "U_abc", [{"id": "dup"}], [{"type": "text", "text": "second"}])
        rows = list(LineOutboundMessage.objects.filter(tenant=tenant))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].text_excerpt, "first")


class ExtractLineReplyContextTests(TestCase):
    def test_empty_when_no_quoted_id(self):
        tenant = _make_tenant()
        self.assertEqual(_extract_line_reply_context(tenant, {"type": "text", "text": "hi"}), "")

    def test_empty_when_no_tenant(self):
        self.assertEqual(
            _extract_line_reply_context(None, {"type": "text", "quotedMessageId": "m1"}),
            "",
        )

    def test_returns_quoted_excerpt_when_row_exists(self):
        tenant = _make_tenant()
        LineOutboundMessage.objects.create(
            tenant=tenant,
            line_user_id="U_abc",
            line_message_id="m1",
            text_excerpt="What is Nana into these days?",
        )
        prefix = _extract_line_reply_context(
            tenant,
            {"type": "text", "quotedMessageId": "m1", "text": "she likes roblox"},
        )
        self.assertEqual(prefix, '[Replying to: "What is Nana into these days?"]\n\n')

    def test_truncates_long_quoted_excerpt(self):
        tenant = _make_tenant()
        LineOutboundMessage.objects.create(
            tenant=tenant,
            line_user_id="U_abc",
            line_message_id="m1",
            text_excerpt="x" * 400,
        )
        prefix = _extract_line_reply_context(
            tenant,
            {"quotedMessageId": "m1"},
        )
        # 200 chars + "…" + the wrapper
        self.assertIn("x" * 200 + "…", prefix)

    def test_returns_generic_phrase_when_row_missing(self):
        tenant = _make_tenant()
        prefix = _extract_line_reply_context(tenant, {"quotedMessageId": "unknown"})
        self.assertEqual(prefix, "[Replying to an earlier message of yours]\n\n")

    def test_tenant_scoped_lookup(self):
        tenant_a = _make_tenant()
        tenant_b = _make_tenant()
        LineOutboundMessage.objects.create(
            tenant=tenant_a,
            line_user_id="U_abc",
            line_message_id="shared",
            text_excerpt="tenant A content",
        )
        # tenant B asking for the same id falls back to generic
        prefix = _extract_line_reply_context(tenant_b, {"quotedMessageId": "shared"})
        self.assertEqual(prefix, "[Replying to an earlier message of yours]\n\n")


class HandleMessagePrependsReplyContextTests(TestCase):
    """End-to-end: inbound LINE text event with ``quotedMessageId`` must
    cause ``_forward_to_container`` to receive the prefixed text.
    """

    def test_prepends_quoted_excerpt_to_forwarded_text(self):
        from apps.router.line_webhook import LineWebhookView

        tenant = _make_tenant()
        tenant.user.line_user_id = "U_test"
        tenant.user.save(update_fields=["line_user_id"])

        LineOutboundMessage.objects.create(
            tenant=tenant,
            line_user_id="U_test",
            line_message_id="bot-msg-1",
            text_excerpt="What's Nana into these days?",
        )

        view = LineWebhookView()
        event = {
            "type": "message",
            "replyToken": "rt",
            "source": {"userId": "U_test"},
            "message": {
                "type": "text",
                "id": "user-msg-1",
                "text": "i guess writing in japanese. and she wants to do roblox",
                "quotedMessageId": "bot-msg-1",
            },
        }

        tenant.onboarding_complete = True
        tenant.onboarding_step = 99
        tenant.save(update_fields=["onboarding_complete", "onboarding_step"])

        with (
            patch("apps.router.wake_on_message.handle_hibernated_message", return_value=None),
            patch("apps.router.line_webhook.check_budget", return_value=None),
            patch("apps.router.onboarding.needs_reintroduction", return_value=False),
            patch("apps.router.line_webhook._show_loading"),
            patch.object(view, "_forward_to_container") as forward,
        ):
            view._handle_message(event)

        forward.assert_called_once()
        kwargs = forward.call_args.kwargs
        args = forward.call_args.args
        # Signature: (line_user_id, tenant, text, **kw)
        forwarded_text = args[2] if len(args) >= 3 else kwargs.get("text", "")
        self.assertIn('[Replying to: "What\'s Nana into these days?"]', forwarded_text)
        self.assertIn("i guess writing in japanese", forwarded_text)
        # raw_user_text must not include the prefix (used for the dropped-message apology)
        self.assertEqual(kwargs.get("raw_user_text"), "i guess writing in japanese. and she wants to do roblox")
