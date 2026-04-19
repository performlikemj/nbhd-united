"""Dashboard view tests — currently scoped to the Horizons Weekly Pulse helpers."""

from __future__ import annotations

from datetime import date

from django.test import SimpleTestCase

from apps.dashboard.views import _clean_markdown_preview, _derive_week_bounds


class CleanMarkdownPreviewTests(SimpleTestCase):
    def test_strips_headings_bold_and_lists(self):
        md = (
            "# Weekly Review — 2026-W14\n"
            "*Week of April 6–12, 2026*\n\n"
            "## 🏆 Wins\n"
            "- Shipped **billing** fix\n"
            "- Landed *reflection* gate\n"
        )
        out = _clean_markdown_preview(md)
        self.assertNotIn("#", out)
        self.assertNotIn("**", out)
        self.assertNotIn("- ", out)
        self.assertIn("Shipped billing fix", out)
        self.assertIn("Landed reflection gate", out)

    def test_drops_links_keeps_visible_text(self):
        md = "See [the doc](https://example.com/thing) for details."
        self.assertEqual(
            _clean_markdown_preview(md),
            "See the doc for details.",
        )

    def test_strips_inline_code_and_blockquotes(self):
        md = "> quoted line\n`inline` snippet here"
        out = _clean_markdown_preview(md)
        self.assertEqual(out, "quoted line inline snippet here")

    def test_empty_input_returns_empty(self):
        self.assertEqual(_clean_markdown_preview(""), "")
        self.assertEqual(_clean_markdown_preview(None), "")  # type: ignore[arg-type]

    def test_truncates_with_ellipsis(self):
        md = "a " * 200
        out = _clean_markdown_preview(md, max_chars=50)
        self.assertLessEqual(len(out), 50)
        self.assertTrue(out.endswith("\u2026"))

    def test_preserves_underscore_in_identifiers(self):
        # A bare `some_var` should not be treated as italic
        md = "Look at some_var and another_name in the logs."
        out = _clean_markdown_preview(md)
        self.assertIn("some_var", out)
        self.assertIn("another_name", out)


class DeriveWeekBoundsTests(SimpleTestCase):
    def test_parses_iso_monday_slug(self):
        start, end = _derive_week_bounds("2026-04-06", date(2026, 4, 13))
        self.assertEqual(start, date(2026, 4, 6))
        self.assertEqual(end, date(2026, 4, 12))

    def test_falls_back_to_week_of_fallback(self):
        # Wednesday 2026-04-15 → Monday is 2026-04-13
        start, end = _derive_week_bounds("not-a-date", date(2026, 4, 15))
        self.assertEqual(start, date(2026, 4, 13))
        self.assertEqual(end, date(2026, 4, 19))

    def test_invalid_month_falls_back(self):
        start, _ = _derive_week_bounds("2026-13-01", date(2026, 4, 20))
        self.assertEqual(start, date(2026, 4, 20))  # Monday
