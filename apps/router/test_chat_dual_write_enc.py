"""Tests for the Phase 2 PR-2 chat dual-write (AppChatMessage.user_text + ChatThread.title).

When ``tenant.encrypt_chat_writes`` is on, the 5 writers in chat_views.py ALSO
seal the value into the ``*_enc`` sidecar via box.encrypt (AAD from
enc_columns), alongside the still-written plaintext column. Ships behind the
per-tenant flag; off by default. Uses the crypto ``_is_mock`` stateful mock like
the backfill/box tests — never the ambient AZURE_MOCK env var.
"""

from __future__ import annotations

import secrets
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.crypto import box
from apps.crypto.keys import mint_and_wrap_dek
from apps.router import enc_columns
from apps.router.chat_views import _encrypt_chat_value, _get_or_create_main_thread
from apps.router.models import AppChatMessage, ChatThread
from apps.tenants.models import Tenant, User


def _ok_drain_response(*args, **kwargs):
    resp = MagicMock()
    resp.status_code = 200
    resp.is_success = True
    resp.json.return_value = {"choices": [{"message": {"content": "ok"}}], "usage": {}, "model": "test"}
    resp.raise_for_status = MagicMock()
    return resp


@override_settings(NBHD_INTERNAL_API_KEY="test-key")
class ChatDualWriteEncTest(TestCase):
    def setUp(self):
        patcher = patch("apps.orchestrator.azure_client._is_mock", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.user = User.objects.create_user(
            username=f"dw_{secrets.token_hex(4)}", email=f"{secrets.token_hex(4)}@e.com"
        )
        self.tenant = Tenant.objects.create(
            user=self.user,
            status=Tenant.Status.ACTIVE,
            container_fqdn="oc-dw.example.com",
        )
        mint_and_wrap_dek(self.tenant)

        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _enable(self):
        self.tenant.encrypt_chat_writes = True
        self.tenant.save(update_fields=["encrypt_chat_writes", "updated_at"])

    def _reveal(self, aad, blob):
        return box.decrypt(self.tenant.id, aad[0], aad[1], blob).reveal()

    # ---- flag off ----

    def test_flag_off_writes_null_enc(self):
        resp = self.client.post("/api/v1/chat/threads/", {"title": "Work"}, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        thread = ChatThread.objects.get(id=resp.data["id"])
        self.assertIsNone(thread.title_enc)

        with patch("apps.router.pending_queue.httpx.post", side_effect=_ok_drain_response):
            resp = self.client.post("/api/v1/chat/messages/", {"text": "hi", "client_msg_id": "m-off"}, format="json")
        self.assertIn(resp.status_code, (200, 201), resp.content)
        msg = AppChatMessage.objects.get(tenant=self.tenant, client_msg_id="m-off")
        self.assertIsNone(msg.user_text_enc)

    # ---- title dual-write ----

    def test_thread_title_dual_write_when_flag_on(self):
        self._enable()
        resp = self.client.post("/api/v1/chat/threads/", {"title": "Work Notes"}, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        thread = ChatThread.objects.get(id=resp.data["id"])

        self.assertIsNotNone(thread.title_enc)
        blob = bytes(thread.title_enc)
        self.assertEqual(blob[0], 0x01)
        self.assertGreaterEqual(len(blob), 15)
        self.assertEqual(self._reveal(enc_columns.CHAT_THREAD_TITLE, blob), "Work Notes")

    def test_titleless_thread_enc_is_empty_sentinel(self):
        self._enable()
        resp = self.client.post("/api/v1/chat/threads/", {}, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        thread = ChatThread.objects.get(id=resp.data["id"])
        # "" seals to the b"" sentinel — NOT NULL (NULL means "not encrypted").
        self.assertEqual(bytes(thread.title_enc), b"")

    def test_main_thread_title_dual_write(self):
        self._enable()
        thread = _get_or_create_main_thread(self.tenant, self.user)
        self.assertEqual(self._reveal(enc_columns.CHAT_THREAD_TITLE, bytes(thread.title_enc)), "Main")

    # ---- user_text dual-write ----

    def test_user_text_dual_write_when_flag_on(self):
        self._enable()
        with patch("apps.router.pending_queue.httpx.post", side_effect=_ok_drain_response):
            resp = self.client.post(
                "/api/v1/chat/messages/", {"text": "who am I?", "client_msg_id": "m-on"}, format="json"
            )
        self.assertIn(resp.status_code, (200, 201), resp.content)
        msg = AppChatMessage.objects.get(tenant=self.tenant, client_msg_id="m-on")
        blob = bytes(msg.user_text_enc)
        self.assertEqual(blob[0], 0x01)
        self.assertGreaterEqual(len(blob), 15)
        self.assertEqual(self._reveal(enc_columns.APP_CHAT_MESSAGE_USER_TEXT, blob), "who am I?")
        # Plaintext is STILL written (dual-write, reversible until erase).
        self.assertEqual(msg.user_text, "who am I?")

    def test_on_device_turn_dual_write(self):
        self._enable()
        resp = self.client.post("/api/v1/chat/turns/", {"text": "a local note", "client_msg_id": "t-on"}, format="json")
        self.assertIn(resp.status_code, (200, 201), resp.content)
        msg = AppChatMessage.objects.get(tenant=self.tenant, client_msg_id="t-on")
        self.assertEqual(msg.source, AppChatMessage.Source.ON_DEVICE)
        self.assertEqual(self._reveal(enc_columns.APP_CHAT_MESSAGE_USER_TEXT, bytes(msg.user_text_enc)), "a local note")

    # ---- empty + None sentinels at the helper (covers the photo-only user_text="" case) ----

    def test_empty_and_none_sentinels(self):
        self._enable()
        # "" (photo/PDF-only turn) → the b"" sentinel; None → None.
        self.assertEqual(_encrypt_chat_value(self.tenant, enc_columns.APP_CHAT_MESSAGE_USER_TEXT, ""), b"")
        self.assertIsNone(_encrypt_chat_value(self.tenant, enc_columns.APP_CHAT_MESSAGE_USER_TEXT, None))

    # ---- soft-fail ----

    def test_encrypt_failure_soft_fails_and_keeps_plaintext(self):
        self._enable()
        with (
            patch("apps.crypto.box.encrypt", side_effect=RuntimeError("KV down")),
            patch("apps.router.pending_queue.httpx.post", side_effect=_ok_drain_response),
            self.assertLogs("apps.router.chat_views", level="WARNING") as log_ctx,
        ):
            resp = self.client.post(
                "/api/v1/chat/messages/", {"text": "still here", "client_msg_id": "m-fail"}, format="json"
            )
        self.assertIn(resp.status_code, (200, 201), resp.content)
        msg = AppChatMessage.objects.get(tenant=self.tenant, client_msg_id="m-fail")
        # Soft-fail: no ciphertext, but the row is fully readable via plaintext.
        self.assertIsNone(msg.user_text_enc)
        self.assertEqual(msg.user_text, "still here")
        self.assertTrue(any("dual-write encrypt failed" in m for m in log_ctx.output))
