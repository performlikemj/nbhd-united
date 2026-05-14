"""Tests for the dreaming plugin entry + the daily-note backfill command.

Dreaming is the background consolidation layer of memory-core. It runs
Light → Deep → REM phases on a cron, promotes durable short-term
signals into MEMORY.md, and writes a Dream Diary into DREAMS.md.

Phase 5 enables dreaming behind ``experimental_dreaming_enabled``
(default off, requires ``experimental_memory_core_enabled``). It also
ships a one-off backfill command that replays
``pending_messages.payload`` into workspace daily notes so dreaming
has historical material to consolidate from.
"""

from __future__ import annotations

import logging
from datetime import datetime
from io import StringIO
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.core.management import call_command
from django.test import TestCase

from apps.orchestrator.config_generator import (
    _build_memory_core_plugin_entry,
    generate_openclaw_config,
)
from apps.router.models import PendingMessage
from apps.tenants.services import create_tenant


class DreamingFlagOffTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="DreamOff", telegram_chat_id=750001)
        self.assertFalse(self.tenant.experimental_dreaming_enabled)

    def test_helper_returns_none(self):
        self.assertIsNone(_build_memory_core_plugin_entry(self.tenant))

    def test_full_config_omits_memory_core_entry(self):
        config = generate_openclaw_config(self.tenant)
        entries = config.get("plugins", {}).get("entries", {})
        self.assertNotIn("memory-core", entries)


class DreamingWithoutMemoryCoreTest(TestCase):
    """Dreaming requires memory-core; the helper must skip + warn when
    the dependency is missing."""

    def setUp(self):
        self.tenant = create_tenant(display_name="DreamNoMC", telegram_chat_id=750002)
        self.tenant.experimental_dreaming_enabled = True
        self.tenant.experimental_memory_core_enabled = False
        self.tenant.save()

    def test_helper_returns_none_and_logs_warning(self):
        with self.assertLogs("apps.orchestrator.config_generator", level=logging.WARNING) as cm:
            entry = _build_memory_core_plugin_entry(self.tenant)
        self.assertIsNone(entry)
        joined = "\n".join(cm.output)
        self.assertIn("dreaming", joined.lower())
        self.assertIn("memory-core", joined.lower())


class DreamingFullyEnabledTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="DreamOn", telegram_chat_id=750003)
        self.tenant.experimental_memory_core_enabled = True
        self.tenant.experimental_dreaming_enabled = True
        self.tenant.save()

    def test_helper_returns_verified_shape(self):
        entry = _build_memory_core_plugin_entry(self.tenant)
        self.assertIsNotNone(entry)
        self.assertTrue(entry["enabled"])
        # Verified shape per docs/concepts/dreaming.md "Enable dreaming" tab
        self.assertEqual(entry["config"], {"dreaming": {"enabled": True}})

    def test_full_config_includes_memory_core_entry(self):
        config = generate_openclaw_config(self.tenant)
        entries = config["plugins"]["entries"]
        self.assertIn("memory-core", entries)
        self.assertTrue(entries["memory-core"]["config"]["dreaming"]["enabled"])


class BackfillDailyNotesCommandTest(TestCase):
    """The backfill command groups pending_messages by the user's local
    date and writes each day's user-side messages into the corresponding
    workspace daily note. Dry-run mode counts but doesn't write."""

    def setUp(self):
        self.tenant = create_tenant(
            display_name="Backfill",
            telegram_chat_id=750004,
        )
        # Pin a Tokyo TZ user so date-boundary tests are deterministic
        self.tenant.user.timezone = "Asia/Tokyo"
        self.tenant.user.save()

        tz = ZoneInfo("Asia/Tokyo")

        # Two messages on May 10 (local), one on May 11 (local). The model
        # uses auto_now_add for created_at, so we create then UPDATE the
        # timestamp via the manager (which bypasses auto_now_add).
        m1 = PendingMessage.objects.create(
            tenant=self.tenant,
            channel="line",
            channel_user_id="line:test",
            payload={"message_text": "i was 69kg today"},
            user_text="i was 69kg today",
        )
        m2 = PendingMessage.objects.create(
            tenant=self.tenant,
            channel="line",
            channel_user_id="line:test",
            payload={"message_text": "also 69.4 yesterday"},
            user_text="also 69.4 yesterday",
        )
        m3 = PendingMessage.objects.create(
            tenant=self.tenant,
            channel="line",
            channel_user_id="line:test",
            payload={"message_text": "morning check-in"},
            user_text="morning check-in",
        )
        PendingMessage.objects.filter(id=m1.id).update(
            created_at=datetime(2026, 5, 10, 14, 0, tzinfo=tz)  # 14:00 JST
        )
        PendingMessage.objects.filter(id=m2.id).update(
            created_at=datetime(2026, 5, 10, 18, 30, tzinfo=tz)  # same day
        )
        PendingMessage.objects.filter(id=m3.id).update(
            created_at=datetime(2026, 5, 11, 8, 0, tzinfo=tz)  # next day
        )

    @patch("apps.orchestrator.azure_client.download_workspace_file")
    @patch("apps.orchestrator.azure_client.upload_workspace_file")
    def test_writes_one_file_per_local_day(self, mock_upload, mock_download):
        mock_download.return_value = None  # no existing daily notes

        out = StringIO()
        call_command(
            "backfill_daily_notes_from_messages",
            "--tenant",
            str(self.tenant.id),
            "--start",
            "2026-05-10",
            "--end",
            "2026-05-11",
            stdout=out,
        )

        # Two days → two upload calls
        self.assertEqual(mock_upload.call_count, 2)

        uploads_by_path = {call.args[1]: call.args[2] for call in mock_upload.call_args_list}
        self.assertIn("workspace/memory/2026-05-10.md", uploads_by_path)
        self.assertIn("workspace/memory/2026-05-11.md", uploads_by_path)

        # May 10 had two messages — both should be in the file
        may10 = uploads_by_path["workspace/memory/2026-05-10.md"]
        self.assertIn("i was 69kg today", may10)
        self.assertIn("also 69.4 yesterday", may10)

        # May 11 had one message
        may11 = uploads_by_path["workspace/memory/2026-05-11.md"]
        self.assertIn("morning check-in", may11)
        self.assertNotIn("69kg", may11)  # didn't bleed across day boundary

    @patch("apps.orchestrator.azure_client.download_workspace_file")
    @patch("apps.orchestrator.azure_client.upload_workspace_file")
    def test_dry_run_does_not_upload(self, mock_upload, mock_download):
        mock_download.return_value = None

        out = StringIO()
        call_command(
            "backfill_daily_notes_from_messages",
            "--tenant",
            str(self.tenant.id),
            "--start",
            "2026-05-10",
            "--end",
            "2026-05-11",
            "--dry-run",
            stdout=out,
        )

        mock_upload.assert_not_called()
        output = out.getvalue()
        self.assertIn("dry-run", output.lower())
        self.assertIn("2026-05-10", output)
        self.assertIn("2026-05-11", output)

    @patch("apps.orchestrator.azure_client.download_workspace_file")
    @patch("apps.orchestrator.azure_client.upload_workspace_file")
    def test_appends_to_existing_daily_note(self, mock_upload, mock_download):
        # Simulate an existing daily note from prior agent activity
        mock_download.return_value = "# 2026-05-10\n\n## morning-report\n\nweather: clear\n"

        call_command(
            "backfill_daily_notes_from_messages",
            "--tenant",
            str(self.tenant.id),
            "--start",
            "2026-05-10",
            "--end",
            "2026-05-10",
            stdout=StringIO(),
        )

        # The existing content must be preserved + the replay appended
        merged = mock_upload.call_args_list[0].args[2]
        self.assertIn("morning-report", merged)
        self.assertIn("weather: clear", merged)
        self.assertIn("i was 69kg today", merged)
        self.assertIn("also 69.4 yesterday", merged)
        # Replay header is present
        self.assertIn("Replayed conversation", merged)

    def test_pulls_full_payload_not_truncated_user_text(self):
        # user_text is clipped at 200 chars in pending_messages; we
        # must use payload.message_text to get the full content. Insert
        # a message where payload differs from user_text to verify.
        full = "x" * 500
        m = PendingMessage.objects.create(
            tenant=self.tenant,
            channel="line",
            channel_user_id="line:test",
            payload={"message_text": full},
            user_text=full[:200],  # what the queue actually stores
        )
        PendingMessage.objects.filter(id=m.id).update(
            created_at=datetime(2026, 5, 12, 10, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
        )

        with (
            patch("apps.orchestrator.azure_client.download_workspace_file", return_value=None),
            patch("apps.orchestrator.azure_client.upload_workspace_file") as mock_upload,
        ):
            call_command(
                "backfill_daily_notes_from_messages",
                "--tenant",
                str(self.tenant.id),
                "--start",
                "2026-05-12",
                "--end",
                "2026-05-12",
                stdout=StringIO(),
            )

        merged = mock_upload.call_args_list[0].args[2]
        # Full 500-char body present (not just 200)
        self.assertIn(full, merged)
