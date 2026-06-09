"""Tests for the markdown → Telegram-HTML renderer.

The invariants that matter most (a broken tag makes Telegram 400 and leak raw
markdown to the user):

  * Output contains ONLY Telegram-supported tags, balanced & nested.
  * No raw markdown syntax (``##``, ``---``, ``**``, ``|---|``) survives.
  * The plaintext fallback is also markdown-free.
"""

from __future__ import annotations

import re

from django.test import SimpleTestCase

from apps.router.telegram_format import (
    _is_valid_html,
    markdown_to_plaintext,
    render_telegram_html,
)

_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)(\s[^>]*)?>")
_ALLOWED = {"b", "i", "s", "u", "code", "pre", "a", "blockquote"}


def _joined(md: str) -> str:
    return "\n\n".join(render_telegram_html(md))


def _assert_valid(self, md: str) -> str:
    """Render and assert every chunk is valid Telegram HTML."""
    chunks = render_telegram_html(md)
    for c in chunks:
        self.assertTrue(_is_valid_html(c), f"invalid HTML produced: {c!r}")
        for m in _TAG_RE.finditer(c):
            self.assertIn(m.group(2).lower(), _ALLOWED, f"unexpected tag {m.group(0)!r}")
    return "\n\n".join(chunks)


class HeadingsAndRulesTests(SimpleTestCase):
    def test_headings_become_bold_no_hash(self):
        out = _assert_valid(self, "## Django Deep Dives\n\nReading list")
        self.assertIn("<b>Django Deep Dives</b>", out)
        self.assertNotIn("##", out)
        self.assertNotIn("#", out)

    def test_all_heading_levels(self):
        for lvl in range(1, 7):
            md = ("#" * lvl) + " Title"
            out = _assert_valid(self, md)
            self.assertIn("<b>Title</b>", out)

    def test_horizontal_rule_no_dashes(self):
        out = _assert_valid(self, "Above\n\n---\n\nBelow")
        self.assertNotIn("---", out)
        self.assertIn("──────────", out)

    def test_rule_variants(self):
        for rule in ("---", "***", "___", "- - -", "* * *"):
            out = _assert_valid(self, f"a\n\n{rule}\n\nb")
            self.assertNotIn(rule, out)


class InlineTests(SimpleTestCase):
    def test_double_star_bold(self):
        out = _assert_valid(self, "This is **bold** text")
        self.assertIn("<b>bold</b>", out)
        self.assertNotIn("**", out)

    def test_single_star_italic(self):
        out = _assert_valid(self, "This is *italic* text")
        self.assertIn("<i>italic</i>", out)

    def test_underscore_emphasis(self):
        out = _assert_valid(self, "__strong__ and _slanted_")
        self.assertIn("<b>strong</b>", out)
        self.assertIn("<i>slanted</i>", out)

    def test_strikethrough(self):
        out = _assert_valid(self, "~~gone~~")
        self.assertIn("<s>gone</s>", out)

    def test_inline_code(self):
        out = _assert_valid(self, "Run `make test` now")
        self.assertIn("<code>make test</code>", out)

    def test_link(self):
        out = _assert_valid(self, "See [the docs](https://example.com/x?a=1&b=2)")
        self.assertIn('<a href="https://example.com/x?a=1&amp;b=2">the docs</a>', out)

    def test_bold_inside_link_label(self):
        out = _assert_valid(self, "[**important** link](https://x.com)")
        self.assertIn("<b>important</b>", out)
        self.assertIn('href="https://x.com"', out)

    def test_emphasis_not_applied_inside_code(self):
        out = _assert_valid(self, "`a * b * c`")
        self.assertIn("<code>a * b * c</code>", out)
        self.assertNotIn("<i>", out)

    def test_arithmetic_asterisk_not_italic(self):
        out = _assert_valid(self, "The product 2 * 3 * 4 = 24")
        self.assertNotIn("<i>", out)
        self.assertIn("2 * 3 * 4", out)

    def test_html_special_chars_escaped(self):
        out = _assert_valid(self, "if x < 5 && y > 3 then a & b")
        self.assertIn("&lt;", out)
        self.assertIn("&gt;", out)
        self.assertIn("&amp;", out)
        self.assertNotIn("<5", out)


class ListTests(SimpleTestCase):
    def test_bullets(self):
        out = _assert_valid(self, "- one\n- two\n- three")
        self.assertEqual(out.count("•"), 3)
        self.assertNotIn("- one", out)

    def test_star_bullets(self):
        out = _assert_valid(self, "* alpha\n* beta")
        self.assertEqual(out.count("•"), 2)

    def test_ordered(self):
        out = _assert_valid(self, "1. first\n2. second")
        self.assertIn("1. first", out)
        self.assertIn("2. second", out)

    def test_nested_bullets(self):
        out = _assert_valid(self, "- parent\n  - child")
        self.assertIn("•", out)
        self.assertIn("◦", out)

    def test_task_list(self):
        out = _assert_valid(self, "- [x] done\n- [ ] todo")
        self.assertIn("☑", out)
        self.assertIn("☐", out)
        self.assertNotIn("[x]", out)
        self.assertNotIn("[ ]", out)


class CodeBlockTests(SimpleTestCase):
    def test_fenced_code(self):
        out = _assert_valid(self, "```python\nprint('hi')\n```")
        self.assertIn('<pre><code class="language-python">', out)
        self.assertIn("print(&#x27;hi&#x27;)" if "&#x27;" in out else "print('hi')", out)

    def test_fenced_code_with_html_chars(self):
        out = _assert_valid(self, "```\nif a < b and c > d:\n    pass\n```")
        self.assertIn("&lt;", out)
        self.assertIn("&gt;", out)
        self.assertNotIn("<b", out.replace("<b>", ""))  # no stray tag from < b

    def test_fenced_no_lang(self):
        out = _assert_valid(self, "```\nplain code\n```")
        self.assertIn("<pre><code>", out)


class TableTests(SimpleTestCase):
    def test_basic_table_renders_aligned_pre(self):
        md = (
            "| Exercise | Sets | Rest |\n"
            "|----------|------|------|\n"
            "| Pull-Ups | 4    | 90s  |\n"
            "| Squats   | 5    | 120s |\n"
        )
        out = _assert_valid(self, md)
        self.assertIn("<pre>", out)
        # No raw markdown table pipes-with-dashes separator survives.
        self.assertNotIn("|----------|", out)
        self.assertNotIn("|------|", out)
        # Header and data present.
        self.assertIn("Exercise", out)
        self.assertIn("Pull-Ups", out)
        # Aligned grid uses box-drawing separators.
        self.assertIn("│", out)
        self.assertIn("┼", out)

    def test_table_columns_aligned(self):
        md = "| a | bbbb |\n|---|------|\n| ccc | d |\n"
        out = _joined(md)
        # Extract the <pre> body and confirm each line has the separator at a
        # consistent column (i.e. real alignment).
        body = re.search(r"<pre>(.*)</pre>", out, re.DOTALL).group(1)
        lines = [ln for ln in body.split("\n") if "│" in ln]
        positions = {ln.index("│") for ln in lines}
        self.assertEqual(len(positions), 1, f"columns not aligned: {lines!r}")

    def test_table_with_markdown_in_cells(self):
        md = "| Name | Note |\n|------|------|\n| **Bob** | see `x` |\n"
        out = _assert_valid(self, md)
        # Inline markdown inside cells is stripped (monospace grid), not leaked.
        self.assertNotIn("**", out)
        self.assertNotIn("`", out)
        self.assertIn("Bob", out)


class StudyKitRegressionTests(SimpleTestCase):
    """The exact failure shape from the goal image."""

    MD = (
        "Here's your study kit:\n\n"
        "---\n\n"
        "## Django Deep Dives\n\n"
        "Reading:\n"
        "- [Django documentation → Migrations](https://docs.djangoproject.com/migrations) "
        "— often overlooked, always tested\n"
        "- [Django ORM optimization tricks](https://example.com/orm) — N+1 problems\n\n"
        "Video:\n"
        '- [DjangoCon US talks](https://youtube.com/x) — search "ORM"\n\n'
        "---\n\n"
        "## Python Backend Reading\n\n"
        "Reading:\n"
        "- [Real Python — Async IO explained](https://realpython.com/async)\n"
    )

    def test_no_visible_markdown(self):
        out = _assert_valid(self, self.MD)
        self.assertNotIn("##", out)
        self.assertNotIn("---", out)
        # Links became anchors, headings became bold.
        self.assertIn("<b>Django Deep Dives</b>", out)
        self.assertIn("<b>Python Backend Reading</b>", out)
        self.assertIn('<a href="https://docs.djangoproject.com/migrations">', out)
        self.assertIn("•", out)
        self.assertIn("──────────", out)


class AdversarialTests(SimpleTestCase):
    """Inputs designed to produce malformed HTML if the renderer is naive."""

    CASES = [
        "**unbalanced bold",
        "*lone asterisk and **mixed** stuff*",
        "**a *b** c*",  # overlapping emphasis
        "text with <script>alert(1)</script> injection",
        "a < b > c & d",
        "[broken link](no-close",
        "```\nunclosed code fence\n",
        "| a | b |\n|---|---|\n| only one cell\n",
        "####### too many hashes",
        "> quote with **bold** and <tag>",
        "_ leading underscore not italic _",
        "100% * 50% calculation",
        "nested `code with **bold** inside`",
        "[a](b)[c](d)[e](f) many links",
        "1. ordered\n2. with **bold**\n3. and `code`",
        "🔴 emoji heading\n## real heading",
        "\n\n\n   \n\n",  # all whitespace
        "a" * 9000,  # very long single paragraph
        "| h |\n|---|\n" + "\n".join(f"| row {n} |" for n in range(500)),  # huge table
    ]

    def test_all_cases_produce_valid_html(self):
        for md in self.CASES:
            chunks = render_telegram_html(md)
            for c in chunks:
                self.assertTrue(
                    _is_valid_html(c),
                    f"INVALID HTML for input {md[:40]!r}: produced {c[:120]!r}",
                )
                self.assertLessEqual(len(c), 4096, f"chunk over limit for {md[:40]!r}")
                for m in _TAG_RE.finditer(c):
                    self.assertIn(
                        m.group(2).lower(),
                        _ALLOWED,
                        f"unexpected tag {m.group(0)!r} for {md[:40]!r}",
                    )

    def test_script_injection_neutralised(self):
        out = _joined("text with <script>alert(1)</script>")
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)


class PlaintextFallbackTests(SimpleTestCase):
    def test_plaintext_has_no_markdown(self):
        md = (
            "## Heading\n\n"
            "Some **bold** and *italic* and `code`.\n\n"
            "- bullet one\n- bullet two\n\n"
            "| A | B |\n|---|---|\n| 1 | 2 |\n\n"
            "---\n\n"
            "[link](https://x.com)\n"
        )
        out = markdown_to_plaintext(md)
        for token in ("##", "**", "`", "|---|", "](http"):
            self.assertNotIn(token, out)
        self.assertIn("Heading", out)
        self.assertIn("bold", out)
        self.assertIn("•", out)

    def test_plaintext_empty(self):
        self.assertEqual(markdown_to_plaintext(""), "")
        self.assertEqual(markdown_to_plaintext("   \n\n  "), "")


class ChunkingTests(SimpleTestCase):
    def test_long_text_split_under_limit(self):
        md = "\n\n".join(f"Paragraph {n} with some words here." for n in range(1000))
        chunks = render_telegram_html(md)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c), 4096)
            self.assertTrue(_is_valid_html(c))

    def test_empty_returns_empty_list(self):
        self.assertEqual(render_telegram_html(""), [])
        self.assertEqual(render_telegram_html("   "), [])


class FuzzRegressionTests(SimpleTestCase):
    """Locked-in cases surfaced by the adversarial fuzz pass."""

    def test_unclosed_bold_does_not_leak(self):
        out = _assert_valid(self, "This is **bold but never closed at all")
        self.assertNotIn("**", out)
        self.assertIn("bold but never closed", out)

    def test_quadruple_star_no_leak(self):
        out = _assert_valid(self, "****Net Worth**** climbed to $52,310")
        self.assertNotIn("**", out)
        self.assertIn("Net Worth", out)

    def test_lone_trailing_stars(self):
        for md in ("Monthly fee **", "Total $42 ~~", "**Summary:\nTwo lines**"):
            out = _assert_valid(self, md)
            self.assertNotIn("**", out)

    def test_power_operator_in_code_keeps_stars(self):
        # ** inside a code span is literal and must be preserved.
        out = _assert_valid(self, "Use `base ** exponent` for powers")
        self.assertIn("<code>base ** exponent</code>", out)

    def test_power_operator_in_fence_keeps_stars(self):
        out = _assert_valid(self, "```python\nx = a ** b\nd = {**m, **n}\n```")
        self.assertIn("a ** b", out)  # literal inside <pre>
        self.assertIn("<pre>", out)

    def test_malformed_link_space_url_degrades_to_label(self):
        out = _assert_valid(self, "See [the guide](https://ex.com/a b c) here")
        self.assertNotIn("](http", out)
        self.assertIn("the guide", out)

    def test_unclosed_link_paren_degrades(self):
        out = _assert_valid(self, "Watch [demo](https://x.com/v and more")
        self.assertNotIn("](http", out)
        self.assertIn("demo", out)

    def test_bold_inside_unclosed_link_label(self):
        out = _assert_valid(self, "[**unclosed label](https://example.com/path)")
        self.assertNotIn("**", out)
        self.assertIn("unclosed label", out)

    def test_hashes_only_line_dropped(self):
        out = _assert_valid(self, "######\n\n**Total:** $42")
        self.assertNotIn("#", out)
        self.assertIn("<b>Total:</b>", out)

    def test_seven_hashes_still_heading(self):
        out = _assert_valid(self, "####### Deep Heading")
        self.assertNotIn("#", out)
        self.assertIn("<b>Deep Heading</b>", out)

    def test_orphan_table_separator_dropped(self):
        out = "\n\n".join(render_telegram_html("|---|---|---|"))
        self.assertNotIn("---", out)

    def test_markdown_inside_code_fence_is_literal(self):
        md = "```markdown\n## Not a heading\n**not bold**\n[x](http://y)\n```"
        out = _assert_valid(self, md)
        # All of it lives inside <pre> as literal example text.
        self.assertIn("<pre>", out)
        self.assertIn("## Not a heading", out)

    def test_huge_table_splits_into_valid_pre_chunks(self):
        rows = "\n".join(f"| item {n} | {'x' * 80} |" for n in range(400))
        md = f"| A | B |\n|---|---|\n{rows}\n"
        chunks = render_telegram_html(md)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c), 4096)
            self.assertTrue(_is_valid_html(c), f"invalid pre chunk: {c[:80]!r}")

    def test_huge_code_fence_splits_with_balanced_tags(self):
        body = "\n".join(f"line_{n} = compute(value_{n})  # comment {n}" for n in range(400))
        md = f"```python\n{body}\n```"
        chunks = render_telegram_html(md)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c), 4096)
            self.assertTrue(_is_valid_html(c), f"unbalanced pre/code chunk: {c[:80]!r}")

    def test_nul_placeholder_injection_no_crash(self):
        # A user typing the inline-stash sentinel must not crash or leak.
        out = _assert_valid(self, "user types \x000\x00 and \x0099\x00 sentinels")
        self.assertNotIn("\x00", out)
        self.assertIn("sentinels", out)

    def test_nested_link_labels_no_leak(self):
        md = "- [outer [inner →](https://e.ex/i?a=1&b=2) tail](https://e.ex/o?c=3&d=4)"
        out = _assert_valid(self, md)
        self.assertNotIn("](http", out)
        self.assertIn("inner", out)
        self.assertIn("tail", out)

    def test_embedded_table_separator_in_prose_stripped(self):
        for md in (
            "Summary of legs |---|---| smashed it today",
            "bench | press |---|---| then rows",
            "weekly split:\n|:--|:--:|--:|\nfill in numbers",
        ):
            out = _assert_valid(self, md)
            self.assertNotIn("|---|", out)
            self.assertNotIn("|--:|", out)

    def test_separator_inside_blockquote_stripped(self):
        out = _assert_valid(self, "> fake table | --- | --- | inside a quote")
        self.assertNotIn("| --- |", out)
        self.assertIn("<blockquote>", out)

    def test_heading_marker_in_table_cell_stripped(self):
        md = "| Section | Exercise |\n|---|---|\n| ## A | Squat |\n| ## B | Bench |\n"
        out = _assert_valid(self, md)
        self.assertNotIn("## A", out)
        self.assertIn("Squat", out)
        # And the plaintext fallback is clean too.
        self.assertNotIn("## A", markdown_to_plaintext(md))

    def test_url_with_stars_is_not_a_leak(self):
        # ** inside a URL path is literal URL content, preserved in the href.
        out = _assert_valid(self, "See [the docs](https://x.io/page**1) now")
        self.assertIn('href="https://x.io/page**1"', out)
        self.assertIn(">the docs</a>", out)
