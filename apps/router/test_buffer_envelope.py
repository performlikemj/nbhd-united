"""Unit tests for the minimal hibernation-buffer envelope.

Covers the Phase-0 privacy change (docs/encryption-at-rest-directive.md §7):
BufferedMessage stores a minimal redacted envelope, never the raw provider
webhook. These are pure-function tests — no DB, no model.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.router.buffer_envelope import (
    SCHEMA,
    build_buffer_envelope,
    envelope_is_minimal,
    envelope_is_voice,
    envelope_media,
    envelope_telegram_chat_id,
    redact_for_buffer,
)


class BuildBufferEnvelopeTelegramTest(SimpleTestCase):
    def test_text_message_keeps_only_routing_metadata_no_pii(self):
        update = {
            "update_id": 42,
            "message": {
                "message_id": 7,
                "text": "hey it's Alice, call me on 555-1234",
                "chat": {"id": 12345, "type": "private", "first_name": "Alice", "username": "alice_x"},
                "from": {"id": 999, "first_name": "Alice", "last_name": "Zhang", "username": "alice_x"},
            },
        }
        env = build_buffer_envelope("telegram", update)
        self.assertEqual(
            env, {"schema": SCHEMA, "channel": "telegram", "is_voice": False, "is_image": False, "chat_id": 12345}
        )
        # No raw PII field survived into the envelope.
        flat = str(env)
        for pii in ("Alice", "Zhang", "alice_x", "555-1234", "hey it's"):
            self.assertNotIn(pii, flat)

    def test_photo_message_preserves_file_id_reference_only(self):
        update = {
            "message": {
                "message_id": 8,
                "caption": "my form check",
                "chat": {"id": 12345, "type": "private"},
                "photo": [
                    {"file_id": "small_abc", "width": 90},
                    {"file_id": "large_xyz", "width": 1280},
                ],
            }
        }
        env = build_buffer_envelope("telegram", update)
        self.assertTrue(env["is_image"])
        # Keeps the LARGEST photo's opaque file_id — a reference, not bytes/PII.
        self.assertEqual(env["media"], {"photo_file_id": "large_xyz"})
        self.assertNotIn("my form check", str(env))

    def test_voice_message_flagged_and_file_id_kept(self):
        update = {"message": {"chat": {"id": 5}, "voice": {"file_id": "voice_1", "duration": 3}}}
        env = build_buffer_envelope("telegram", update)
        self.assertTrue(env["is_voice"])
        self.assertEqual(env["media"], {"voice_file_id": "voice_1"})

    def test_edited_message_shape_supported(self):
        update = {"edited_message": {"chat": {"id": 77}, "text": "typo fix"}}
        env = build_buffer_envelope("telegram", update)
        self.assertEqual(env["chat_id"], 77)

    def test_missing_chat_id_is_omitted(self):
        env = build_buffer_envelope("telegram", {"message": {"text": "x"}})
        self.assertNotIn("chat_id", env)


class BuildBufferEnvelopeLineTest(SimpleTestCase):
    def test_text_event_extracts_no_pii(self):
        event = {
            "type": "message",
            "replyToken": "tok",
            "source": {"userId": "U_secret_line_id", "type": "user"},
            "message": {"type": "text", "id": "m1", "text": "meet Bob at 5"},
        }
        env = build_buffer_envelope("line", event)
        self.assertEqual(env, {"schema": SCHEMA, "channel": "line", "is_voice": False})
        for pii in ("U_secret_line_id", "Bob", "meet"):
            self.assertNotIn(pii, str(env))

    def test_audio_event_flagged_voice(self):
        event = {"message": {"type": "audio", "id": "m2"}}
        env = build_buffer_envelope("line", event)
        self.assertTrue(env["is_voice"])


class EnvelopeHelpersTest(SimpleTestCase):
    def test_envelope_is_minimal_distinguishes_shapes(self):
        self.assertTrue(envelope_is_minimal({"schema": SCHEMA, "channel": "line"}))
        self.assertFalse(envelope_is_minimal({"events": []}))  # legacy LINE raw
        self.assertFalse(envelope_is_minimal({"message": {"text": "hi"}}))  # legacy TG raw
        self.assertFalse(envelope_is_minimal(None))

    def test_envelope_is_voice_new_shape(self):
        self.assertTrue(envelope_is_voice({"schema": SCHEMA, "channel": "line", "is_voice": True}))
        self.assertFalse(envelope_is_voice({"schema": SCHEMA, "channel": "line", "is_voice": False}))

    def test_envelope_is_voice_legacy_line_raw(self):
        # Legacy raw LINE webhook body shape (list of events).
        voice_raw = {"events": [{"message": {"type": "audio"}}]}
        text_raw = {"events": [{"message": {"type": "text"}}]}
        self.assertTrue(envelope_is_voice(voice_raw))
        self.assertFalse(envelope_is_voice(text_raw))

    def test_envelope_is_voice_legacy_telegram_raw(self):
        self.assertTrue(envelope_is_voice({"message": {"voice": {"file_id": "v"}}}))
        self.assertFalse(envelope_is_voice({"message": {"text": "hi"}}))

    def test_envelope_media_only_from_minimal(self):
        self.assertEqual(envelope_media({"schema": SCHEMA, "media": {"photo_file_id": "p"}}), {"photo_file_id": "p"})
        self.assertEqual(envelope_media({"schema": SCHEMA}), {})
        # Legacy raw rows expose no envelope media (their media rode the raw payload).
        self.assertEqual(envelope_media({"message": {"photo": [{"file_id": "p"}]}}), {})

    def test_envelope_telegram_chat_id_both_shapes(self):
        self.assertEqual(envelope_telegram_chat_id({"schema": SCHEMA, "chat_id": 555}), 555)
        self.assertEqual(envelope_telegram_chat_id({"message": {"chat": {"id": 666}}}), 666)  # legacy raw
        self.assertIsNone(envelope_telegram_chat_id({"schema": SCHEMA}))
        self.assertIsNone(envelope_telegram_chat_id(None))


class RedactForBufferTest(SimpleTestCase):
    def _tenant(self, entity_map):
        return SimpleNamespace(pii_entity_map=entity_map, pii_denylist={})

    def test_empty_text_returns_empty(self):
        self.assertEqual(redact_for_buffer(self._tenant({}), ""), "")
        self.assertEqual(redact_for_buffer(self._tenant({}), "   "), "   ")

    def test_reuse_only_second_pass_masks_known_entity_even_if_mint_noops(self):
        # Simulate redact_user_message fail-open / tier-disabled: returns raw.
        tenant = self._tenant({"[PERSON_1]": "Alice"})
        with patch("apps.pii.redactor.redact_user_message", side_effect=lambda t, x: t if False else "hi Alice"):
            out = redact_for_buffer(tenant, "hi Alice")
        # The reuse-only second pass still masks the known name.
        self.assertNotIn("Alice", out)
        self.assertIn("[PERSON_1]", out)

    def test_mint_exception_falls_back_to_reuse_only(self):
        tenant = self._tenant({"[PERSON_1]": "Alice"})
        with patch("apps.pii.redactor.redact_user_message", side_effect=RuntimeError("NER down")):
            out = redact_for_buffer(tenant, "hi Alice")
        # redact_user_message blew up → reuse-only fallback still masked the name.
        self.assertNotIn("Alice", out)
        self.assertIn("[PERSON_1]", out)

    def test_double_failure_drops_text_never_stores_raw(self):
        tenant = self._tenant({"[PERSON_1]": "Alice"})
        with (
            patch("apps.pii.redactor.redact_user_message", side_effect=RuntimeError("NER down")),
            patch("apps.pii.redactor.redact_known_entities", side_effect=RuntimeError("map broken")),
        ):
            out = redact_for_buffer(tenant, "hi Alice — SSN 123-45-6789")
        # Both paths failed → we drop the text rather than persist raw PII.
        self.assertEqual(out, "")

    def test_happy_path_delegates_to_redactor(self):
        tenant = self._tenant({})
        with (
            patch("apps.pii.redactor.redact_user_message", return_value="masked-1") as mock_mint,
            patch("apps.pii.redactor.redact_known_entities", return_value="masked-2") as mock_reuse,
        ):
            out = redact_for_buffer(tenant, "raw text")
        mock_mint.assert_called_once()
        mock_reuse.assert_called_once()
        self.assertEqual(out, "masked-2")
