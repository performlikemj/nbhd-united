"""Tests for per-tenant key generation utilities."""
from __future__ import annotations

from django.test import SimpleTestCase

from apps.orchestrator.key_utils import generate_internal_api_key, hash_internal_api_key


class KeyUtilsTest(SimpleTestCase):
    def test_generate_returns_43_char_url_safe_string(self):
        key = generate_internal_api_key()
        self.assertEqual(len(key), 43)
        allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
        self.assertTrue(all(c in allowed for c in key))

    def test_generated_keys_are_unique(self):
        keys = {generate_internal_api_key() for _ in range(100)}
        self.assertEqual(len(keys), 100)

    def test_hash_returns_64_hex_chars(self):
        h = hash_internal_api_key("test-key")
        self.assertEqual(len(h), 64)
        allowed = set("0123456789abcdef")
        self.assertTrue(all(c in allowed for c in h))

    def test_hash_is_deterministic(self):
        self.assertEqual(
            hash_internal_api_key("abc"),
            hash_internal_api_key("abc"),
        )

    def test_hash_differs_for_different_inputs(self):
        self.assertNotEqual(
            hash_internal_api_key("key-a"),
            hash_internal_api_key("key-b"),
        )
