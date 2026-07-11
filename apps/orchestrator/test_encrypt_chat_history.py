"""Tests for the encrypt_chat_history backfill command (Encryption-at-rest Phase 2 PR-3).

Seals legacy user_text / title plaintext into the *_enc sidecars for
write-flag-ON tenants. Uses the crypto ``_is_mock`` stateful mock like the
DEK backfill / box tests — never the ambient AZURE_MOCK env var.
"""

from __future__ import annotations

import secrets
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.db.models import QuerySet
from django.db.utils import OperationalError
from django.test import TestCase

from apps.crypto import box
from apps.crypto.keys import mint_and_wrap_dek
from apps.orchestrator.management.commands.encrypt_chat_history import Command
from apps.router import enc_columns
from apps.router.models import AppChatMessage, ChatThread
from apps.tenants.models import Tenant, User


class EncryptChatHistoryTest(TestCase):
    def setUp(self):
        patcher = patch("apps.orchestrator.azure_client._is_mock", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.tenant = self._make_tenant(write_flag=True)
        mint_and_wrap_dek(self.tenant)

    def _make_tenant(self, *, write_flag: bool) -> Tenant:
        user = User.objects.create_user(username=f"ech_{secrets.token_hex(4)}", email=f"{secrets.token_hex(4)}@e.com")
        return Tenant.objects.create(user=user, status=Tenant.Status.ACTIVE, encrypt_chat_writes=write_flag)

    def _thread(self, tenant, *, title="Work") -> ChatThread:
        # Created directly (not via the dual-write views) → title_enc defaults NULL.
        return ChatThread.objects.create(tenant=tenant, user=tenant.user, title=title, is_main=False)

    def _msg(self, tenant, thread, *, text, cid) -> AppChatMessage:
        return AppChatMessage.objects.create(
            tenant=tenant,
            user=tenant.user,
            thread=thread,
            client_msg_id=cid,
            user_text=text,
            status=AppChatMessage.Status.READY,
        )

    def _reveal(self, aad, blob):
        return box.decrypt(self.tenant.id, aad[0], aad[1], blob).reveal()

    def test_fills_gaps_and_round_trips(self):
        thread = self._thread(self.tenant, title="Grocery list")
        m1 = self._msg(self.tenant, thread, text="who am I?", cid="g1")
        m2 = self._msg(self.tenant, thread, text="remind me tomorrow", cid="g2")

        out = StringIO()
        call_command("encrypt_chat_history", stdout=out)

        m1.refresh_from_db()
        m2.refresh_from_db()
        thread.refresh_from_db()
        self.assertEqual(self._reveal(enc_columns.APP_CHAT_MESSAGE_USER_TEXT, bytes(m1.user_text_enc)), "who am I?")
        self.assertEqual(
            self._reveal(enc_columns.APP_CHAT_MESSAGE_USER_TEXT, bytes(m2.user_text_enc)), "remind me tomorrow"
        )
        self.assertEqual(self._reveal(enc_columns.CHAT_THREAD_TITLE, bytes(thread.title_enc)), "Grocery list")
        self.assertIn("Encrypted: 2 user_text + 1 title", out.getvalue())

    def test_idempotent_rerun_reports_zero(self):
        thread = self._thread(self.tenant)
        self._msg(self.tenant, thread, text="hi", cid="i1")
        call_command("encrypt_chat_history", stdout=StringIO())

        out = StringIO()
        call_command("encrypt_chat_history", stdout=out)
        self.assertIn("Encrypted: 0 user_text + 0 title", out.getvalue())

    def test_dry_run_writes_nothing(self):
        thread = self._thread(self.tenant, title="Secret plan")
        msg = self._msg(self.tenant, thread, text="don't store me plaintext", cid="d1")

        out = StringIO()
        call_command("encrypt_chat_history", "--dry-run", stdout=out)

        msg.refresh_from_db()
        thread.refresh_from_db()
        self.assertIsNone(msg.user_text_enc)
        self.assertIsNone(thread.title_enc)
        self.assertIn("Would encrypt", out.getvalue())
        # Counts only — the plaintext value must never appear in the output.
        self.assertNotIn("don't store me plaintext", out.getvalue())
        self.assertNotIn("Secret plan", out.getvalue())

    def test_flag_off_tenant_is_skipped(self):
        off = self._make_tenant(write_flag=False)
        mint_and_wrap_dek(off)
        thread = self._thread(off, title="Untouched")
        msg = self._msg(off, thread, text="leave me alone", cid="f1")

        out = StringIO()
        # Target only the flag-off tenant so nothing else is processed.
        call_command("encrypt_chat_history", "--tenant-id", str(off.id), stdout=out)

        msg.refresh_from_db()
        thread.refresh_from_db()
        self.assertIsNone(msg.user_text_enc)
        self.assertIsNone(thread.title_enc)
        self.assertIn("(1 flag-off skipped)", out.getvalue())
        self.assertIn("Encrypted: 0 user_text + 0 title", out.getvalue())

    def test_empty_values_seal_to_b_empty_sentinel(self):
        thread = self._thread(self.tenant, title="")
        msg = self._msg(self.tenant, thread, text="", cid="e1")

        call_command("encrypt_chat_history", stdout=StringIO())

        msg.refresh_from_db()
        thread.refresh_from_db()
        # "" seals to b"" — NOT left NULL (NULL = "not encrypted" discriminator).
        self.assertEqual(bytes(msg.user_text_enc), b"")
        self.assertEqual(bytes(thread.title_enc), b"")

    def test_only_null_enc_rows_touched(self):
        thread = self._thread(self.tenant)
        # A row already encrypted (as the dual-write would leave it) must NOT be
        # re-sealed — pin its ciphertext and confirm the backfill leaves it.
        pre = box.encrypt(self.tenant.id, *enc_columns.APP_CHAT_MESSAGE_USER_TEXT, "already sealed")
        m = AppChatMessage.objects.create(
            tenant=self.tenant,
            user=self.tenant.user,
            thread=thread,
            client_msg_id="p1",
            user_text="already sealed",
            user_text_enc=pre,
            status=AppChatMessage.Status.READY,
        )

        call_command("encrypt_chat_history", stdout=StringIO())

        m.refresh_from_db()
        self.assertEqual(bytes(m.user_text_enc), bytes(pre))

    def test_update_reconnects_and_retries_once_on_connection_drop(self):
        # Amendment (c): a per-row .update() that hits OperationalError closes
        # the dead connection, re-sets the RLS GUC, and retries once.
        thread = self._thread(self.tenant)
        m = self._msg(self.tenant, thread, text="survive the reap", cid="r1")

        real_update = QuerySet.update
        calls = {"n": 0}

        def flaky_update(self, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OperationalError("terminating connection due to idle-session timeout")
            return real_update(self, **kwargs)

        cmd = Command()
        with (
            patch("django.db.models.QuerySet.update", flaky_update),
            patch("django.db.connection.close") as mock_close,
        ):
            cmd._update_with_retry(
                self.tenant, AppChatMessage, m.pk, "user_text_enc", box.encrypt(self.tenant.id, "t", "c", "x")
            )

        self.assertEqual(calls["n"], 2)  # failed once, retried once
        mock_close.assert_called_once()
        m.refresh_from_db()
        self.assertIsNotNone(m.user_text_enc)
