"""Tests for apps.crypto.audit — DecryptAudit stdout logger.

No DB — SimpleTestCase. Never touches decrypted content, so these tests
only need to prove: fires for admin/owner_request, silent for
system/system_cron/runtime_endpoint/default, and the emitted payload shape.
"""

from __future__ import annotations

import json
import logging

from django.test import SimpleTestCase

from apps.crypto import audit

LOGGER_NAME = "nbhd.decrypt_audit"


class _ListHandler(logging.Handler):
    """Collects log records without relying on assertLogs (which raises if none fire)."""

    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


class DecryptAuditTest(SimpleTestCase):
    def setUp(self):
        # `_PRINCIPAL` is a ContextVar that persists across tests within the
        # same thread/context — reset it so test order never matters.
        self._token = audit._PRINCIPAL.set("system")

    def tearDown(self):
        audit._PRINCIPAL.reset(self._token)

    def _capture(self, fn):
        handler = _ListHandler()
        logger = logging.getLogger(LOGGER_NAME)
        old_level = logger.level
        # `nbhd.decrypt_audit` has no explicit level configured anywhere, so
        # its EFFECTIVE level is inherited from root (WARNING by default) —
        # without lowering it here, `_AUDIT.info(...)` is filtered before it
        # ever reaches a handler, regardless of what's attached.
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        try:
            fn()
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)
        return handler.records

    def test_fires_for_admin(self):
        audit.set_principal("admin")
        records = self._capture(lambda: audit.emit("tenant-1", "app_chat_message", "user_text", row_count=1))
        self.assertEqual(len(records), 1)

    def test_fires_for_owner_request(self):
        audit.set_principal("owner_request")
        records = self._capture(lambda: audit.emit("tenant-1", "app_chat_message", "user_text", row_count=3))
        self.assertEqual(len(records), 1)

    def test_silent_for_system(self):
        audit.set_principal("system")
        records = self._capture(lambda: audit.emit("tenant-1", "t", "c", row_count=1))
        self.assertEqual(records, [])

    def test_silent_for_system_cron(self):
        audit.set_principal("system_cron")
        records = self._capture(lambda: audit.emit("tenant-1", "t", "c", row_count=1))
        self.assertEqual(records, [])

    def test_silent_for_runtime_endpoint(self):
        audit.set_principal("runtime_endpoint")
        records = self._capture(lambda: audit.emit("tenant-1", "t", "c", row_count=1))
        self.assertEqual(records, [])

    def test_default_principal_is_system_and_silent(self):
        # No set_principal() call at all — get_principal() must default to
        # "system", and emit() must stay silent.
        self.assertEqual(audit.get_principal(), "system")
        records = self._capture(lambda: audit.emit("tenant-1", "t", "c", row_count=1))
        self.assertEqual(records, [])

    def test_emitted_payload_shape(self):
        audit.set_principal("admin")
        records = self._capture(
            lambda: audit.emit("tenant-xyz", "app_chat_message", "user_text", row_count=5),
        )
        payload = json.loads(records[0].getMessage())
        self.assertEqual(payload["tenant_id"], "tenant-xyz")
        self.assertEqual(payload["table"], "app_chat_message")
        self.assertEqual(payload["column"], "user_text")
        self.assertEqual(payload["row_count"], 5)
        self.assertEqual(payload["principal"], "admin")
        self.assertIn("ts", payload)

    def test_set_principal_persists_until_changed(self):
        audit.set_principal("admin")
        self.assertEqual(audit.get_principal(), "admin")
        audit.set_principal("system")
        self.assertEqual(audit.get_principal(), "system")
