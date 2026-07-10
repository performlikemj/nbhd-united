"""Tests for apps.crypto.kdf — HKDF-SHA256 subkey derivation.

Pure Python, no DB — SimpleTestCase.
"""

from __future__ import annotations

import os

from django.test import SimpleTestCase

from apps.crypto import kdf


class SubkeyTest(SimpleTestCase):
    def setUp(self):
        self.dek = os.urandom(32)

    def test_returns_32_bytes(self):
        self.assertEqual(len(kdf.subkey(self.dek, kdf.CONTENT_V1)), 32)

    def test_deterministic_for_same_dek_and_info(self):
        a = kdf.subkey(self.dek, kdf.CONTENT_V1)
        b = kdf.subkey(self.dek, kdf.CONTENT_V1)
        self.assertEqual(a, b)

    def test_distinct_infos_yield_distinct_subkeys(self):
        content = kdf.subkey(self.dek, kdf.CONTENT_V1)
        pii_map = kdf.subkey(self.dek, kdf.MAP_V1)
        search = kdf.subkey(self.dek, kdf.SEARCH_V1)

        self.assertNotEqual(content, pii_map)
        self.assertNotEqual(content, search)
        self.assertNotEqual(pii_map, search)

    def test_distinct_deks_yield_distinct_subkeys(self):
        other_dek = os.urandom(32)
        a = kdf.subkey(self.dek, kdf.CONTENT_V1)
        b = kdf.subkey(other_dek, kdf.CONTENT_V1)
        self.assertNotEqual(a, b)

    def test_known_info_constants(self):
        self.assertEqual(kdf.CONTENT_V1, "content-v1")
        self.assertEqual(kdf.MAP_V1, "map-v1")
        self.assertEqual(kdf.SEARCH_V1, "search-v1")
