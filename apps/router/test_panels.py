"""Panels are bounded references; malformed candidates never break messages."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import get_args
from unittest.mock import patch
from uuid import uuid4

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.router.panels import (
    PANEL_KINDS,
    PanelKind,
    chat_panels_enabled,
    extract_panels,
    tool_schema_module,
    validate_panels,
)

PANELS = [
    {"kind": "sleep", "params": {"range": "last_night"}, "title": "Last night"},
    {"kind": "workout", "params": {"day": "2026-09-24"}},
    {"kind": "schedule", "params": {"range": "today"}},
    {"kind": "tasks", "params": {"filter": "due_today"}},
    {"kind": "log_table", "params": {"metric": "body_weight", "range": "this_month"}},
]


def block(panels=PANELS):
    return "```nbhd-panels\n" + json.dumps(panels) + "\n```"


class PanelSchemaTests(SimpleTestCase):
    def test_vocabulary_and_generated_tool_schema(self):
        self.assertEqual(get_args(PanelKind), PANEL_KINDS)
        path = Path(__file__).resolve().parents[2] / "runtime/openclaw/plugins/nbhd-journal-tools/panel-schema.js"
        self.assertEqual(path.read_text(), tool_schema_module())

    def test_valid_schema_table(self):
        for panel in [
            *PANELS,
            *({"kind": kind, "params": {}} for kind in PANEL_KINDS),
            {"kind": "timer", "params": {"duration_seconds": 1}},
            {"kind": "timer", "params": {"duration_seconds": 14400}},
            {"kind": "journal_table", "params": {"day": "2024-02-29"}, "title": "t" * 60},
        ]:
            with self.subTest(panel=panel):
                self.assertEqual(validate_panels([panel]), [panel])
        self.assertEqual(validate_panels([{"kind": "tasks"}]), [{"kind": "tasks", "params": {}}])

    def test_invalid_schema_table_drops_individually_without_content(self):
        bad = [
            {},
            None,
            "private-sentinel",
            [],
            {"kind": "unknown"},
            {"kind": "tasks", "snapshot": "private-sentinel"},
            {"kind": "sleep", "params": {"unknown": "private-sentinel"}},
            {"kind": "sleep", "params": {"range": "all_time"}},
            {"kind": "sleep", "params": {"day": "2026-02-29"}},
            {"kind": "sleep", "params": {"day": "20260924"}},
            {"kind": "sleep", "params": {"day": "2026-09-24T00:00:00"}},
            {"kind": "sleep", "params": {"day": 20260924}},
            {"kind": "sleep", "params": {"metric": "sleep"}},
            {"kind": "sleep", "params": {"filter": "open"}},
            {"kind": "sleep", "params": {"duration_seconds": 60}},
            {"kind": "log_table", "params": {"metric": "height"}},
            {"kind": "tasks", "params": {"filter": "done"}},
            *({"kind": "timer", "params": {"duration_seconds": v}} for v in (0, 14401, True, "60", 1.5, None)),
            {"kind": "sleep", "params": None},
            {"kind": "sleep", "params": {"range": None}},
            {"kind": "sleep", "title": None},
            {"kind": "sleep", "title": "t" * 61},
        ]
        for panel in bad:
            with self.subTest(panel=panel), self.assertLogs("apps.router.panels", level="WARNING") as logs:
                self.assertEqual(validate_panels([PANELS[0], panel, PANELS[1]]), PANELS[:2])
            self.assertNotIn("private-sentinel", str([vars(r) for r in logs.records]))

    def test_limit_and_bad_envelope(self):
        with self.assertLogs("apps.router.panels", level="WARNING"):
            self.assertEqual(len(validate_panels(PANELS * 2)), 6)
            self.assertEqual(validate_panels({"kind": "sleep"}), [])

    def test_fence_placement_and_absence(self):
        for source, expected in [
            ("Hello\n" + block(), "Hello"),
            (block(), ""),
            ("Before\n" + block() + "\nAfter", "Before\nAfter"),
        ]:
            with self.subTest(source=source):
                self.assertEqual(extract_panels(source), (expected, PANELS))
        for source in ("  Ordinary text\n", '```json\n{"a":1}\n```', "", "Use `nbhd-panels`."):
            self.assertEqual(extract_panels(source), (source, []))

    def test_bad_and_unclosed_fences_are_removed_without_logging_payload(self):
        for source in ("```nbhd-panels\nprivate-sentinel\n```", "```nbhd-panels\nprivate-sentinel", block({})):
            with self.subTest(source=source), self.assertLogs("apps.router.panels", level="WARNING") as logs:
                self.assertEqual(extract_panels("Before\n" + source), ("Before", []))
            self.assertNotIn("private-sentinel", str([vars(r) for r in logs.records]))

    def test_multiple_blocks_first_valid_wins_and_all_are_stripped(self):
        with self.assertLogs("apps.router.panels", level="WARNING"):
            text, panels = extract_panels("```nbhd-panels\nbad\n```\n" + block(PANELS[:1]) + "\n" + block(PANELS[1:]))
        self.assertEqual((text, panels), ("", PANELS[:1]))
        self.assertEqual(extract_panels(block([]) + "\n" + block()), ("", []))
        with self.assertLogs("apps.router.panels", level="WARNING"):
            self.assertEqual(extract_panels(block([{}]) + "\n" + block()), ("", PANELS))

    def test_streaming_partial_and_complete_blocks_are_hidden(self):
        from apps.router.chat_views import _parse_partial

        for fragment in ("```nbhd-", "```nbhd-panels\n[{", block()):
            with self.subTest(fragment=fragment), self.assertNoLogs("apps.router.panels", level="WARNING"):
                self.assertEqual(_parse_partial({"text": "Hello\n" + fragment, "seq": 1}), ("Hello", 1))

    def test_serializer_drops_bad_panels_but_preserves_message(self):
        from apps.router.cron_delivery import SendToUserSerializer

        for value in ([{"kind": "unknown"}, PANELS[0]], "wrong envelope"):
            serializer = SendToUserSerializer(data={"message": "Hello", "panels": value})
            with self.assertLogs("apps.router.panels", level="WARNING"):
                self.assertTrue(serializer.is_valid(), serializer.errors)
            self.assertEqual(serializer.validated_data["message"], "Hello")

    def test_gate_is_exact_case_insensitive_and_has_no_wildcard(self):
        tenant = SimpleNamespace(id=uuid4())
        for gate, expected in (("", False), ("*", False), (str(uuid4()), False), (f" {str(tenant.id).upper()} ", True)):
            with self.subTest(gate=gate), override_settings(CHAT_SHAPE_TENANT_IDS=gate):
                self.assertEqual(chat_panels_enabled(tenant), expected)
                self.assertFalse(chat_panels_enabled(None))

    def test_chat_preamble_is_byte_identical_off_gate_and_single_line_on_gate(self):
        from apps.router.panels import CHAT_PANEL_INSTRUCTION
        from apps.router.services import build_chat_context_marker, build_coalesced_chat_marker

        tenant = SimpleNamespace(id=uuid4())
        expected = "[chat via NBHD app: user is mid-conversation, reply concisely without loading workspace docs unless the question explicitly requires it]\n"
        with override_settings(CHAT_SHAPE_TENANT_IDS=""):
            self.assertEqual(build_chat_context_marker("ios", tenant=tenant), expected)
            coalesced = build_coalesced_chat_marker("ios", tenant=tenant)
        with override_settings(CHAT_SHAPE_TENANT_IDS=str(tenant.id)):
            self.assertEqual(build_chat_context_marker("ios", tenant=tenant), expected + CHAT_PANEL_INSTRUCTION)
            self.assertEqual(build_coalesced_chat_marker("ios", tenant=tenant), coalesced + CHAT_PANEL_INSTRUCTION)
            self.assertEqual(CHAT_PANEL_INSTRUCTION.count("\n"), 1)
            for channel in ("telegram", "line", None):
                self.assertNotIn("nbhd-panels", build_chat_context_marker(channel, tenant=tenant))


@override_settings(
    NBHD_DISABLE_BACKGROUND_THREADS=True, NBHD_INTERNAL_API_KEY="test-key", TELEGRAM_BOT_TOKEN="test-token"
)
class PanelPersistenceTests(TestCase):
    def setUp(self):
        from apps.router.cron_delivery import _rate_counts
        from apps.router.models import ChatThread
        from apps.tenants.models import Tenant, User
        from apps.tenants.test_utils import seed_internal_key

        self.user = User.objects.create_user(username="panels", telegram_chat_id=12345)
        self.tenant = Tenant.objects.create(user=self.user, status="active")
        self.thread = ChatThread.objects.create(tenant=self.tenant, user=self.user)
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        seed_internal_key(self.tenant)
        _rate_counts.clear()

    def test_proactive_app_telegram_and_line_store_panels_transport_text_only(self):
        from rest_framework.response import Response

        from apps.router.cron_delivery import CronDeliveryView
        from apps.router.models import ProactiveOutbound

        for channel in ("app", "telegram", "line"):
            with (
                self.subTest(channel=channel),
                patch.object(CronDeliveryView, "_resolve_channel", return_value=channel),
                patch.object(
                    CronDeliveryView, "_send_via_telegram", return_value=Response({"status": "sent"})
                ) as telegram,
                patch.object(CronDeliveryView, "_send_via_line", return_value=Response({"status": "sent"})) as line,
                patch("apps.router.proactive_context._dispatch_ios_push"),
            ):
                self.user.line_user_id = "U-panels"
                self.user.save(update_fields=["line_user_id"])
                response = self.client.post(
                    f"/api/v1/integrations/runtime/{self.tenant.id}/send-to-user/",
                    {"message": "Good morning", "panels": PANELS},
                    format="json",
                    HTTP_X_NBHD_INTERNAL_KEY="test-key",
                    HTTP_X_NBHD_TENANT_ID=str(self.tenant.id),
                )
                self.assertEqual(response.status_code, 200, response.data)
                row = ProactiveOutbound.objects.filter(tenant=self.tenant, channel=channel).latest("created_at")
                self.assertEqual(row.panels, PANELS)
                self.assertEqual(row.message_text, "Good morning")
                transport = telegram if channel == "telegram" else line
                if channel != "app":
                    self.assertIn("Good morning", transport.call_args.kwargs.values())
                    self.assertNotIn("panels", transport.call_args.kwargs)

    def test_panel_only_proactive_send(self):
        from apps.router.chat_history import _proactive_rows
        from apps.router.cron_delivery import CronDeliveryView
        from apps.router.models import ProactiveOutbound

        with (
            patch.object(CronDeliveryView, "_resolve_channel", return_value="app"),
            patch("apps.router.proactive_context._dispatch_ios_push"),
        ):
            response = self.client.post(
                f"/api/v1/integrations/runtime/{self.tenant.id}/send-to-user/",
                {"message": "", "panels": PANELS},
                format="json",
                HTTP_X_NBHD_INTERNAL_KEY="test-key",
                HTTP_X_NBHD_TENANT_ID=str(self.tenant.id),
            )
        self.assertEqual(response.status_code, 200, response.data)
        row = ProactiveOutbound.objects.get(tenant=self.tenant)
        self.assertEqual(_proactive_rows(row, self.thread.id)[0]["msg"]["panels"], PANELS)

    def test_coalesced_reply_stores_only_last_row_and_feed_keeps_panel_only_reply(self):
        from apps.router.chat_history import _app_rows
        from apps.router.chat_views import _serialize_message
        from apps.router.models import AppChatMessage
        from apps.router.pending_queue import _store_ios_turn_reply

        rows = [
            AppChatMessage.objects.create(
                tenant=self.tenant, user=self.user, thread=self.thread, client_msg_id=str(uuid4()), user_text="Hello"
            )
            for _ in range(3)
        ]
        batch = [SimpleNamespace(payload={"client_msg_id": row.client_msg_id}) for row in rows]
        with patch("apps.router.pending_queue._dispatch_push"):
            _store_ios_turn_reply(self.tenant, batch, block())
        for row in rows:
            row.refresh_from_db()
            self.assertEqual(row.status, "ready")
            self.assertEqual(row.reply_text, "")
            if row != rows[-1]:
                self.assertIsNone(row.panels)
                self.assertFalse(any(r["msg"]["role"] == "assistant" for r in _app_rows(row, self.thread.id)))
        last = rows[-1]
        self.assertEqual(last.panels, PANELS)
        self.assertEqual(_serialize_message(last)["panels"], PANELS)
        self.assertEqual(_app_rows(last, self.thread.id)[-1]["msg"]["panels"], PANELS)
        response = self.client.get("/api/v1/chat/messages/", {"since": ""})
        self.assertEqual(response.status_code, 200)
        attached = [r for r in response.data["messages"] if r.get("panels")]
        self.assertEqual(len(attached), 1)
        self.assertEqual(attached[0]["panels"], PANELS)

    def test_coalesced_preamble_gets_one_panel_instruction(self):
        from apps.router.pending_queue import _build_batch_chat_content

        batch = [
            SimpleNamespace(payload={}, user_text="hi", channel_user_id="t", created_at=timezone.now())
            for _ in range(2)
        ]
        with override_settings(CHAT_SHAPE_TENANT_IDS=str(self.tenant.id)):
            content, _, _ = _build_batch_chat_content(batch, "t", channel="ios", tenant=self.tenant)
        self.assertEqual(content.count("nbhd-panels"), 1)

    def test_panels_never_reach_telegram_drain_poller_or_line_delivery(self):
        from unittest.mock import MagicMock

        from apps.router.conversation_capture import clean_reply_for_capture
        from apps.router.line_webhook import relay_ai_response_to_line
        from apps.router.pending_queue import relay_ai_response_to_telegram
        from apps.router.poller import TelegramPoller

        for fence in (block(), "```nbhd-panels\nprivate-sentinel\n```"):
            source = "Before\n" + fence + "\nAfter"
            with patch("apps.router.pending_queue.httpx.post") as send:
                send.return_value.is_success = True
                send.return_value.status_code = 200
                relay_ai_response_to_telegram(self.tenant, 12345, source)
                bodies = str([c.kwargs.get("json") for c in send.call_args_list])
                self.assertIn("Before", bodies)
                self.assertIn("After", bodies)
                self.assertNotIn("nbhd-panels", bodies)
                self.assertNotIn("last_night", bodies)
                self.assertNotIn("private-sentinel", bodies)
            poller = object.__new__(TelegramPoller)
            poller.bot_token = "test-token"
            poller._http = MagicMock()
            poller._send_message = MagicMock()
            poller._send_photo = MagicMock()
            poller._send_markdown = MagicMock()
            poller._send_rich_response(12345, self.tenant, source)
            calls = str(poller._send_markdown.call_args_list) + str(poller._send_message.call_args_list)
            self.assertIn("Before", calls)
            self.assertNotIn("nbhd-panels", calls)
            self.assertNotIn("private-sentinel", calls)
            with patch("apps.router.line_webhook._send_line_messages", return_value=True) as send:
                self.assertTrue(relay_ai_response_to_line(self.tenant, "U-panels", source))
                self.assertIn("Before", str(send.call_args))
                self.assertNotIn("nbhd-panels", str(send.call_args))
                self.assertNotIn("private-sentinel", str(send.call_args))
            self.assertEqual(clean_reply_for_capture(self.tenant, source), "Before\nAfter")

    def test_panel_titles_are_guarded_at_rest_and_rehydrated_at_both_read_seams(self):
        from apps.router.chat_history import _app_rows, _proactive_rows
        from apps.router.chat_views import _serialize_message
        from apps.router.models import AppChatMessage, ProactiveOutbound
        from apps.router.pending_queue import _store_ios_turn_reply
        from apps.router.proactive_context import record_proactive_outbound

        self.tenant.pii_entity_map = {"[PERSON_1]": "Alice"}
        self.tenant.save(update_fields=["pii_entity_map"])
        titled = [{"kind": "tasks", "params": {}, "title": "Ask Alice"}]
        row = AppChatMessage.objects.create(
            tenant=self.tenant, user=self.user, thread=self.thread, client_msg_id="titled", user_text="Hi"
        )
        with patch("apps.router.pending_queue._dispatch_push"):
            _store_ios_turn_reply(
                self.tenant, [SimpleNamespace(payload={"client_msg_id": "titled"})], "Hello\n" + block(titled)
            )
        row.refresh_from_db()
        self.assertNotIn("Alice", str(row.panels))
        self.assertEqual(_serialize_message(row)["panels"], titled)
        self.assertEqual(_app_rows(row, self.thread.id, self.tenant.pii_entity_map)[-1]["msg"]["panels"], titled)
        with patch("apps.router.proactive_context._dispatch_ios_push"):
            proactive = record_proactive_outbound(
                tenant=self.tenant,
                channel="app",
                channel_user_id=str(self.user.id),
                message_text="Hello",
                panels=titled,
            )
        self.assertIsInstance(proactive, ProactiveOutbound)
        self.assertNotIn("Alice", str(proactive.panels))
        self.assertEqual(
            _proactive_rows(proactive, self.thread.id, self.tenant.pii_entity_map)[0]["msg"]["panels"], titled
        )
