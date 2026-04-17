"""Tests for daily note chunking and embedding."""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

from django.test import TestCase

from apps.journal.embedding import chunk_markdown, embed_daily_note
from apps.journal.models import Document, DocumentChunk
from apps.tenants.models import Tenant, User

SAMPLE_NOTE = """# Daily Note 2026-03-02

## Morning

Worked on the NBHD United platform. Fixed the timezone resync issue that was
affecting all 15 tenant containers. The root cause was that the resync endpoint
only queried ACTIVE tenants, missing 10 that were in other states.

## Afternoon

Started building the proactive extraction system. The key insight is that
extraction should happen end-of-day, not per-message. This keeps noise low
and gives the LLM full context from the entire day.

## Evening

Reviewed the extraction prompt with MJ. He approved the approach after
seeing the dedup logic and goal document append pattern. Pushed to a
feature branch for CI validation.

## Lessons

QStash retries indefinitely on 5xx responses. Always return 200 for
background tasks that might fail due to missing resources.
"""

SHORT_NOTE = "Just a quick note."

LONG_SECTION = (
    """## Research

"""
    + "This is a very long paragraph about research. " * 100
    + """

Another paragraph about different research topics that should be split into a separate chunk because the first one is too long.

"""
    + "More detailed analysis follows. " * 80
)


class TestChunkMarkdown(TestCase):
    def test_splits_by_headings(self):
        chunks = chunk_markdown(SAMPLE_NOTE)
        self.assertGreaterEqual(len(chunks), 3)
        # Each chunk should contain section content
        all_text = " ".join(chunks)
        self.assertIn("timezone resync", all_text)
        self.assertIn("proactive extraction", all_text)

    def test_empty_note(self):
        self.assertEqual(chunk_markdown(""), [])
        self.assertEqual(chunk_markdown("   "), [])
        self.assertEqual(chunk_markdown(None), [])

    def test_short_note_skipped(self):
        chunks = chunk_markdown(SHORT_NOTE)
        self.assertEqual(len(chunks), 0)  # below MIN_CHUNK_CHARS

    def test_long_section_split_on_paragraphs(self):
        chunks = chunk_markdown(LONG_SECTION)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 5000)  # sections may exceed MAX_CHUNK_CHARS slightly

    def test_preserves_heading_with_section(self):
        chunks = chunk_markdown(SAMPLE_NOTE)
        # At least one chunk should start with ##
        has_heading = any(c.startswith("##") for c in chunks)
        self.assertTrue(has_heading)


def _make_tenant():
    user = User.objects.create_user(username="embedtest", password="pass")
    return Tenant.objects.create(user=user, status=Tenant.Status.ACTIVE)


FAKE_EMBEDDING = [0.1] * 1536


class TestEmbedDailyNote(TestCase):
    def setUp(self):
        self.tenant = _make_tenant()
        self.doc = Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.DAILY,
            slug=str(date.today()),
            title="Today",
            markdown=SAMPLE_NOTE,
        )

    @patch("apps.lessons.services.generate_embedding", return_value=FAKE_EMBEDDING)
    def test_creates_chunks(self, mock_embed):
        count = embed_daily_note(self.tenant, date.today())
        self.assertGreater(count, 0)
        self.assertEqual(DocumentChunk.objects.filter(tenant=self.tenant).count(), count)
        # Each chunk should be prefixed with date
        for chunk in DocumentChunk.objects.filter(tenant=self.tenant):
            self.assertTrue(chunk.text.startswith(f"[{date.today()}]"))

    @patch("apps.lessons.services.generate_embedding", return_value=FAKE_EMBEDDING)
    def test_idempotent_reembedding(self, mock_embed):
        count1 = embed_daily_note(self.tenant, date.today())
        count2 = embed_daily_note(self.tenant, date.today())
        self.assertEqual(count1, count2)
        self.assertEqual(DocumentChunk.objects.filter(tenant=self.tenant).count(), count2)

    def test_no_doc_returns_zero(self):
        count = embed_daily_note(self.tenant, date(2020, 1, 1))
        self.assertEqual(count, 0)

    @patch("apps.lessons.services.generate_embedding", side_effect=Exception("API down"))
    def test_embedding_failure_skips_chunk(self, mock_embed):
        count = embed_daily_note(self.tenant, date.today())
        self.assertEqual(count, 0)  # all chunks failed
        self.assertEqual(DocumentChunk.objects.filter(tenant=self.tenant).count(), 0)
