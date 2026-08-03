"""Tests for the journal deep-link marker parser.

``extract_journal_link`` is the shared helper every outbound-reply chokepoint
(iOS app, Telegram queue drain, Telegram poller, LINE, cron delivery) calls to
strip the agent's ``[[journal-link: kind|slug|title]]`` marker before a user
ever sees it. Only the iOS-facing paths keep the parsed link (see
test_ios_chat.py / test_cron_delivery.py); this module covers the
parsing/validation + rehydration contract in isolation.
"""

from __future__ import annotations

import logging

from django.test import SimpleTestCase, TestCase

from apps.router.journal_link import (
    MAX_TITLE_LEN,
    extract_journal_link,
    rehydrate_journal_link,
    strip_streaming_journal_link_marker,
)


class ExtractJournalLinkTest(SimpleTestCase):
    def test_no_marker_returns_text_unchanged(self):
        text, link = extract_journal_link("Just a normal reply.")
        self.assertEqual(text, "Just a normal reply.")
        self.assertIsNone(link)

    def test_empty_text(self):
        text, link = extract_journal_link("")
        self.assertEqual(text, "")
        self.assertIsNone(link)

    def test_none_text(self):
        text, link = extract_journal_link(None)
        self.assertIsNone(text)
        self.assertIsNone(link)

    def test_valid_daily_link(self):
        text, link = extract_journal_link("Logged it.\n[[journal-link: daily|2026-07-13|Morning Report]]")
        self.assertEqual(text, "Logged it.")
        self.assertEqual(link, {"kind": "daily", "slug": "2026-07-13", "title": "Morning Report"})

    def test_valid_goal_link(self):
        text, link = extract_journal_link("Saved.\n[[journal-link: goal|debt-freedom|Become debt-free]]")
        self.assertEqual(link, {"kind": "goal", "slug": "debt-freedom", "title": "Become debt-free"})

    def test_all_document_kinds_accepted(self):
        from apps.journal.models import Document

        for kind in Document.Kind.values:
            _, link = extract_journal_link(f"x\n[[journal-link: {kind}|some-slug|Title]]")
            self.assertIsNotNone(link, kind)
            self.assertEqual(link["kind"], kind)

    def test_fields_trimmed(self):
        _, link = extract_journal_link("x\n[[journal-link:  daily | 2026-07-13 |  Morning Report  ]]")
        self.assertEqual(link, {"kind": "daily", "slug": "2026-07-13", "title": "Morning Report"})

    def test_title_may_contain_pipe(self):
        # split on the first two pipes only, so a title with a "|" survives.
        _, link = extract_journal_link("x\n[[journal-link: daily|2026-07-13|Report | draft]]")
        self.assertEqual(link["title"], "Report | draft")

    def test_trailing_newlines_after_marker_ignored(self):
        text, link = extract_journal_link("x\n[[journal-link: daily|2026-07-13|Report]]\n\n")
        self.assertEqual(text, "x")
        self.assertEqual(link["slug"], "2026-07-13")

    def test_case_insensitive_marker(self):
        _, link = extract_journal_link("x\n[[JOURNAL-LINK: daily|2026-07-13|Report]]")
        self.assertEqual(link["kind"], "daily")

    def test_multiline_prose_before_marker_preserved(self):
        text, link = extract_journal_link("Line one.\nLine two.\n[[journal-link: daily|2026-07-13|Report]]")
        self.assertEqual(text, "Line one.\nLine two.")
        self.assertIsNotNone(link)

    def test_only_marker_yields_empty_remainder(self):
        text, link = extract_journal_link("[[journal-link: daily|2026-07-13|Report]]")
        self.assertEqual(text, "")
        self.assertEqual(link["slug"], "2026-07-13")

    # ── marker must be the LAST line ────────────────────────────────────────

    def test_marker_mid_text_is_not_parsed(self):
        original = "Before [[journal-link: daily|2026-07-13|Report]] and after."
        text, link = extract_journal_link(original)
        self.assertEqual(text, original)
        self.assertIsNone(link)

    def test_marker_followed_by_more_prose_is_not_parsed(self):
        original = "[[journal-link: daily|2026-07-13|Report]]\nOne more thing."
        text, link = extract_journal_link(original)
        self.assertEqual(text, original)
        self.assertIsNone(link)

    def test_marker_with_trailing_prose_on_same_line_not_parsed(self):
        original = "x\n[[journal-link: daily|2026-07-13|Report]] please"
        text, link = extract_journal_link(original)
        self.assertEqual(text, original)
        self.assertIsNone(link)

    # ── malformed marker: stripped anyway, no link, telemetry logged ────────

    def test_bad_kind_stripped_with_no_link(self):
        with self.assertLogs("apps.router.journal_link", level=logging.WARNING) as cm:
            text, link = extract_journal_link(
                "x\n[[journal-link: note|some-slug|Title]]", tenant_id="t-1", channel="ios"
            )
        self.assertEqual(text, "x")
        self.assertIsNone(link)
        self.assertIn("journal_link_marker_malformed", cm.output[0])
        self.assertEqual(cm.records[0].reason, "bad_kind")

    def test_wrong_field_count_stripped(self):
        with self.assertLogs("apps.router.journal_link", level=logging.WARNING) as cm:
            text, link = extract_journal_link("x\n[[journal-link: daily|2026-07-13]]")
        self.assertEqual(text, "x")
        self.assertIsNone(link)
        self.assertEqual(cm.records[0].reason, "field_count")

    def test_empty_slug_stripped(self):
        with self.assertLogs("apps.router.journal_link", level=logging.WARNING) as cm:
            _, link = extract_journal_link("x\n[[journal-link: daily||Report]]")
        self.assertIsNone(link)
        self.assertEqual(cm.records[0].reason, "bad_slug")

    def test_slug_with_space_stripped(self):
        with self.assertLogs("apps.router.journal_link", level=logging.WARNING) as cm:
            _, link = extract_journal_link("x\n[[journal-link: daily|July 13th|Report]]")
        self.assertIsNone(link)
        self.assertEqual(cm.records[0].reason, "bad_slug")

    def test_slug_too_long_stripped(self):
        slug = "a" * 129
        with self.assertLogs("apps.router.journal_link", level=logging.WARNING):
            _, link = extract_journal_link(f"x\n[[journal-link: daily|{slug}|Report]]")
        self.assertIsNone(link)

    def test_empty_title_stripped(self):
        with self.assertLogs("apps.router.journal_link", level=logging.WARNING) as cm:
            _, link = extract_journal_link("x\n[[journal-link: daily|2026-07-13|]]")
        self.assertIsNone(link)
        self.assertEqual(cm.records[0].reason, "bad_title")

    def test_title_at_cap_is_valid(self):
        title = "t" * MAX_TITLE_LEN
        _, link = extract_journal_link(f"x\n[[journal-link: daily|2026-07-13|{title}]]")
        self.assertEqual(link["title"], title)

    def test_title_over_cap_stripped(self):
        title = "t" * (MAX_TITLE_LEN + 1)
        with self.assertLogs("apps.router.journal_link", level=logging.WARNING) as cm:
            text, link = extract_journal_link(f"x\n[[journal-link: daily|2026-07-13|{title}]]")
        self.assertEqual(text, "x")
        self.assertIsNone(link)
        self.assertEqual(cm.records[0].reason, "bad_title")

    def test_telemetry_includes_tenant_and_channel(self):
        with self.assertLogs("apps.router.journal_link", level=logging.WARNING) as cm:
            extract_journal_link("x\n[[journal-link: note|s|T]]", tenant_id="tenant-abc", channel="telegram_drain")
        record = cm.records[0]
        self.assertEqual(record.tenant_id, "tenant-abc")
        self.assertEqual(record.channel, "telegram_drain")


class JournalLinkExistenceTest(TestCase):
    def setUp(self):
        from apps.tenants.models import Tenant, User

        user = User.objects.create_user(username="journal-link-existence", password="pass")
        self.tenant = Tenant.objects.create(user=user, status="active")

    def test_existing_document_emits_link_with_one_lookup(self):
        from apps.journal.models import Document

        Document.objects.create(
            tenant=self.tenant,
            kind="weekly",
            slug="2026-W28",
            title="Week 28",
            markdown="# Week 28",
        )

        with self.assertNumQueries(1):
            text, link = extract_journal_link(
                "Saved.\n[[journal-link: weekly|2026-W28|Week 28]]",
                tenant_id=self.tenant.id,
                channel="ios",
            )

        self.assertEqual(text, "Saved.")
        self.assertEqual(
            link,
            {"kind": "weekly", "slug": "2026-W28", "title": "Week 28"},
        )

    def test_missing_document_drops_link_and_preserves_text(self):
        with (
            self.assertLogs("apps.router.journal_link", level=logging.WARNING) as cm,
            self.assertNumQueries(1),
        ):
            text, link = extract_journal_link(
                "Still saved the reply text.\n[[journal-link: weekly|2026-W99|Missing]]",
                tenant_id=self.tenant.id,
                channel="ios",
            )

        self.assertEqual(text, "Still saved the reply text.")
        self.assertIsNone(link)
        self.assertEqual(cm.records[0].reason, "missing_document")

    def test_reply_without_marker_does_not_query(self):
        with self.assertNumQueries(0):
            text, link = extract_journal_link(
                "No marker here.",
                tenant_id=self.tenant.id,
                channel="ios",
            )
        self.assertEqual(text, "No marker here.")
        self.assertIsNone(link)


class RehydrateJournalLinkTest(SimpleTestCase):
    def test_none_link(self):
        self.assertIsNone(rehydrate_journal_link(None, {"[PERSON_1]": "Alice"}))

    def test_no_entity_map_returns_unchanged(self):
        link = {"kind": "goal", "slug": "g1", "title": "Call [PERSON_1]"}
        self.assertEqual(rehydrate_journal_link(link, None), link)

    def test_title_rehydrated(self):
        link = {"kind": "goal", "slug": "g1", "title": "Reconnect with [PERSON_1]"}
        out = rehydrate_journal_link(link, {"[PERSON_1]": "Alice"})
        self.assertEqual(out["title"], "Reconnect with Alice")

    def test_kind_and_slug_not_rehydrated(self):
        # kind is an enum and slug is slugified/an ISO date — neither is touched.
        link = {"kind": "goal", "slug": "g1", "title": "Plain"}
        out = rehydrate_journal_link(link, {"g1": "SHOULD-NOT-APPEAR"})
        self.assertEqual(out["kind"], "goal")
        self.assertEqual(out["slug"], "g1")

    def test_rehydration_overflow_truncates_title_but_keeps_link(self):
        # A short placeholder-space title can overflow once the real name lands.
        # Unlike quick-replies (whole set dropped), the chip title is display-
        # only — the link stays tappable via kind+slug, title is truncated.
        long_name = "A Very Long Full Legal Name That Definitely Blows Past Eighty Characters When Substituted In"
        link = {"kind": "goal", "slug": "g1", "title": "Call [PERSON_1]"}
        with self.assertLogs("apps.router.journal_link", level=logging.WARNING) as cm:
            out = rehydrate_journal_link(link, {"[PERSON_1]": long_name}, tenant_id="t", channel="ios_feed")
        self.assertEqual(out["kind"], "goal")
        self.assertEqual(out["slug"], "g1")
        self.assertLessEqual(len(out["title"]), MAX_TITLE_LEN)
        overflow = [r for r in cm.records if getattr(r, "reason", None) == "rehydration_overflow"]
        self.assertTrue(overflow)
        # telemetry sample stays placeholder-space (no real name in logs).
        self.assertNotIn(long_name, overflow[0].sample)
        self.assertIn("[PERSON_1]", overflow[0].sample)


class StripStreamingJournalLinkMarkerTest(SimpleTestCase):
    def test_complete_marker_stripped(self):
        text = "Here it is.\n[[journal-link: daily|2026-07-13|Report]]"
        self.assertEqual(strip_streaming_journal_link_marker(text), "Here it is.")

    def test_unclosed_marker_stripped(self):
        text = "Here it is.\n[[journal-link: daily|2026-07-13"
        self.assertEqual(strip_streaming_journal_link_marker(text), "Here it is.")

    def test_short_opener_fragment_stripped(self):
        self.assertEqual(strip_streaming_journal_link_marker("Done.\n[[j"), "Done.")

    def test_bare_double_bracket_not_stripped(self):
        # Ambiguous (< 3 chars) — could be any marker; leave it.
        self.assertEqual(strip_streaming_journal_link_marker("Done. [["), "Done. [[")

    def test_other_marker_not_stripped(self):
        # A [[chart: opener is not ours — leave it for its own handler.
        self.assertEqual(strip_streaming_journal_link_marker("Done.\n[[chart:foo]]"), "Done.\n[[chart:foo]]")

    def test_no_marker_unchanged(self):
        self.assertEqual(strip_streaming_journal_link_marker("Just text."), "Just text.")

    def test_empty(self):
        self.assertEqual(strip_streaming_journal_link_marker(""), "")
