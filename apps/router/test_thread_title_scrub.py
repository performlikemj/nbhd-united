"""Regression coverage for coordinate-safe ChatThread title persistence."""

from __future__ import annotations

import secrets
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient

from apps.crypto import box
from apps.crypto.keys import mint_and_wrap_dek
from apps.router import enc_columns
from apps.router.conversation_capture import _LOCATION_COORDINATE_PAIR_RE
from apps.router.models import ChatThread
from apps.tenants.models import Tenant, User

_RAW_PIN_TITLE = "📍 Current location: 34.69337, 135.49415 (±12m)"
_SAFE_PIN_TITLE = "📍 Current location: … (±12m)"


class ChatThreadTitleScrubTest(TestCase):
    def setUp(self):
        patcher = patch("apps.orchestrator.azure_client._is_mock", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.user = User.objects.create_user(
            username=f"title_scrub_{secrets.token_hex(4)}",
            email=f"{secrets.token_hex(4)}@example.com",
        )
        self.tenant = Tenant.objects.create(
            user=self.user,
            status=Tenant.Status.ACTIVE,
            encrypt_chat_writes=True,
            read_encrypted_chat=True,
        )
        mint_and_wrap_dek(self.tenant)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _reveal_title(self, thread: ChatThread) -> str:
        return box.decrypt(
            self.tenant.id,
            *enc_columns.CHAT_THREAD_TITLE,
            bytes(thread.title_enc),
        ).reveal()

    def _legacy_thread(self, title: str) -> ChatThread:
        return ChatThread.objects.create(
            tenant=self.tenant,
            user=self.user,
            title=title,
            title_enc=box.encrypt(self.tenant.id, *enc_columns.CHAT_THREAD_TITLE, title),
        )

    def test_location_pin_is_scrubbed_on_create_in_plaintext_and_ciphertext(self):
        response = self.client.post("/api/v1/chat/threads/", {"title": _RAW_PIN_TITLE}, format="json")

        self.assertEqual(response.status_code, 201, response.content)
        thread = ChatThread.objects.get(pk=response.data["id"])
        self.assertEqual(thread.title, _SAFE_PIN_TITLE)
        self.assertEqual(self._reveal_title(thread), _SAFE_PIN_TITLE)
        self.assertIsNone(_LOCATION_COORDINATE_PAIR_RE.search(thread.title))
        self.assertIsNone(_LOCATION_COORDINATE_PAIR_RE.search(self._reveal_title(thread)))

    def test_normal_title_passes_through_byte_identical(self):
        title = "Roadmap — Q3:\r\ncafé"
        response = self.client.post("/api/v1/chat/threads/", {"title": title}, format="json")

        self.assertEqual(response.status_code, 201, response.content)
        thread = ChatThread.objects.get(pk=response.data["id"])
        self.assertEqual(thread.title, title)
        self.assertEqual(self._reveal_title(thread), title)

    def test_backfill_dry_run_then_real_run_scrubs_both_title_columns(self):
        thread = self._legacy_thread(_RAW_PIN_TITLE)

        dry_run_output = StringIO()
        call_command(
            "scrub_chat_thread_titles",
            "--tenant-id",
            str(self.tenant.id),
            "--dry-run",
            stdout=dry_run_output,
        )
        thread.refresh_from_db()
        self.assertEqual(thread.title, _RAW_PIN_TITLE)
        self.assertEqual(self._reveal_title(thread), _RAW_PIN_TITLE)
        self.assertIn("Scanned 1; would change 1; errors 0", dry_run_output.getvalue())

        real_output = StringIO()
        call_command(
            "scrub_chat_thread_titles",
            "--tenant-id",
            str(self.tenant.id),
            stdout=real_output,
        )
        thread.refresh_from_db()
        self.assertEqual(thread.title, _SAFE_PIN_TITLE)
        self.assertEqual(self._reveal_title(thread), _SAFE_PIN_TITLE)
        self.assertIn("Scanned 1; changed 1; errors 0", real_output.getvalue())

    def test_backfill_is_idempotent(self):
        thread = self._legacy_thread(_RAW_PIN_TITLE)
        call_command(
            "scrub_chat_thread_titles",
            "--tenant-id",
            str(self.tenant.id),
            stdout=StringIO(),
        )
        thread.refresh_from_db()
        first_ciphertext = bytes(thread.title_enc)

        output = StringIO()
        call_command(
            "scrub_chat_thread_titles",
            "--tenant-id",
            str(self.tenant.id),
            stdout=output,
        )
        thread.refresh_from_db()
        self.assertEqual(thread.title, _SAFE_PIN_TITLE)
        self.assertEqual(bytes(thread.title_enc), first_ciphertext)
        self.assertIn("Scanned 1; changed 0; errors 0", output.getvalue())
