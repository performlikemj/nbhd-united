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

        for fragment in (*("```nbhd-panels"[:n] for n in range(1, 15)), "```nbhd-panels\n[{", block()):
            with self.subTest(fragment=fragment), self.assertNoLogs("apps.router.panels", level="WARNING"):
                self.assertEqual(_parse_partial({"text": "Hello\n" + fragment, "seq": 1}), ("Hello", 1))

    def test_streaming_prefixes_release_ordinary_content_and_hide_cap_boundaries(self):
        from apps.router.chat_views import _MAX_PARTIAL_TEXT_CHARS, _parse_partial

        for content in ("```notebook\nhello\n```", "```nbhd-custom\nhello", "```nbhd\nhello"):
            self.assertEqual(_parse_partial({"text": content, "seq": 2}), (content, 2))
        for prefix in ("```n", "```nbhd"):
            lead = "x" * (_MAX_PARTIAL_TEXT_CHARS - len(prefix) - 1)
            raw = lead + "\n" + "```nbhd-panels\n" + json.dumps(PANELS)
            self.assertEqual(_parse_partial({"text": raw, "seq": 1}), (lead, 1))

    def test_serializer_off_gate_drops_explicit_and_fallback_without_content_logs(self):
        from apps.router.cron_delivery import SendToUserSerializer

        tenant = SimpleNamespace(id=uuid4())
        private = [{"kind": "tasks", "title": "private-sentinel"}]
        for gate in ("", str(uuid4())):
            for data in ({"message": "Hello", "panels": private}, {"message": "Hello\n" + block(private)}):
                with override_settings(CHAT_SHAPE_TENANT_IDS=gate), self.assertLogs(level="WARNING") as logs:
                    serializer = SendToUserSerializer(data=data, context={"tenant": tenant})
                    self.assertTrue(serializer.is_valid(), serializer.errors)
                self.assertEqual(serializer.validated_data["message"], "Hello")
                self.assertEqual(serializer.validated_data["panels"], [])
                self.assertNotIn("private-sentinel", str([vars(r) for r in logs.records]))
                self.assertIn("tenant_disabled", str(logs.output))

    def test_serializer_explicit_panels_win_even_when_empty_null_or_invalid(self):
        from apps.router.cron_delivery import SendToUserSerializer

        tenant = SimpleNamespace(id=uuid4())
        for value, expected in (([], []), (None, []), ([{}], []), (PANELS[1:], PANELS[1:])):
            with override_settings(CHAT_SHAPE_TENANT_IDS=str(tenant.id)):
                serializer = SendToUserSerializer(
                    data={"message": "Hello\n" + block(PANELS[:1]), "panels": value}, context={"tenant": tenant}
                )
                self.assertTrue(serializer.is_valid(), serializer.errors)
            self.assertEqual(serializer.validated_data["message"], "Hello")
            self.assertEqual(serializer.validated_data["panels"], expected)

    def test_serializer_drops_bad_panels_but_preserves_message(self):
        from apps.router.cron_delivery import SendToUserSerializer

        tenant = SimpleNamespace(id=uuid4())
        self.enterContext(override_settings(CHAT_SHAPE_TENANT_IDS=str(tenant.id)))
        for value in ([{"kind": "unknown"}, PANELS[0]], "wrong envelope"):
            serializer = SendToUserSerializer(data={"message": "Hello", "panels": value}, context={"tenant": tenant})
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
        self.enterContext(override_settings(CHAT_SHAPE_TENANT_IDS=str(self.tenant.id)))
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

    def _post_proactive(self, body):
        return self.client.post(
            f"/api/v1/integrations/runtime/{self.tenant.id}/send-to-user/",
            body,
            format="json",
            HTTP_X_NBHD_INTERNAL_KEY="test-key",
            HTTP_X_NBHD_TENANT_ID=str(self.tenant.id),
        )

    def test_cron_fences_strip_before_markers_and_transport_on_every_channel(self):
        from rest_framework.response import Response

        from apps.router.cron_delivery import CronDeliveryView, _rate_counts
        from apps.router.models import ProactiveOutbound

        for channel in ("app", "telegram", "line"):
            for enabled in (False, True):
                for fence, expected_panels in ((block(), PANELS), ("```nbhd-panels\nprivate-sentinel\n```", [])):
                    with (
                        self.subTest(channel=channel, enabled=enabled, malformed=not expected_panels),
                        override_settings(CHAT_SHAPE_TENANT_IDS=str(self.tenant.id) if enabled else ""),
                        patch.object(CronDeliveryView, "_resolve_channel", return_value=channel),
                        patch.object(
                            CronDeliveryView, "_send_via_telegram", return_value=Response({"status": "sent"})
                        ) as telegram,
                        patch.object(
                            CronDeliveryView, "_send_via_line", return_value=Response({"status": "sent"})
                        ) as line,
                        patch("apps.router.proactive_context._dispatch_ios_push"),
                    ):
                        _rate_counts.clear()
                        self.user.line_user_id = "U-panels"
                        self.user.save(update_fields=["line_user_id"])
                        response = self._post_proactive(
                            {"message": "Before\n" + fence + "\nAfter\n[[quick-replies: Thanks]]"}
                        )
                        self.assertEqual(response.status_code, 200, response.data)
                        stored = ProactiveOutbound.objects.filter(tenant=self.tenant).latest("created_at")
                        self.assertEqual(stored.message_text, "Before\nAfter")
                        self.assertEqual(stored.quick_replies, ["Thanks"])
                        self.assertEqual(stored.panels or [], expected_panels if enabled else [])
                        if channel != "app":
                            call = (telegram if channel == "telegram" else line).call_args
                            self.assertEqual(call.kwargs["message_text"], "Before\nAfter")
                        else:
                            telegram.assert_not_called()
                            line.assert_not_called()

    def test_panel_only_linked_tenants_persist_app_feed_without_text_transport_or_devices(self):
        from apps.router.chat_history import _proactive_rows
        from apps.router.cron_delivery import CronDeliveryView, _rate_counts
        from apps.router.models import DeviceToken, ProactiveOutbound

        self.assertFalse(DeviceToken.objects.filter(tenant=self.tenant).exists())
        for linked_channel in ("telegram", "line"):
            self.user.telegram_chat_id = 12345 if linked_channel == "telegram" else None
            self.user.line_user_id = "U-panels" if linked_channel == "line" else ""
            self.user.save(update_fields=["telegram_chat_id", "line_user_id"])
            for body in ({"message": "", "panels": PANELS}, {"message": block()}):
                with (
                    self.subTest(channel=linked_channel, body=body),
                    patch.object(CronDeliveryView, "_send_via_telegram") as telegram,
                    patch.object(CronDeliveryView, "_send_via_line") as line,
                    patch("apps.router.proactive_context._dispatch_ios_push"),
                ):
                    _rate_counts.clear()
                    response = self._post_proactive(body)
                    self.assertEqual(response.status_code, 200, response.data)
                    self.assertEqual(response.data["channel"], "app")
                    telegram.assert_not_called()
                    line.assert_not_called()
                    stored = ProactiveOutbound.objects.filter(tenant=self.tenant).latest("created_at")
                    self.assertEqual(stored.channel, "app")
                    self.assertEqual(stored.message_text, "")
                    self.assertEqual(_proactive_rows(stored, self.thread.id)[0]["msg"]["panels"], PANELS)

    def test_non_gated_direct_writers_cannot_store_panels(self):
        from apps.router.models import AppChatMessage
        from apps.router.pending_queue import _store_ios_turn_reply
        from apps.router.proactive_context import record_proactive_outbound

        row = AppChatMessage.objects.create(
            tenant=self.tenant, user=self.user, thread=self.thread, client_msg_id="off", user_text="Hi"
        )
        with (
            override_settings(CHAT_SHAPE_TENANT_IDS=""),
            patch("apps.router.pending_queue._dispatch_push"),
            patch("apps.router.proactive_context._dispatch_ios_push"),
        ):
            _store_ios_turn_reply(self.tenant, [SimpleNamespace(payload={"client_msg_id": "off"})], "Hello\n" + block())
            proactive = record_proactive_outbound(
                tenant=self.tenant,
                channel="app",
                channel_user_id=str(self.user.id),
                message_text="Hello",
                panels=PANELS,
            )
        row.refresh_from_db()
        self.assertIsNone(row.panels)
        self.assertEqual(row.reply_text, "Hello")
        self.assertIsNone(proactive.panels)

    def test_title_expansion_never_persists_partial_placeholders(self):
        from apps.router.chat_history import _app_rows, _proactive_rows
        from apps.router.chat_views import _serialize_message
        from apps.router.models import AppChatMessage
        from apps.router.pending_queue import _store_ios_turn_reply
        from apps.router.proactive_context import record_proactive_outbound

        self.tenant.pii_entity_map = {"[PERSON_12345]": "Alice"}
        self.tenant.save(update_fields=["pii_entity_map"])
        title = "x" * 54 + " Alice"
        panels = [{"kind": "tasks", "params": {}, "title": title}]
        row = AppChatMessage.objects.create(
            tenant=self.tenant, user=self.user, thread=self.thread, client_msg_id="expanded", user_text="Hi"
        )
        with (
            patch("apps.router.pending_queue._dispatch_push"),
            patch("apps.router.proactive_context._dispatch_ios_push"),
        ):
            _store_ios_turn_reply(
                self.tenant, [SimpleNamespace(payload={"client_msg_id": "expanded"})], "Hello\n" + block(panels)
            )
            proactive = record_proactive_outbound(
                tenant=self.tenant,
                channel="app",
                channel_user_id=str(self.user.id),
                message_text="Hello",
                panels=panels,
            )
        row.refresh_from_db()
        expected = "x" * 54 + " "
        for stored in (row, proactive):
            self.assertEqual(stored.panels[0]["title"], expected)
        for response in (
            _serialize_message(row),
            _app_rows(row, self.thread.id, self.tenant.pii_entity_map)[-1]["msg"],
            _proactive_rows(proactive, self.thread.id, self.tenant.pii_entity_map)[0]["msg"],
        ):
            self.assertEqual(response["panels"][0]["title"], expected)
            self.assertNotIn("[PERS", str(response["panels"]))

    def test_titles_rehydrate_then_apply_render_bound(self):
        from apps.router.panels import prepare_panels, rehydrate_panels

        self.tenant.pii_entity_map = {"[PERSON_12345]": "A" * 100}
        stored = prepare_panels(self.tenant, [{"kind": "tasks", "title": "Ask [PERSON_12345]"}])
        self.assertEqual(stored[0]["title"], "Ask [PERSON_12345]")
        rendered = rehydrate_panels(stored, self.tenant.pii_entity_map, tenant_id=self.tenant.id)
        self.assertEqual(rendered[0]["title"], "Ask " + "A" * 56)
