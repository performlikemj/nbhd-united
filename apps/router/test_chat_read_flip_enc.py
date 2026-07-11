"""Tests for the Phase 2 PR-4 chat read-flip (read_encrypted_chat).

When the flag is on, the egress seams serve chat content from the _enc column
(box.decrypt, dual-read with legacy fallback) and .reveal() it; when off, they
serve the legacy plaintext untouched. To prove WHICH column a seam reads, the
fixtures deliberately store a DIFFERENT value in _enc than in the legacy column
(real dual-write keeps them equal — here they diverge so the read routing is
observable). Uses the crypto _is_mock stateful mock.
"""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.crypto import audit, box
from apps.crypto.keys import mint_and_wrap_dek
from apps.router import enc_columns, enc_read
from apps.router.conversation_capture import _collect_turns
from apps.router.models import AppChatMessage, ChatThread
from apps.tenants.models import Tenant, User

_UT = enc_columns.APP_CHAT_MESSAGE_USER_TEXT
_TITLE = enc_columns.CHAT_THREAD_TITLE


class _AuditCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


@override_settings(NBHD_INTERNAL_API_KEY="test-key")
class ChatReadFlipTest(TestCase):
    def setUp(self):
        p = patch("apps.orchestrator.azure_client._is_mock", return_value=True)
        p.start()
        self.addCleanup(p.stop)

        self.user = User.objects.create_user(
            username=f"rf_{secrets.token_hex(4)}", email=f"{secrets.token_hex(4)}@e.com"
        )
        self.tenant = Tenant.objects.create(
            user=self.user, status=Tenant.Status.ACTIVE, container_fqdn="oc.example.com"
        )
        mint_and_wrap_dek(self.tenant)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _read_on(self):
        self.tenant.read_encrypted_chat = True
        self.tenant.save(update_fields=["read_encrypted_chat", "updated_at"])

    def _seal(self, aad, value):
        return box.encrypt(self.tenant.id, aad[0], aad[1], value)

    def _thread(self, *, legacy_title="X", enc_title=None, is_main=False):
        return ChatThread.objects.create(
            tenant=self.tenant,
            user=self.user,
            title=legacy_title,
            title_enc=self._seal(_TITLE, enc_title) if enc_title is not None else None,
            is_main=is_main,
        )

    def _msg(self, thread, *, cid, legacy="X", enc_value=None, enc_blob=None, status=AppChatMessage.Status.READY):
        if enc_value is not None:
            enc_blob = self._seal(_UT, enc_value)
        return AppChatMessage.objects.create(
            tenant=self.tenant,
            user=self.user,
            thread=thread,
            client_msg_id=cid,
            user_text=legacy,
            user_text_enc=enc_blob,
            status=status,
        )

    def _capture(self, fn):
        handler = _AuditCapture()
        lg = logging.getLogger("nbhd.decrypt_audit")
        old = lg.level
        lg.setLevel(logging.INFO)
        lg.addHandler(handler)
        try:
            fn()
        finally:
            lg.removeHandler(handler)
            lg.setLevel(old)
        return handler.records

    # ---------------- correctness: thread messages page (bulk) ----------------

    def test_thread_page_flag_off_serves_legacy(self):
        thread = self._thread(legacy_title="LegacyTitle", enc_title="EncTitle")
        self._msg(thread, cid="a", legacy="LegacyMsg", enc_value="EncMsg")
        resp = self.client.get(f"/api/v1/chat/threads/{thread.id}/messages/")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data["thread"]["title"], "LegacyTitle")
        self.assertEqual(resp.data["messages"][0]["user_text"], "LegacyMsg")

    def test_thread_page_flag_on_serves_decrypted(self):
        self._read_on()
        thread = self._thread(legacy_title="LegacyTitle", enc_title="EncTitle")
        self._msg(thread, cid="a", legacy="LegacyMsg", enc_value="EncMsg")
        resp = self.client.get(f"/api/v1/chat/threads/{thread.id}/messages/")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data["thread"]["title"], "EncTitle")
        self.assertEqual(resp.data["messages"][0]["user_text"], "EncMsg")

    def test_thread_page_mixed_falls_back_to_legacy(self):
        self._read_on()
        thread = self._thread(legacy_title="T", enc_title="T")
        self._msg(thread, cid="enc", legacy="X", enc_value="FROM_ENC")
        self._msg(thread, cid="leg", legacy="FROM_LEGACY", enc_blob=None)  # not backfilled
        resp = self.client.get(f"/api/v1/chat/threads/{thread.id}/messages/")
        by_cid = {m["client_msg_id"]: m["user_text"] for m in resp.data["messages"]}
        self.assertEqual(by_cid["enc"], "FROM_ENC")
        self.assertEqual(by_cid["leg"], "FROM_LEGACY")

    # ---------------- correctness: since feed (bulk) + b"" ----------------

    def test_since_feed_decrypts_and_drops_empty(self):
        self._read_on()
        main = self._thread(legacy_title="Main", enc_title="Main", is_main=True)
        self._msg(main, cid="hello", legacy="X", enc_value="HELLO_FROM_ENC")
        # An empty (photo-only) turn: legacy "" + b"" sentinel — must be dropped.
        self._msg(main, cid="empty", legacy="", enc_blob=box.encrypt(self.tenant.id, _UT[0], _UT[1], ""))
        resp = self.client.get("/api/v1/chat/messages/")
        self.assertEqual(resp.status_code, 200, resp.content)
        user_texts = [m["text"] for m in resp.data["messages"] if m.get("role") == "user"]
        self.assertIn("HELLO_FROM_ENC", user_texts)
        self.assertNotIn("", user_texts)  # empty turn produced no user row

    # ---------------- correctness: single-row poll + thread list ----------------

    def test_poll_single_decrypts(self):
        self._read_on()
        thread = self._thread()
        self._msg(thread, cid="poll1", legacy="LEG", enc_value="POLL_DECRYPTED")
        resp = self.client.get("/api/v1/chat/messages/poll1/")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data["user_text"], "POLL_DECRYPTED")

    def test_thread_list_decrypts_title(self):
        self._read_on()
        self._thread(legacy_title="LEG_TITLE", enc_title="REAL_TITLE")
        resp = self.client.get("/api/v1/chat/threads/")
        titles = [t["title"] for t in resp.data["threads"]]
        self.assertIn("REAL_TITLE", titles)
        self.assertNotIn("LEG_TITLE", titles)

    # ---------------- audit shape ----------------

    def test_bulk_read_emits_one_owner_request_event(self):
        self._read_on()
        pairs = [(self._seal(_UT, f"v{i}"), "x") for i in range(3)]
        records = self._capture(lambda: enc_read.read_values_bulk(self.tenant, _UT, pairs))
        self.assertEqual(len(records), 1)  # ONE event for the whole batch
        import json as _json

        payload = _json.loads(records[0].getMessage())
        self.assertEqual(payload["principal"], "owner_request")
        self.assertEqual(payload["row_count"], 3)

    def test_single_read_audits_under_ambient_principal(self):
        self._read_on()
        blob = self._seal(_UT, "v")
        token = audit._PRINCIPAL.set("owner_request")
        self.addCleanup(audit._PRINCIPAL.reset, token)
        records = self._capture(lambda: enc_read.read_value(self.tenant, _UT, blob, "x"))
        self.assertEqual(len(records), 1)
        import json as _json

        self.assertEqual(_json.loads(records[0].getMessage())["principal"], "owner_request")

    def test_collect_turns_silent_even_under_owner_ambient(self):
        self._read_on()
        thread = self._thread(is_main=True)
        self._msg(thread, cid="d1", legacy="LEG", enc_value="DIGEST_DECRYPTED")
        token = audit._PRINCIPAL.set("owner_request")  # a genuinely-ambient owner read context
        self.addCleanup(audit._PRINCIPAL.reset, token)

        captured = {}

        def run():
            turns, _tz = _collect_turns(self.tenant, since=timezone.now() - timedelta(hours=1))
            captured["turns"] = turns

        records = self._capture(run)
        # SILENT: the shared digest builder forces system principal, so no audit
        # fires even though owner_request is ambient (plan §2 / amendment b).
        self.assertEqual(records, [])
        # ...and it still decrypts the user_text for the digest.
        self.assertIn("DIGEST_DECRYPTED", [t["user"] for t in captured["turns"]])

    def test_flag_off_bulk_is_silent_and_legacy(self):
        pairs = [(self._seal(_UT, "enc"), "legacy")]
        records = self._capture(
            lambda: self.assertEqual(enc_read.read_values_bulk(self.tenant, _UT, pairs)[0], "legacy")
        )
        self.assertEqual(records, [])  # flag off → no decrypt → no audit

    def test_thread_str_has_no_title(self):
        thread = self._thread(legacy_title="SensitiveTitleText")
        self.assertNotIn("SensitiveTitleText", str(thread))
