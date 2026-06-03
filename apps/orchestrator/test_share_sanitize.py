"""Guard tests for the Azure File Share text-write corruption sanitizer.

Background: a tmp+rename race (2026-05-22) and carried-forward corrupted reads
left config/workspace files allocated-to-size but filled with trailing NUL
bytes (e.g. USER.md = 5K real content + 18.8K of \\x00). Every text write now
routes through ``_put_share_file`` -> ``sanitize_share_text`` so NUL/control
junk can never be (re)persisted. See apps/orchestrator/azure_client.py.
"""

from django.test import SimpleTestCase

from apps.orchestrator.azure_client import sanitize_share_text


class SanitizeShareTextTest(SimpleTestCase):
    def test_strips_embedded_null_bytes(self):
        self.assertEqual(sanitize_share_text("hello\x00\x00world"), "helloworld")

    def test_strips_trailing_null_run(self):
        # The observed corruption shape: real content + a long NUL tail.
        self.assertEqual(sanitize_share_text("openclaw config" + "\x00" * 18800), "openclaw config")

    def test_keeps_tab_newline_cr(self):
        s = "a\tb\nc\r\nd"
        self.assertEqual(sanitize_share_text(s), s)

    def test_strips_other_c0_controls(self):
        # All C0 controls except \t \n \r are stripped.
        self.assertEqual(sanitize_share_text("a\x01\x02\x08\x0b\x0c\x1fb"), "ab")

    def test_normal_markdown_and_json_unchanged(self):
        md = "# USER.md\n\n## Profile\n- name: example\n"
        self.assertEqual(sanitize_share_text(md), md)
        js = '{"agents": {"defaults": {"params": {"cacheRetention": "long"}}}}'
        self.assertEqual(sanitize_share_text(js), js)

    def test_empty_string_safe(self):
        self.assertEqual(sanitize_share_text(""), "")
