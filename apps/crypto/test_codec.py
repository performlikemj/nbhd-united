"""Tests for apps.crypto.codec — envelope byte layout + AES-GCM seal/open.

Pure Python, no DB — SimpleTestCase.
"""

from __future__ import annotations

import os
import struct

from django.test import SimpleTestCase

from apps.crypto import codec


class PackUnpackTest(SimpleTestCase):
    def test_round_trips_components(self):
        nonce = os.urandom(codec.NONCE_LEN)
        ct = b"some-ciphertext-and-tag-bytes"

        blob = codec.pack(7, nonce, ct)
        epoch, out_nonce, out_ct = codec.unpack(blob)

        self.assertEqual(epoch, 7)
        self.assertEqual(out_nonce, nonce)
        self.assertEqual(out_ct, ct)

    def test_byte_layout_exact(self):
        nonce = bytes(range(12))  # 12 distinct bytes, easy to eyeball
        ct = b"CIPHERTEXT+TAG"

        blob = codec.pack(0x0102, nonce, ct)

        self.assertEqual(blob[0], 0x01)
        self.assertEqual(blob[1:3], struct.pack(">H", 0x0102))
        self.assertEqual(blob[3:15], nonce)
        self.assertEqual(blob[15:], ct)

    def test_marker_byte_is_0x01(self):
        blob = codec.pack(0, os.urandom(12), b"x")
        self.assertEqual(blob[0], codec.MARKER)
        self.assertEqual(codec.MARKER, 0x01)

    def test_pack_rejects_wrong_nonce_length(self):
        with self.assertRaises(ValueError):
            codec.pack(0, b"too-short", b"ct")

    def test_pack_rejects_epoch_out_of_uint16_range(self):
        with self.assertRaises(ValueError):
            codec.pack(70000, os.urandom(12), b"ct")

    def test_unpack_rejects_blob_too_short(self):
        with self.assertRaises(codec.CryptoError):
            codec.unpack(bytes([0x01, 0x00]))  # way under HEADER_LEN

    def test_unpack_rejects_wrong_marker(self):
        blob = bytes([0x02]) + b"\x00" * 20
        with self.assertRaises(codec.CryptoError):
            codec.unpack(blob)


class BuildAadTest(SimpleTestCase):
    def test_format_is_tenant_table_column(self):
        aad = codec.build_aad("tenant-123", "app_chat_message", "user_text")
        self.assertEqual(aad, b"tenant-123:app_chat_message:user_text")

    def test_coerces_non_str_tenant_id(self):
        import uuid

        tid = uuid.uuid4()
        aad = codec.build_aad(tid, "t", "c")
        self.assertEqual(aad, f"{tid}:t:c".encode())

    def test_no_row_id_in_aad(self):
        # Same (tenant, table, column) -> identical AAD regardless of which
        # row is being encrypted/decrypted — this IS the design (AAD must be
        # reproducible at decrypt time without needing the row's own id).
        aad1 = codec.build_aad("t1", "table", "col")
        aad2 = codec.build_aad("t1", "table", "col")
        self.assertEqual(aad1, aad2)


class SealOpenEnvelopeTest(SimpleTestCase):
    def setUp(self):
        self.dek = os.urandom(32)
        self.aad = codec.build_aad("tenant-1", "table", "col")

    def test_round_trips_plaintext(self):
        blob = codec.seal(self.dek, 0, self.aad, b"hello world")
        pt = codec.open_envelope(self.dek, self.aad, blob)
        self.assertEqual(pt, b"hello world")

    def test_each_seal_uses_a_fresh_random_nonce(self):
        blob1 = codec.seal(self.dek, 0, self.aad, b"same plaintext")
        blob2 = codec.seal(self.dek, 0, self.aad, b"same plaintext")
        self.assertNotEqual(blob1, blob2)  # different nonce -> different ciphertext
        self.assertNotEqual(blob1[3:15], blob2[3:15])

    def test_embeds_dek_epoch_in_header(self):
        blob = codec.seal(self.dek, 3, self.aad, b"x")
        epoch, _nonce, _ct = codec.unpack(blob)
        self.assertEqual(epoch, 3)

    def test_tampered_ciphertext_byte_fails_closed(self):
        blob = bytearray(codec.seal(self.dek, 0, self.aad, b"hello world"))
        blob[-1] ^= 0xFF  # flip a bit inside the tag
        with self.assertRaises(codec.CryptoError):
            codec.open_envelope(self.dek, self.aad, bytes(blob))

    def test_tampered_tag_fails_closed(self):
        blob = bytearray(codec.seal(self.dek, 0, self.aad, b"hello world"))
        blob[len(blob) - 16] ^= 0xFF  # flip a bit in the tag region
        with self.assertRaises(codec.CryptoError):
            codec.open_envelope(self.dek, self.aad, bytes(blob))

    def test_wrong_aad_fails_closed(self):
        blob = codec.seal(self.dek, 0, self.aad, b"hello world")
        wrong_aad = codec.build_aad("tenant-1", "table", "OTHER_COLUMN")
        with self.assertRaises(codec.CryptoError):
            codec.open_envelope(self.dek, wrong_aad, blob)

    def test_wrong_dek_fails_closed(self):
        blob = codec.seal(self.dek, 0, self.aad, b"hello world")
        wrong_dek = os.urandom(32)
        with self.assertRaises(codec.CryptoError):
            codec.open_envelope(wrong_dek, self.aad, blob)

    def test_open_never_returns_partial_plaintext_on_failure(self):
        blob = bytearray(codec.seal(self.dek, 0, self.aad, b"hello world"))
        blob[-1] ^= 0xFF
        try:
            codec.open_envelope(self.dek, self.aad, bytes(blob))
        except codec.CryptoError:
            pass
        else:
            self.fail("expected CryptoError, got a return value instead")
