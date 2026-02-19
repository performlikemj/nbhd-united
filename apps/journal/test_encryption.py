"""Encryption-specific tests for Journal Document plaintext<->ciphertext behavior."""

from __future__ import annotations

from uuid import uuid4
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.journal import encryption
from apps.journal.models import Document
from apps.tenants.models import Tenant, User


class EncryptionUtilsTest(TestCase):
    def test_encrypt_round_trip(self):
        key = b"\x00" * 32
        plaintext = "Hello, encrypted markdown ⚡"

        ciphertext = encryption.encrypt(plaintext, key)
        self.assertIsInstance(ciphertext, str)
        self.assertIn(":", ciphertext)

        restored = encryption.decrypt(ciphertext, key)
        self.assertEqual(restored, plaintext)

        with self.assertRaises(ValueError):
            encryption.decrypt("bad-payload", key)


@override_settings(AZURE_MOCK="true")
class DocumentEncryptionModelTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username=f"enc-{uuid4()}", password="pass")
        self.tenant = Tenant.objects.create(user=self.user, status="active")
        self.key = b"\x01" * 32

    def test_encrypt_on_save_and_decrypt_on_read(self):
        with patch("apps.journal.models.encryption.get_tenant_key", return_value=self.key):
            self.tenant.encryption_key_ref = "tenant-key-ref"
            self.tenant.save(update_fields=["encryption_key_ref", "updated_at"])

            doc = Document.objects.create(
                tenant=self.tenant,
                kind="daily",
                slug="2026-02-19",
                title="Secret Journal",
                markdown="# Morning Notes",
            )

        self.assertTrue(doc.is_encrypted)
        self.assertNotEqual(doc.title, "Secret Journal")
        self.assertNotEqual(doc.markdown, "# Morning Notes")
        self.assertEqual(doc.title_plaintext, "Secret Journal")
        self.assertEqual(doc.markdown_plaintext, "# Morning Notes")

    def test_unencrypted_docs_read_back_as_plaintext(self):
        doc = Document.objects.create(
            tenant=self.tenant,
            kind="daily",
            slug="2026-02-18",
            title="Plain Journal",
            markdown="# Not Encrypted",
        )

        self.assertFalse(doc.is_encrypted)
        self.assertEqual(doc.decrypt()["title"], "Plain Journal")
        self.assertEqual(doc.decrypt()["markdown"], "# Not Encrypted")

    def test_missing_key_raises_clear_error(self):
        self.tenant.encryption_key_ref = "tenant-key-missing"
        self.tenant.save(update_fields=["encryption_key_ref", "updated_at"])

        with patch("apps.journal.models.encryption.get_tenant_key", side_effect=ValueError("missing key")):
            with self.assertRaises(ValueError):
                Document.objects.create(
                    tenant=self.tenant,
                    kind="daily",
                    slug="2026-02-17",
                    title="Needs key",
                    markdown="# Blocked",
                )
