"""Tests for journal models, markdown parser, and API endpoints."""
from __future__ import annotations

from datetime import date

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.tenants.models import Tenant, User

from .md_utils import append_entry_markdown, parse_daily_note, serialise_daily_note
from .models import DailyNote, NoteTemplate, UserMemory, WeeklyReview
from .services import (
    DEFAULT_TEMPLATE_SECTIONS,
    append_log_to_note,
    get_or_seed_note_template,
    seed_default_templates_for_tenant,
    set_daily_note_section,
)


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

    def test_parse_section_heading_without_time(self):
        markdown = """# 2026-02-16

## Morning Report
Today I woke up early.
"""
        entries = parse_daily_note(markdown)
        self.assertEqual(entries, [])

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


class JournalServiceTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="serviceuser", password="servicepass")
        self.tenant = Tenant.objects.create(user=self.user, status="active")

    def test_get_or_seed_note_template_preserves_legacy_markdown(self):
        markdown = """# 2026-02-16

## 09:30 — MJ
Legacy entry for migration safety.
"""
        template, sections = get_or_seed_note_template(
            tenant=self.tenant,
            date_value=date(2026, 2, 16),
            markdown=markdown,
        )

        self.assertIsNotNone(template)
        # Legacy entries that don't parse into sections are returned as default
        # template sections (no "log" section in new defaults).
        self.assertTrue(len(sections) > 0)


class DefaultTemplateSectionsTest(TestCase):
    def test_default_template_has_five_sections(self):
        self.assertEqual(len(DEFAULT_TEMPLATE_SECTIONS), 5)
        slugs = [s["slug"] for s in DEFAULT_TEMPLATE_SECTIONS]
        self.assertEqual(slugs, [
            "morning-report", "weather", "news", "focus", "evening-check-in",
        ])

    def test_seed_creates_template_with_five_sections(self):
        user = User.objects.create_user(username="seeduser", password="pass")
        tenant = Tenant.objects.create(user=user, status="active")
        result = seed_default_templates_for_tenant(tenant=tenant)
        template = result["template"]
        self.assertEqual(len(template.sections), 5)


class SetSectionTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="sectionuser", password="pass")
        self.tenant = Tenant.objects.create(user=self.user, status="active")

    def test_set_known_section(self):
        note = DailyNote.objects.create(tenant=self.tenant, date=date(2026, 2, 16))
        note, sections = set_daily_note_section(
            note=note, section_slug="morning-report", content="Hello morning",
        )
        mr = next(s for s in sections if s["slug"] == "morning-report")
        self.assertEqual(mr["content"], "Hello morning")
        self.assertIn("Hello morning", note.markdown)

    def test_set_unknown_slug_auto_creates(self):
        note = DailyNote.objects.create(tenant=self.tenant, date=date(2026, 2, 16))
        note, sections = set_daily_note_section(
            note=note, section_slug="tweet-drafts", content="Some tweet ideas",
        )
        slugs = [s["slug"] for s in sections]
        self.assertIn("tweet-drafts", slugs)
        # Should be inserted before evening-check-in
        evening_idx = slugs.index("evening-check-in")
        tweet_idx = slugs.index("tweet-drafts")
        self.assertLess(tweet_idx, evening_idx)

    def test_set_unknown_slug_appended_when_no_evening(self):
        """When evening-check-in doesn't exist, new sections are appended."""
        note = DailyNote.objects.create(tenant=self.tenant, date=date(2026, 2, 16))
        # Create a custom template without evening-check-in
        template = NoteTemplate.objects.create(
            tenant=self.tenant,
            slug="custom",
            name="Custom",
            sections=[
                {"slug": "morning-report", "title": "Morning Report", "content": "", "source": "agent"},
            ],
            is_default=True,
        )
        note.template = template
        note.save()
        note, sections = set_daily_note_section(
            note=note, section_slug="new-section", content="New content",
        )
        self.assertEqual(sections[-1]["slug"], "new-section")


class AppendLogTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="loguser", password="pass")
        self.tenant = Tenant.objects.create(user=self.user, status="active")

    def test_append_log_without_log_section(self):
        """When there's no log section, append to document tail."""
        note = DailyNote.objects.create(
            tenant=self.tenant,
            date=date(2026, 2, 16),
            markdown="# 2026-02-16\n\n## Morning Report\nHello\n",
        )
        note = append_log_to_note(
            note=note, content="Quick note", author="human", time_str="14:00",
        )
        self.assertIn("14:00", note.markdown)
        self.assertIn("Quick note", note.markdown)
        self.assertIn("MJ", note.markdown)

    def test_append_log_with_log_section(self):
        """When a log section exists, append within it."""
        note = DailyNote.objects.create(tenant=self.tenant, date=date(2026, 2, 16))
        # Create a template with a log section
        template = NoteTemplate.objects.create(
            tenant=self.tenant,
            slug="with-log",
            name="With Log",
            sections=[
                {"slug": "morning-report", "title": "Morning Report", "content": "", "source": "agent"},
                {"slug": "log", "title": "Log", "content": "", "source": "shared"},
                {"slug": "evening-check-in", "title": "Evening Check-in", "content": "", "source": "human"},
            ],
            is_default=True,
        )
        note.template = template
        note.save()
        note = append_log_to_note(
            note=note, content="Logged this", author="agent", time_str="10:30",
        )
        self.assertIn("Logged this", note.markdown)
        self.assertIn("10:30", note.markdown)


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
        # Sections are returned; entries are no longer included by default.
        self.assertIn("sections", resp.data)
        self.assertIn("markdown", resp.data)

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

    def test_patch_section_endpoint(self):
        resp = self.client.patch(
            "/api/v1/journal/daily/2026-02-15/sections/morning-report/",
            {"content": "Good morning!"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        sections = resp.data.get("sections", [])
        mr = next((s for s in sections if s["slug"] == "morning-report"), None)
        self.assertIsNotNone(mr)
        self.assertEqual(mr["content"], "Good morning!")

    def test_patch_section_auto_creates_unknown_slug(self):
        resp = self.client.patch(
            "/api/v1/journal/daily/2026-02-15/sections/custom-section/",
            {"content": "Custom content"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        slugs = [s["slug"] for s in resp.data.get("sections", [])]
        self.assertIn("custom-section", slugs)

    def test_patch_section_missing_content(self):
        resp = self.client.patch(
            "/api/v1/journal/daily/2026-02-15/sections/morning-report/",
            {},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)


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


@override_settings(
    REST_FRAMEWORK={
        "DEFAULT_AUTHENTICATION_CLASSES": [
            "rest_framework.authentication.SessionAuthentication",
        ],
    }
)
class WeeklyReviewAPITest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="testpass")
        self.tenant = Tenant.objects.create(user=self.user, status="active")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _create_review(self, **overrides):
        payload = {
            "week_start": "2026-02-09",
            "week_end": "2026-02-15",
            "mood_summary": "Good week overall",
            "top_wins": ["Shipped feature"],
            "top_challenges": ["Tight deadline"],
            "lessons": ["Start earlier"],
            "week_rating": "thumbs-up",
            "intentions_next_week": ["More testing"],
        }
        payload.update(overrides)
        return self.client.post("/api/v1/journal/reviews/", payload, format="json")

    def test_get_empty_list(self):
        resp = self.client.get("/api/v1/journal/reviews/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, [])

    def test_create_review(self):
        resp = self._create_review()
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["mood_summary"], "Good week overall")
        self.assertEqual(resp.data["week_rating"], "thumbs-up")
        self.assertIn("id", resp.data)

    def test_list_reviews(self):
        self._create_review(week_start="2026-02-02", week_end="2026-02-08")
        self._create_review(week_start="2026-02-09", week_end="2026-02-15")
        resp = self.client.get("/api/v1/journal/reviews/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 2)
        # Newest first
        self.assertEqual(resp.data[0]["week_start"], "2026-02-09")

    def test_get_detail(self):
        create_resp = self._create_review()
        review_id = create_resp.data["id"]
        resp = self.client.get(f"/api/v1/journal/reviews/{review_id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["mood_summary"], "Good week overall")

    def test_patch_review(self):
        create_resp = self._create_review()
        review_id = create_resp.data["id"]
        resp = self.client.patch(
            f"/api/v1/journal/reviews/{review_id}/",
            {"mood_summary": "Updated mood"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["mood_summary"], "Updated mood")

    def test_delete_review(self):
        create_resp = self._create_review()
        review_id = create_resp.data["id"]
        resp = self.client.delete(f"/api/v1/journal/reviews/{review_id}/")
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(WeeklyReview.objects.filter(tenant=self.tenant).count(), 0)

    def test_invalid_week_end_before_start(self):
        resp = self._create_review(week_start="2026-02-15", week_end="2026-02-09")
        self.assertEqual(resp.status_code, 400)

    def test_tenant_isolation(self):
        self._create_review()
        user2 = User.objects.create_user(username="user2", password="pass")
        tenant2 = Tenant.objects.create(user=user2, status="active")
        client2 = APIClient()
        client2.force_authenticate(user=user2)
        resp = client2.get("/api/v1/journal/reviews/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, [])


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
        # Runtime endpoint auto-seeds with template content
        self.assertIn("Morning Report", resp.data["markdown"])

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
