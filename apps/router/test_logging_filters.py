"""Tests for the Telegram bot-token log redaction filter.

The leak these guard against: httpx logs every outbound request at INFO with
the URL — and the Telegram Bot API puts the token in the URL path — so without
this filter the shared bot token streams into Log Analytics on every call.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import httpx
from django.test import SimpleTestCase

from apps.router.logging_filters import RedactTelegramToken, scrub_telegram_token

# Obviously-fake token in the classic Telegram shape: <id>:<35 url-safe chars>.
_FAKE_ID = "123456789"
_FAKE_SECRET = "AAH_Fake-Token_value_000111222333444"  # noqa: S105 (not a real secret)
_FAKE_TOKEN = f"{_FAKE_ID}:{_FAKE_SECRET}"


def _record(msg, args=()):
    """Build a LogRecord the way the logging machinery does, so getMessage()
    behaves exactly as it will in production."""
    return logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=None,
    )


class ScrubHelperTests(SimpleTestCase):
    def test_redacts_secret_keeps_id(self):
        url = f"https://api.telegram.org/bot{_FAKE_TOKEN}/getUpdates"
        out = scrub_telegram_token(url)
        self.assertNotIn(_FAKE_SECRET, out)
        self.assertIn(f"{_FAKE_ID}:[REDACTED]", out)
        # Surrounding URL structure is preserved for debuggability.
        self.assertEqual(out, f"https://api.telegram.org/bot{_FAKE_ID}:[REDACTED]/getUpdates")

    def test_redacts_bare_token(self):
        self.assertEqual(scrub_telegram_token(_FAKE_TOKEN), f"{_FAKE_ID}:[REDACTED]")

    def test_redacts_file_download_url(self):
        url = f"https://api.telegram.org/file/bot{_FAKE_TOKEN}/photos/file_1.jpg"
        out = scrub_telegram_token(url)
        self.assertNotIn(_FAKE_SECRET, out)
        self.assertIn(f"bot{_FAKE_ID}:[REDACTED]/photos/file_1.jpg", out)

    def test_leaves_innocent_text_untouched(self):
        for text in (
            'HTTP Request: GET https://openrouter.ai/api/v1/chat "HTTP/1.1 200 OK"',
            "sendMessage failed (429): too many requests",
            "tenant 148ccf1c-ef13-47f8-a woke at 12:34:56",  # colons, but no token shape
            "id=123456 status=ok",  # digits + colon-free
        ):
            self.assertEqual(scrub_telegram_token(text), text)


class RedactTelegramTokenFilterTests(SimpleTestCase):
    def setUp(self):
        self.filter = RedactTelegramToken()

    def test_scrubs_httpx_record_with_url_object_in_args(self):
        # This is the exact shape httpx emits: the URL rides in args as a
        # httpx.URL object, NOT a string — the crux of the leak. A naive
        # isinstance(arg, str) filter would miss it.
        url = httpx.URL(f"https://api.telegram.org/bot{_FAKE_TOKEN}/getUpdates")
        record = _record(
            'HTTP Request: %s %s "%s %d %s"',
            ("GET", url, "HTTP/1.1", 200, "OK"),
        )
        self.assertTrue(self.filter.filter(record))

        rendered = record.getMessage()
        self.assertNotIn(_FAKE_SECRET, rendered)
        self.assertIn(f"{_FAKE_ID}:[REDACTED]", rendered)
        # The non-secret parts of the log line survive.
        self.assertIn("HTTP Request: GET", rendered)
        self.assertIn("/getUpdates", rendered)
        self.assertIn('"HTTP/1.1 200 OK"', rendered)
        # args were collapsed into the redacted message.
        self.assertEqual(record.args, ())

    def test_scrubs_token_embedded_directly_in_msg(self):
        record = _record(f"downloading https://api.telegram.org/file/bot{_FAKE_TOKEN}/x.jpg")
        self.assertTrue(self.filter.filter(record))
        rendered = record.getMessage()
        self.assertNotIn(_FAKE_SECRET, rendered)
        self.assertIn(f"bot{_FAKE_ID}:[REDACTED]/x.jpg", rendered)

    def test_scrubs_bare_token_passed_as_arg(self):
        record = _record("token configured: %s", (_FAKE_TOKEN,))
        self.assertTrue(self.filter.filter(record))
        rendered = record.getMessage()
        self.assertNotIn(_FAKE_SECRET, rendered)
        self.assertIn(f"{_FAKE_ID}:[REDACTED]", rendered)

    def test_innocent_record_passes_through_unmodified(self):
        # A non-token record must keep BOTH its template and its args intact,
        # so lazy %-formatting and downstream structured logging are unaffected.
        record = _record("sendMessage failed (%s): %s", (429, "rate limited"))
        self.assertTrue(self.filter.filter(record))
        self.assertEqual(record.msg, "sendMessage failed (%s): %s")
        self.assertEqual(record.args, (429, "rate limited"))
        self.assertEqual(record.getMessage(), "sendMessage failed (429): rate limited")

    def test_filter_never_drops_records(self):
        # Even a record whose args would raise on formatting must survive.
        record = _record("bad format %d", ("not-an-int",))
        self.assertTrue(self.filter.filter(record))

    def test_httpx_url_arg_scrubbed_and_innocent_arg_record_untouched(self):
        # Regression guard for the two behaviours that matter most, asserted
        # side by side: (1) the httpx.URL-in-args leak shape is scrubbed and its
        # args collapsed; (2) an innocent record with args is left completely
        # untouched — template AND args survive for lazy %-formatting.
        leaky = _record(
            "HTTP Request: %s %s",
            ("GET", httpx.URL(f"https://api.telegram.org/bot{_FAKE_TOKEN}/getUpdates")),
        )
        self.assertTrue(self.filter.filter(leaky))
        self.assertNotIn(_FAKE_SECRET, leaky.getMessage())
        self.assertIn(f"{_FAKE_ID}:[REDACTED]", leaky.getMessage())
        self.assertEqual(leaky.args, ())

        innocent = _record("HTTP Request: %s %s", ("GET", "https://openrouter.ai/api/v1/chat"))
        self.assertTrue(self.filter.filter(innocent))
        self.assertEqual(innocent.msg, "HTTP Request: %s %s")
        self.assertEqual(innocent.args, ("GET", "https://openrouter.ai/api/v1/chat"))


class ProductionLoggingConfigTests(SimpleTestCase):
    """The filter only closes the leak if production actually wires it onto the
    console handler. base.py defines no LOGGING dict — production.py owns it — so
    we read the LOGGING structure straight from the production settings *source*
    via ast rather than importing the module (importing it would mutate the
    shared DATABASES dict and read deploy-only env vars)."""

    def _production_logging(self) -> dict:
        src = Path(__file__).resolve().parents[2] / "config" / "settings" / "production.py"
        tree = ast.parse(src.read_text())
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "LOGGING" for target in node.targets
            ):
                return ast.literal_eval(node.value)
        self.fail("No module-level LOGGING dict found in config/settings/production.py")

    def test_console_handler_attaches_telegram_filter(self):
        logging_cfg = self._production_logging()

        filters = logging_cfg["filters"]
        self.assertIn("redact_telegram_token", filters)
        self.assertEqual(
            filters["redact_telegram_token"]["()"],
            "apps.router.logging_filters.RedactTelegramToken",
        )

        console_filters = logging_cfg["handlers"]["console"]["filters"]
        self.assertIn("redact_telegram_token", console_filters)
        # We add, not replace: the pre-existing BYO filter must stay wired.
        self.assertIn("redact_byo_paste_body", console_filters)
