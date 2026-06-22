"""Audit cluster C01 regression tests.

Covers inbound user-message PII redaction wiring for the Telegram poller
(FA-0913 / FA-0914): the production Telegram path runs through
``TelegramPoller._forward_to_container`` -> ``enqueue_message_for_tenant``.
Before this fix the user's raw text was forwarded to the third-party LLM
provider unredacted. Now it is run through ``redact_user_message`` BEFORE the
agent-only markers are prepended.

Also covers FA-1050: ``get_forwarding_timeout`` now classifies BYO slow
models (Anthropic Sonnet/Opus) alongside reasoning models, matching the
drain's ``_resolve_chat_timeout``.

These tests use the *map-reuse* (Step-1, regex) redaction path — a
pre-populated ``pii_entity_map`` — so they exercise the wiring deterministically
without depending on the DeBERTa detection model in CI.
"""

from __future__ import annotations

import secrets
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from apps.tenants.models import Tenant, User


def _make_tenant(*, preferred_model: str = "") -> Tenant:
    user = User.objects.create_user(
        username=f"c01_{secrets.token_hex(4)}",
        email=f"{secrets.token_hex(4)}@example.com",
        telegram_chat_id=int(secrets.token_hex(3), 16),
        preferred_channel="telegram",
    )
    return Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE,
        container_fqdn="oc-c01.example.com",
        preferred_model=preferred_model,
        pii_entity_map={"[EMAIL_ADDRESS_1]": "alice.johnson@acme.com"},
    )


class PollerInboundRedactionTest(TestCase):
    def setUp(self):
        from apps.router.poller import TelegramPoller

        self.poller = TelegramPoller()
        self.poller._http = MagicMock()
        self.poller._http.post.return_value = MagicMock(is_success=True)

    @patch("apps.router.pending_queue.enqueue_message_for_tenant")
    def test_forward_redacts_known_entity_in_message_and_excerpt(self, mock_enqueue):
        tenant = _make_tenant()
        self.poller._forward_to_container(
            123,
            tenant,
            "email alice.johnson@acme.com about it",
            raw_user_text="email alice.johnson@acme.com about it",
        )

        self.assertEqual(mock_enqueue.call_count, 1)
        kwargs = mock_enqueue.call_args.kwargs

        body = kwargs["payload"]["message_text"]
        self.assertNotIn("alice.johnson@acme.com", body)
        self.assertIn("[EMAIL_ADDRESS_1]", body)
        # Agent-only markers are still prepended (not corrupted by redaction).
        self.assertIn("[Now: ", body)
        self.assertIn("[chat:", body)

        # The persisted apology excerpt is redacted too — no raw PII at rest.
        self.assertNotIn("alice.johnson@acme.com", kwargs["user_text_excerpt"])
        self.assertIn("[EMAIL_ADDRESS_1]", kwargs["user_text_excerpt"])

    @patch("apps.router.pending_queue.enqueue_message_for_tenant")
    def test_forward_leaves_clean_text_untouched(self, mock_enqueue):
        tenant = _make_tenant()
        self.poller._forward_to_container(123, tenant, "what time is it", raw_user_text="what time is it")

        body = mock_enqueue.call_args.kwargs["payload"]["message_text"]
        self.assertTrue(body.endswith("what time is it"))


class ForwardingTimeoutByoSlowModelTest(TestCase):
    """FA-1050: get_forwarding_timeout must treat BYO slow models as slow,
    matching pending_queue._resolve_chat_timeout."""

    def test_byo_slow_model_gets_reasoning_timeout(self):
        from apps.billing.constants import (
            BYO_SLOW_MODELS,
            REASONING_MODEL_TIMEOUT,
        )
        from apps.router.services import get_forwarding_timeout

        slow_model = next(iter(BYO_SLOW_MODELS))
        tenant = _make_tenant(preferred_model=slow_model)
        timeout, is_reasoning = get_forwarding_timeout(tenant)
        self.assertTrue(is_reasoning)
        self.assertEqual(timeout, REASONING_MODEL_TIMEOUT)

    def test_standard_model_gets_default_timeout(self):
        from apps.billing.constants import DEFAULT_CHAT_TIMEOUT
        from apps.router.services import get_forwarding_timeout

        tenant = _make_tenant(preferred_model="some/standard-model")
        timeout, is_reasoning = get_forwarding_timeout(tenant)
        self.assertFalse(is_reasoning)
        self.assertEqual(timeout, DEFAULT_CHAT_TIMEOUT)


@override_settings(TELEGRAM_BOT_TOKEN="test-bot-token")
class TelegramRelayButtonParsingTest(TestCase):
    """FA-1036: the queue-drain Telegram relay must render
    ``[[button:Label|data]]`` markers as an inline keyboard (callback_data
    ``agent:<data>``, which the poller already handles) instead of leaking
    the literal marker text."""

    def setUp(self):
        # Empty map so PII rehydration is a no-op for these tests.
        user = User.objects.create_user(
            username=f"c01btn_{secrets.token_hex(4)}",
            email=f"{secrets.token_hex(4)}@example.com",
            telegram_chat_id=int(secrets.token_hex(3), 16),
        )
        self.tenant = Tenant.objects.create(
            user=user,
            status=Tenant.Status.ACTIVE,
            container_fqdn="oc-c01.example.com",
        )

    @patch("apps.router.pending_queue.httpx.post")
    def test_button_marker_becomes_inline_keyboard(self, mock_post):
        from apps.router.pending_queue import relay_ai_response_to_telegram

        ok = MagicMock()
        ok.is_success = True
        ok.status_code = 200
        mock_post.return_value = ok

        relay_ai_response_to_telegram(
            self.tenant,
            555,
            "Pick one:\n[[button:Yes|confirm_yes]]\n[[button:No|confirm_no]]",
        )

        send_calls = [c for c in mock_post.call_args_list if "sendMessage" in c.args[0]]
        self.assertTrue(send_calls)
        # The button markers must be attached to a message as reply_markup
        # and never leak as literal text.
        markup_calls = [c for c in send_calls if "reply_markup" in c.kwargs["json"]]
        self.assertEqual(len(markup_calls), 1)
        keyboard = markup_calls[0].kwargs["json"]["reply_markup"]["inline_keyboard"]
        flat = [btn for row in keyboard for btn in row]
        self.assertEqual({b["callback_data"] for b in flat}, {"agent:confirm_yes", "agent:confirm_no"})
        for c in send_calls:
            self.assertNotIn("[[button:", c.kwargs["json"]["text"])


class PollerTaskActionCallbackDispatchTest(TestCase):
    """FA-1021: the production Telegram poller must route
    ``task_action:undo:<id>`` callbacks (Remove/Undo button on the morning
    reconciliation summary) to ``handle_task_action_callback`` and execute
    its answerCallbackQuery response — previously they fell through
    unhandled and the button spun forever."""

    def setUp(self):
        from apps.router.poller import TelegramPoller

        self.poller = TelegramPoller()
        self.poller._http = MagicMock()
        self.poller._http.post.return_value = MagicMock(is_success=True)
        self.poller.bot_token = "test-bot-token"

        user = User.objects.create_user(
            username=f"c01ta_{secrets.token_hex(4)}",
            email=f"{secrets.token_hex(4)}@example.com",
            telegram_chat_id=999000,
        )
        self.tenant = Tenant.objects.create(
            user=user,
            status=Tenant.Status.ACTIVE,
            container_fqdn="oc-c01.example.com",
        )

    @patch("apps.router.task_action_callbacks.handle_task_action_callback")
    def test_task_action_callback_is_dispatched_and_answered(self, mock_handle):
        from django.http import JsonResponse

        mock_handle.return_value = JsonResponse(
            {"method": "answerCallbackQuery", "callback_query_id": "cb1", "text": "Undone!"}
        )

        update = {
            "callback_query": {
                "id": "cb1",
                "data": "task_action:undo:abc123",
                "message": {"message_id": 7, "chat": {"id": 999000}},
            }
        }
        self.poller._handle_update(update)

        # The poller routed the task_action callback to the real handler …
        self.assertEqual(mock_handle.call_count, 1)
        self.assertEqual(mock_handle.call_args.args[1], self.tenant)
        # … and executed its answerCallbackQuery response against the Bot API.
        ack_calls = [c for c in self.poller._http.post.call_args_list if "answerCallbackQuery" in c.args[0]]
        self.assertEqual(len(ack_calls), 1)
