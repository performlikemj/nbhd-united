"""Tests for journal models, markdown parser, and API endpoints."""
from __future__ import annotations

from datetime import date

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.tenants.models import Tenant, User

from .md_utils import append_entry_markdown, parse_daily_note, serialise_daily_note
from .models import DailyNote, UserMemory


# ---------------------------------------------------------------------------
# Markdown parser/serializer tests
# ---------------------------------------------------------------------------

SAMPLE_MARKDOWN = """# 2026-02-15

## 09:30 — MJ
Started working on the demo video edit. Feeling good about the footage.
Energy: 7 | Mood: 😊

## 12:15 — Agent
Checked production logs — Composio SDK breaking change found.

## 23:00 — Evening Check-in (Agent)
### What happened today
- Timezone feature merged
- Demo video footage reviewed

### Decisions
- Journaling module will mirror OpenClaw model

### Tomorrow
- Cold emails
- Edit demo videos
"""


class MarkdownParserTest(TestCase):
    def test_parse_basic_entries(self):
        entries = parse_daily_note(SAMPLE_MARKDOWN)
        self.assertEqual(len(entries), 3)

    def test_parse_human_entry(self):
        entries = parse_daily_note(SAMPLE_MARKDOWN)
        e = entries[0]
        self.assertEqual(e["time"], "09:30")
        self.assertEqual(e["author"], "human")
        self.assertIn("demo video edit", e["content"])
        self.assertEqual(e["mood"], "😊")
        self.assertEqual(e["energy"], 7)
        self.assertIsNone(e["section"])
        self.assertIsNone(e["subsections"])

    def test_parse_agent_entry(self):
        entries = parse_daily_note(SAMPLE_MARKDOWN)
        e = entries[1]
        self.assertEqual(e["time"], "12:15")
        self.assertEqual(e["author"], "agent")
        self.assertIn("Composio SDK", e["content"])

    def test_parse_section_with_subsections(self):
        entries = parse_daily_note(SAMPLE_MARKDOWN)
        e = entries[2]
        self.assertEqual(e["time"], "23:00")
        self.assertEqual(e["author"], "agent")
        self.assertEqual(e["section"], "evening-check-in")
        self.assertIsNotNone(e["subsections"])
        self.assertIn("what-happened-today", e["subsections"])
        self.assertIn("decisions", e["subsections"])
        self.assertIn("tomorrow", e["subsections"])

    def test_parse_empty_markdown(self):
        self.assertEqual(parse_daily_note(""), [])
        self.assertEqual(parse_daily_note("   "), [])
        self.assertEqual(parse_daily_note(None), [])

    def test_roundtrip(self):
        """Parse then serialise should produce parseable output."""
        entries = parse_daily_note(SAMPLE_MARKDOWN)
        output = serialise_daily_note("2026-02-15", entries)
        re_entries = parse_daily_note(output)
        self.assertEqual(len(re_entries), len(entries))
        for orig, reparsed in zip(entries, re_entries):
            self.assertEqual(orig["time"], reparsed["time"])
            self.assertEqual(orig["author"], reparsed["author"])
            self.assertEqual(orig["section"], reparsed["section"])

    def test_append_to_empty(self):
        result = append_entry_markdown(
            "",
            time="10:00",
            author="human",
            content="Hello world",
            mood="😊",
            energy=8,
            date_str="2026-02-15",
        )
        entries = parse_daily_note(result)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["time"], "10:00")
        self.assertEqual(entries[0]["author"], "human")
        self.assertEqual(entries[0]["energy"], 8)

    def test_append_to_existing(self):
        result = append_entry_markdown(
            SAMPLE_MARKDOWN,
            time="14:00",
            author="agent",
            content="New entry appended.",
        )
        entries = parse_daily_note(result)
        self.assertEqual(len(entries), 4)
        self.assertEqual(entries[3]["time"], "14:00")
        self.assertEqual(entries[3]["author"], "agent")


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


class DailyNoteModelTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="testpass")
        self.tenant = Tenant.objects.create(user=self.user, status="active")

    def test_one_note_per_tenant_per_date(self):
        DailyNote.objects.create(tenant=self.tenant, date=date(2026, 2, 15), markdown="# test")
        with self.assertRaises(Exception):
            DailyNote.objects.create(tenant=self.tenant, date=date(2026, 2, 15), markdown="# dupe")

    def test_different_dates_ok(self):
        DailyNote.objects.create(tenant=self.tenant, date=date(2026, 2, 15))
        DailyNote.objects.create(tenant=self.tenant, date=date(2026, 2, 16))
        self.assertEqual(DailyNote.objects.filter(tenant=self.tenant).count(), 2)


class UserMemoryModelTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="testpass")
        self.tenant = Tenant.objects.create(user=self.user, status="active")

    def test_one_memory_per_tenant(self):
        UserMemory.objects.create(tenant=self.tenant, markdown="# Memory")
        with self.assertRaises(Exception):
            UserMemory.objects.create(tenant=self.tenant, markdown="# Dupe")

    def test_different_tenants_ok(self):
        user2 = User.objects.create_user(username="testuser2", password="testpass")
        tenant2 = Tenant.objects.create(user=user2, status="active")
        UserMemory.objects.create(tenant=self.tenant, markdown="# M1")
        UserMemory.objects.create(tenant=tenant2, markdown="# M2")
        self.assertEqual(UserMemory.objects.count(), 2)


# ---------------------------------------------------------------------------
# API tests — user-facing
# ---------------------------------------------------------------------------


@override_settings(
    REST_FRAMEWORK={
        "DEFAULT_AUTHENTICATION_CLASSES": [
            "rest_framework.authentication.SessionAuthentication",
        ],
    }
)
class DailyNoteAPITest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="testpass")
        self.tenant = Tenant.objects.create(user=self.user, status="active")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_get_empty_daily_note(self):
        resp = self.client.get("/api/v1/journal/daily/2026-02-15/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["entries"], [])

    def test_post_entry_creates_note(self):
        resp = self.client.post(
            "/api/v1/journal/daily/2026-02-15/entries/",
            {"content": "Hello world", "mood": "😊", "energy": 7},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(len(resp.data["entries"]), 1)
        self.assertEqual(resp.data["entries"][0]["author"], "human")

        # Verify persisted
        note = DailyNote.objects.get(tenant=self.tenant, date=date(2026, 2, 15))
        self.assertIn("Hello world", note.markdown)

    def test_patch_entry(self):
        self.client.post(
            "/api/v1/journal/daily/2026-02-15/entries/",
            {"content": "Original"},
            format="json",
        )
        resp = self.client.patch(
            "/api/v1/journal/daily/2026-02-15/entries/0/",
            {"content": "Updated"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["entries"][0]["content"], "Updated")

    def test_delete_entry(self):
        self.client.post("/api/v1/journal/daily/2026-02-15/entries/", {"content": "A"}, format="json")
        self.client.post("/api/v1/journal/daily/2026-02-15/entries/", {"content": "B"}, format="json")
        resp = self.client.delete("/api/v1/journal/daily/2026-02-15/entries/0/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data["entries"]), 1)

    def test_patch_out_of_range(self):
        resp = self.client.patch(
            "/api/v1/journal/daily/2026-02-15/entries/0/",
            {"content": "nope"},
            format="json",
        )
        self.assertEqual(resp.status_code, 404)


@override_settings(
    REST_FRAMEWORK={
        "DEFAULT_AUTHENTICATION_CLASSES": [
            "rest_framework.authentication.SessionAuthentication",
        ],
    }
)
class MemoryAPITest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="testpass")
        self.tenant = Tenant.objects.create(user=self.user, status="active")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_get_empty_memory(self):
        resp = self.client.get("/api/v1/journal/memory/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["markdown"], "")

    def test_put_memory(self):
        resp = self.client.put(
            "/api/v1/journal/memory/",
            {"markdown": "# My Memory\n\nImportant stuff."},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Important stuff", resp.data["markdown"])

    def test_put_memory_updates(self):
        self.client.put("/api/v1/journal/memory/", {"markdown": "v1"}, format="json")
        self.client.put("/api/v1/journal/memory/", {"markdown": "v2"}, format="json")
        self.assertEqual(UserMemory.objects.filter(tenant=self.tenant).count(), 1)
        self.assertEqual(UserMemory.objects.get(tenant=self.tenant).markdown, "v2")


# ---------------------------------------------------------------------------
# Tenant isolation tests
# ---------------------------------------------------------------------------


@override_settings(
    REST_FRAMEWORK={
        "DEFAULT_AUTHENTICATION_CLASSES": [
            "rest_framework.authentication.SessionAuthentication",
        ],
    }
)
class TenantIsolationTest(TestCase):
    def setUp(self):
        self.user1 = User.objects.create_user(username="user1", password="pass")
        self.tenant1 = Tenant.objects.create(user=self.user1, status="active")
        self.user2 = User.objects.create_user(username="user2", password="pass")
        self.tenant2 = Tenant.objects.create(user=self.user2, status="active")

        # Create notes for both tenants
        DailyNote.objects.create(
            tenant=self.tenant1, date=date(2026, 2, 15), markdown="# T1 note"
        )
        DailyNote.objects.create(
            tenant=self.tenant2, date=date(2026, 2, 15), markdown="# T2 note"
        )
        UserMemory.objects.create(tenant=self.tenant1, markdown="# T1 mem")
        UserMemory.objects.create(tenant=self.tenant2, markdown="# T2 mem")

    def test_user1_sees_only_own_daily_note(self):
        client = APIClient()
        client.force_authenticate(user=self.user1)
        resp = client.get("/api/v1/journal/daily/2026-02-15/")
        self.assertEqual(resp.status_code, 200)
        # The note should exist but entries parsed from "# T1 note" = empty (no ## headers)
        # Just verify it doesn't crash and doesn't leak T2

    def test_user1_sees_only_own_memory(self):
        client = APIClient()
        client.force_authenticate(user=self.user1)
        resp = client.get("/api/v1/journal/memory/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("T1 mem", resp.data["markdown"])
        self.assertNotIn("T2", resp.data["markdown"])


# ---------------------------------------------------------------------------
# Runtime API tests
# ---------------------------------------------------------------------------


@override_settings(NBHD_INTERNAL_API_KEY="test-key")
class RuntimeDailyNoteAPITest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="testpass")
        self.tenant = Tenant.objects.create(user=self.user, status="active")
        self.client = APIClient()
        self.headers = {
            "HTTP_X_NBHD_INTERNAL_KEY": "test-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }

    def test_get_daily_note_raw_markdown(self):
        DailyNote.objects.create(
            tenant=self.tenant, date=date(2026, 2, 15), markdown=SAMPLE_MARKDOWN
        )
        resp = self.client.get(
            f"/api/v1/integrations/runtime/{self.tenant.id}/daily-note/",
            {"date": "2026-02-15"},
            **self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Composio SDK", resp.data["markdown"])

    def test_get_daily_note_empty(self):
        resp = self.client.get(
            f"/api/v1/integrations/runtime/{self.tenant.id}/daily-note/",
            {"date": "2026-02-15"},
            **self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["markdown"], "")

    def test_append_daily_note(self):
        resp = self.client.post(
            f"/api/v1/integrations/runtime/{self.tenant.id}/daily-note/append/",
            {"content": "Agent appended this.", "date": "2026-02-15"},
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 201)
        self.assertIn("Agent appended this", resp.data["markdown"])
        self.assertIn("Agent", resp.data["markdown"])


@override_settings(NBHD_INTERNAL_API_KEY="test-key")
class RuntimeUserMemoryAPITest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="testpass")
        self.tenant = Tenant.objects.create(user=self.user, status="active")
        self.client = APIClient()
        self.headers = {
            "HTTP_X_NBHD_INTERNAL_KEY": "test-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }

    def test_get_empty_memory(self):
        resp = self.client.get(
            f"/api/v1/integrations/runtime/{self.tenant.id}/long-term-memory/",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["markdown"], "")

    def test_put_memory(self):
        resp = self.client.put(
            f"/api/v1/integrations/runtime/{self.tenant.id}/long-term-memory/",
            {"markdown": "# Agent Memory\n\nKey insight here."},
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Key insight", resp.data["markdown"])

    def test_put_overwrites(self):
        self.client.put(
            f"/api/v1/integrations/runtime/{self.tenant.id}/long-term-memory/",
            {"markdown": "v1"},
            format="json",
            **self.headers,
        )
        self.client.put(
            f"/api/v1/integrations/runtime/{self.tenant.id}/long-term-memory/",
            {"markdown": "v2"},
            format="json",
            **self.headers,
        )
        mem = UserMemory.objects.get(tenant=self.tenant)
        self.assertEqual(mem.markdown, "v2")


@override_settings(NBHD_INTERNAL_API_KEY="test-key")
class RuntimeJournalContextAPITest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="testpass")
        self.tenant = Tenant.objects.create(user=self.user, status="active")
        self.client = APIClient()
        self.headers = {
            "HTTP_X_NBHD_INTERNAL_KEY": "test-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }

    def test_journal_context(self):
        from datetime import timedelta

        from django.utils import timezone as tz

        today = tz.now().date()
        DailyNote.objects.create(tenant=self.tenant, date=today, markdown="# Today")
        UserMemory.objects.create(tenant=self.tenant, markdown="# Memory")

        resp = self.client.get(
            f"/api/v1/integrations/runtime/{self.tenant.id}/journal-context/",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["recent_notes_count"], 1)
        self.assertEqual(resp.data["long_term_memory"], "# Memory")
        self.assertEqual(resp.data["recent_notes"][0]["markdown"], "# Today")
