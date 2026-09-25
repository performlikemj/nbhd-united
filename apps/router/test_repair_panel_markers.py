"""The one-off repair is scoped, dry by default and idempotent."""

from datetime import timedelta
from io import StringIO
from uuid import uuid4

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.router.models import AppChatMessage, ChatThread, ProactiveOutbound
from apps.router.test_panels import MORNING_MARKERS, MORNING_PANELS, PANELS
from apps.tenants.models import Tenant, User


class RepairPanelMarkersTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="panel-repair")
        self.tenant = Tenant.objects.create(user=self.user)
        self.thread = ChatThread.objects.create(tenant=self.tenant, user=self.user)
        self.since = timezone.now() - timedelta(hours=1)
        self.enterContext(override_settings(CHAT_PANELS_TOOL_TENANT_IDS=str(self.tenant.id), CHAT_SHAPE_TENANT_IDS=""))

    def rows(self, *, panels=None):
        text = "private-sentinel\n\n" + MORNING_MARKERS + "\n"
        return [
            ProactiveOutbound.objects.create(
                tenant=self.tenant, channel="app", channel_user_id=str(self.user.id), message_text=text, panels=panels
            ),
            AppChatMessage.objects.create(
                tenant=self.tenant,
                user=self.user,
                thread=self.thread,
                client_msg_id=str(uuid4()),
                user_text="Keep user input " + MORNING_MARKERS,
                reply_text=text,
                panels=panels,
            ),
        ]

    def run_repair(self, *, apply=False, since=None):
        output = StringIO()
        call_command(
            "repair_panel_markers",
            tenant=self.tenant.id,
            since=since or self.since.isoformat(),
            apply=apply,
            stdout=output,
        )
        self.assertNotIn("private-sentinel", output.getvalue())
        self.assertNotIn(str(self.tenant.id), output.getvalue())
        self.assertNotIn("[[panel:", output.getvalue())
        return output.getvalue()

    def test_dry_run_then_apply_and_repeat(self):
        rows = self.rows()
        result = self.run_repair()
        self.assertEqual(result.count("matched=1 changed=1 panels_added=4 written=0"), 2)
        for row in rows:
            row.refresh_from_db()
            self.assertIsNone(row.panels)
            self.assertIn("[[panel:", row.message_text if isinstance(row, ProactiveOutbound) else row.reply_text)
        result = self.run_repair(apply=True)
        self.assertEqual(result.count("matched=1 changed=1 panels_added=4 written=1"), 2)
        for row in rows:
            row.refresh_from_db()
            self.assertEqual(row.panels, MORNING_PANELS)
            self.assertEqual(
                row.message_text if isinstance(row, ProactiveOutbound) else row.reply_text, "private-sentinel"
            )
        self.assertIn("[[panel:", rows[1].user_text)
        self.assertEqual(self.run_repair(apply=True).count("matched=0 changed=0 panels_added=0 written=0"), 2)

    def test_tool_gate_required_even_when_shape_gate_enabled(self):
        rows = self.rows()
        with override_settings(CHAT_PANELS_TOOL_TENANT_IDS="", CHAT_SHAPE_TENANT_IDS=str(self.tenant.id)):
            self.run_repair(apply=True)
        for row in rows:
            row.refresh_from_db()
            self.assertIsNone(row.panels)
            self.assertEqual(
                row.message_text if isinstance(row, ProactiveOutbound) else row.reply_text, "private-sentinel"
            )

    def test_existing_panels_win_and_proactive_items_refreshed(self):
        rows = self.rows(panels=PANELS[:1])
        ProactiveOutbound.objects.filter(pk=rows[0].pk).update(parsed_items=["[[panel:sleep]]"])
        self.run_repair(apply=True)
        for row in rows:
            row.refresh_from_db()
            self.assertEqual(row.panels, PANELS[:1])
        self.assertEqual(rows[0].parsed_items, [])

    def test_scope_excludes_older_rows_and_other_tenant(self):
        rows = self.rows()
        for row in rows:
            type(row).objects.filter(pk=row.pk).update(created_at=self.since - timedelta(seconds=1))
        other_user = User.objects.create_user(username="other-panel-repair")
        other_tenant = Tenant.objects.create(user=other_user)
        other_thread = ChatThread.objects.create(tenant=other_tenant, user=other_user)
        other_proactive = ProactiveOutbound.objects.create(
            tenant=other_tenant, channel="app", message_text=MORNING_MARKERS
        )
        other_chat = AppChatMessage.objects.create(
            tenant=other_tenant,
            user=other_user,
            thread=other_thread,
            client_msg_id="other",
            user_text="Hi",
            reply_text=MORNING_MARKERS,
        )
        self.assertEqual(self.run_repair(apply=True).count("matched=0"), 2)
        for row in (*rows, other_proactive, other_chat):
            row.refresh_from_db()
            self.assertIsNone(row.panels)
            self.assertIn("[[panel:", row.message_text if isinstance(row, ProactiveOutbound) else row.reply_text)

    def test_invalid_siblings_are_dropped_and_empty_lists_repaired(self):
        rows = self.rows(panels=[])
        for row, field in ((rows[0], "message_text"), (rows[1], "reply_text")):
            type(row).objects.filter(pk=row.pk).update(**{field: "[[panel:private-sentinel]]\n" + MORNING_MARKERS})
        with self.assertLogs("apps.router.panels", level="WARNING") as logs:
            self.run_repair(apply=True)
        self.assertNotIn("private-sentinel", str([vars(record) for record in logs.records]))
        for row in rows:
            row.refresh_from_db()
            self.assertEqual(row.panels, MORNING_PANELS)

    def test_invalid_since_is_rejected_without_content(self):
        for since in ("private-sentinel", "2026-99-99T00:00:00Z"):
            with self.assertRaisesMessage(CommandError, "--since must be an ISO timestamp."):
                self.run_repair(since=since)
