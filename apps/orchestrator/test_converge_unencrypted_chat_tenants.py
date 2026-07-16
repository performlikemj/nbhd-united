"""Tests for the converge_unencrypted_chat_tenants command (Encryption-at-rest Phase 2).

Compressed per-tenant fleet ladder for the post-2026-07-11 cohort (tenants
provisioned before chat-encryption flags were set at provision time): flip
``encrypt_chat_writes`` ON, seal the pre-flip plaintext into ``*_enc``, verify
zero plaintext-only rows remain, then flip ``read_encrypted_chat`` ON.

Uses the crypto ``_is_mock`` stateful mock (like the DEK backfill / box tests /
the PR-3 backfill), never the ambient AZURE_MOCK env var.
"""

from __future__ import annotations

import secrets
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from apps.crypto import box
from apps.crypto.keys import mint_and_wrap_dek
from apps.orchestrator.management.commands import encrypt_chat_history as ech
from apps.router import enc_columns
from apps.router.models import AppChatMessage, ChatThread
from apps.tenants.models import Tenant, User


class ConvergeUnencryptedChatTenantsTest(TestCase):
    def setUp(self):
        patcher = patch("apps.orchestrator.azure_client._is_mock", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

        # The post-flip cohort: BOTH flags off (writing+reading plaintext).
        self.tenant = self._make_tenant(write_flag=False, read_flag=False)
        mint_and_wrap_dek(self.tenant)

    def _make_tenant(self, *, write_flag: bool, read_flag: bool) -> Tenant:
        user = User.objects.create_user(username=f"cv_{secrets.token_hex(4)}", email=f"{secrets.token_hex(4)}@e.com")
        return Tenant.objects.create(
            user=user,
            status=Tenant.Status.ACTIVE,
            encrypt_chat_writes=write_flag,
            read_encrypted_chat=read_flag,
        )

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

    def _reveal(self, tenant, aad, blob):
        return box.decrypt(tenant.id, aad[0], aad[1], blob).reveal()

    def _run(self, *args):
        out = StringIO()
        call_command("converge_unencrypted_chat_tenants", *args, stdout=out)
        return out.getvalue()

    def test_converges_and_round_trips(self):
        thread = self._thread(self.tenant, title="Grocery list")
        m1 = self._msg(self.tenant, thread, text="who am I?", cid="c1")
        m2 = self._msg(self.tenant, thread, text="remind me tomorrow", cid="c2")

        output = self._run("--tenant-id", str(self.tenant.id))

        self.tenant.refresh_from_db()
        # Both flags flipped ON — the tenant now writes+reads encrypted.
        self.assertTrue(self.tenant.encrypt_chat_writes)
        self.assertTrue(self.tenant.read_encrypted_chat)

        m1.refresh_from_db()
        m2.refresh_from_db()
        thread.refresh_from_db()
        # Every pre-flip row sealed and round-trips.
        self.assertEqual(
            self._reveal(self.tenant, enc_columns.APP_CHAT_MESSAGE_USER_TEXT, bytes(m1.user_text_enc)), "who am I?"
        )
        self.assertEqual(
            self._reveal(self.tenant, enc_columns.APP_CHAT_MESSAGE_USER_TEXT, bytes(m2.user_text_enc)),
            "remind me tomorrow",
        )
        self.assertEqual(
            self._reveal(self.tenant, enc_columns.CHAT_THREAD_TITLE, bytes(thread.title_enc)), "Grocery list"
        )
        self.assertIn("Converged 1", output)

    def test_write_flag_persisted_before_backfill_and_read_flips_after_verify(self):
        # Ordering proof: at the moment the sealer runs, the write-flag must be
        # ON in the DB (flipped + persisted first) and the read-flag still OFF
        # (it flips only AFTER the post-seal verify).
        thread = self._thread(self.tenant, title="Plan")
        self._msg(self.tenant, thread, text="hi", cid="o1")

        real = ech.Command._backfill_tenant
        seen = {}

        def spy(inner_self, tenant, *, dry_run):
            fresh = Tenant.objects.get(pk=tenant.pk)
            seen["writes_at_seal"] = fresh.encrypt_chat_writes
            seen["reads_at_seal"] = fresh.read_encrypted_chat
            return real(inner_self, tenant, dry_run=dry_run)

        with patch.object(ech.Command, "_backfill_tenant", spy):
            self._run("--tenant-id", str(self.tenant.id))

        self.assertTrue(seen["writes_at_seal"])  # write-flag ON before the backfill
        self.assertFalse(seen["reads_at_seal"])  # read-flag still OFF during the backfill
        self.tenant.refresh_from_db()
        self.assertTrue(self.tenant.read_encrypted_chat)  # ... flipped ON only after verify

    def test_read_flag_stays_off_when_backfill_incomplete(self):
        # A backfill that can't seal every row (KV blip → per-row errors) must
        # leave plaintext-only rows behind → verify fails → read-flag stays OFF
        # (writes still on so future rows encrypt; a re-run finishes the seal).
        thread = self._thread(self.tenant, title="Untouched")
        m = self._msg(self.tenant, thread, text="cannot seal me", cid="e1")

        with patch("apps.crypto.box.encrypt", side_effect=RuntimeError("KV unreachable")):
            output = self._run("--tenant-id", str(self.tenant.id))

        self.tenant.refresh_from_db()
        self.assertTrue(self.tenant.encrypt_chat_writes)  # write-flag flipped first, independently
        self.assertFalse(self.tenant.read_encrypted_chat)  # read-flag gated on a clean verify
        m.refresh_from_db()
        self.assertIsNone(m.user_text_enc)  # nothing sealed
        self.assertIn("incomplete", output)

    def test_idempotent_second_run_is_no_op(self):
        thread = self._thread(self.tenant, title="Grocery list")
        m = self._msg(self.tenant, thread, text="who am I?", cid="i1")

        self._run("--tenant-id", str(self.tenant.id))
        m.refresh_from_db()
        first_ciphertext = bytes(m.user_text_enc)

        # Second run: tenant is now fully converged → skipped untouched.
        output = self._run("--tenant-id", str(self.tenant.id))
        self.assertIn("already-converged", output)
        self.assertIn("Converged 0", output)

        m.refresh_from_db()
        self.assertEqual(bytes(m.user_text_enc), first_ciphertext)  # not re-sealed

    def test_never_touches_already_converged_tenant(self):
        # A tenant that is already (writes=ON, reads=ON) with a pre-sealed row
        # must be skipped entirely — no flag write, no re-seal.
        converged = self._make_tenant(write_flag=True, read_flag=True)
        mint_and_wrap_dek(converged)
        thread = self._thread(converged, title="Done")
        pre = box.encrypt(converged.id, *enc_columns.APP_CHAT_MESSAGE_USER_TEXT, "already sealed")
        m = AppChatMessage.objects.create(
            tenant=converged,
            user=converged.user,
            thread=thread,
            client_msg_id="a1",
            user_text="already sealed",
            user_text_enc=pre,
            status=AppChatMessage.Status.READY,
        )

        output = self._run("--tenant-id", str(converged.id))

        converged.refresh_from_db()
        self.assertTrue(converged.encrypt_chat_writes)
        self.assertTrue(converged.read_encrypted_chat)
        m.refresh_from_db()
        self.assertEqual(bytes(m.user_text_enc), bytes(pre))  # ciphertext untouched
        self.assertIn("1 already-converged", output)

    def test_dry_run_flips_no_flag_and_seals_no_row(self):
        thread = self._thread(self.tenant, title="Secret plan")
        msg = self._msg(self.tenant, thread, text="don't store me plaintext", cid="d1")

        output = self._run("--dry-run", "--tenant-id", str(self.tenant.id))

        self.tenant.refresh_from_db()
        self.assertFalse(self.tenant.encrypt_chat_writes)
        self.assertFalse(self.tenant.read_encrypted_chat)
        msg.refresh_from_db()
        thread.refresh_from_db()
        self.assertIsNone(msg.user_text_enc)
        self.assertIsNone(thread.title_enc)
        self.assertIn("Would converge", output)
        # Counts only — the plaintext value must never appear in the output.
        self.assertNotIn("don't store me plaintext", output)
        self.assertNotIn("Secret plan", output)
