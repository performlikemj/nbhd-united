"""Tests for apps.crypto.box — the public encrypt/decrypt/decrypt_bulk surface.

Runs against the stateful `_MOCK_KEK_REGISTRY` in
`apps.orchestrator.azure_client` (AZURE_MOCK=true). Each test mints its own
tenant(s) with a fresh random UUID so DEK-cache entries never collide across
tests (matches the convention in apps/crypto/test_keys.py).
"""

from __future__ import annotations

import json
import logging
from unittest.mock import patch

from django.test import TestCase

from apps.crypto import audit, box, cache
from apps.crypto.keys import mint_and_wrap_dek
from apps.crypto.nolog import RedactedStr
from apps.orchestrator import azure_client
from apps.tenants.models import Tenant, User

TABLE = "app_chat_message"
COLUMN = "user_text"


def _create_tenant(*, suffix: str) -> Tenant:
    user = User.objects.create_user(username=f"crypto-box-{suffix}", password="pass1234")
    return Tenant.objects.create(user=user, status=Tenant.Status.ACTIVE, model_tier=Tenant.ModelTier.STARTER)


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


class BoxTestCase(TestCase):
    """Common setUp for every box test that touches encrypt/decrypt.

    Forces `azure_client` into mock mode by patching `_is_mock` -> True, NOT
    by relying on the ambient `AZURE_MOCK` env var. In CI's full suite (~5k
    tests) another test can mutate `os.environ`, leaving `AZURE_MOCK` unset
    by the time these run — then `cache.get_dek` -> `azure_client.unwrap_dek`
    would take the REAL Azure branch and error. This mirrors the merged PR1
    pattern in `apps/orchestrator/test_azure_client_keys.py`. Started here in
    setUp (with addCleanup(stop)) rather than as a class decorator so it also
    covers subclasses that encrypt/decrypt inside their OWN setUp — a
    class-level `@patch` only wraps `test*` methods, never setUp.

    There is deliberately NO blanket `_PRINCIPAL` reset here. It used to exist
    to paper over `decrypt_bulk` mutating the shared ambient ContextVar; that
    leak is now fixed at the source (bulk passes a one-shot
    `principal_override` instead of writing the var), so the mask is gone. The
    handful of tests that DO set an ambient principal manage their own cleanup
    via `_set_principal` below.
    """

    def setUp(self):
        mock_patcher = patch("apps.orchestrator.azure_client._is_mock", return_value=True)
        mock_patcher.start()
        self.addCleanup(mock_patcher.stop)

    def _set_principal(self, kind: str) -> None:
        """Set the ambient audit principal for this test and auto-reset after.

        Uses a ContextVar token + addCleanup so a test that sets "admin" can
        never leak it into a later test on the same thread — self-contained
        per test, not a blanket mask.
        """
        token = audit._PRINCIPAL.set(kind)
        self.addCleanup(audit._PRINCIPAL.reset, token)

    def _capture_audit(self, fn):
        handler = _ListHandler()
        logger = logging.getLogger("nbhd.decrypt_audit")
        old_level = logger.level
        # See apps/crypto/test_audit.py — "nbhd.decrypt_audit" has no
        # explicit level configured, so its effective level is inherited
        # from root (WARNING); INFO calls are dropped before any handler
        # sees them unless the level is lowered for the capture window.
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        try:
            fn()
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)
        return handler.records


class EncryptDecryptRoundTripTest(BoxTestCase):
    def test_round_trips_plaintext(self):
        tenant = _create_tenant(suffix="roundtrip")
        mint_and_wrap_dek(tenant)

        blob = box.encrypt(tenant.id, TABLE, COLUMN, "hello, this is a real message")
        result = box.decrypt(tenant.id, TABLE, COLUMN, blob)

        self.assertIsInstance(result, RedactedStr)
        self.assertEqual(result.reveal(), "hello, this is a real message")

    def test_ciphertext_starts_with_marker_byte(self):
        tenant = _create_tenant(suffix="marker")
        mint_and_wrap_dek(tenant)

        blob = box.encrypt(tenant.id, TABLE, COLUMN, "content")

        self.assertEqual(blob[0], 0x01)

    def test_none_passes_through_both_ways(self):
        tenant = _create_tenant(suffix="none")
        mint_and_wrap_dek(tenant)

        self.assertIsNone(box.encrypt(tenant.id, TABLE, COLUMN, None))
        self.assertIsNone(box.decrypt(tenant.id, TABLE, COLUMN, None))

    def test_empty_string_is_the_b_empty_sentinel(self):
        tenant = _create_tenant(suffix="empty")
        mint_and_wrap_dek(tenant)

        blob = box.encrypt(tenant.id, TABLE, COLUMN, "")
        self.assertEqual(blob, b"")

        result = box.decrypt(tenant.id, TABLE, COLUMN, b"")
        self.assertIsInstance(result, RedactedStr)
        self.assertEqual(result.reveal(), "")


class RedactedStrProofAtBoxLevelTest(BoxTestCase):
    """The task's exact proof requirement, exercised on a REAL decrypted value
    (not a bare RedactedStr("...") construction) — str(), f-string, %s, and a
    real logging call must all redact; only .reveal() shows plaintext."""

    def setUp(self):
        super().setUp()
        self.tenant = _create_tenant(suffix="redact-proof")
        mint_and_wrap_dek(self.tenant)
        self.plaintext = "a secret the logs must never show"
        blob = box.encrypt(self.tenant.id, TABLE, COLUMN, self.plaintext)
        self.decrypted = box.decrypt(self.tenant.id, TABLE, COLUMN, blob)
        self.expected_redaction = f"‹redacted:{len(self.plaintext)}c›"

    def test_str_redacts(self):
        self.assertEqual(str(self.decrypted), self.expected_redaction)
        self.assertNotIn(self.plaintext, str(self.decrypted))

    def test_fstring_redacts(self):
        rendered = f"{self.decrypted}"
        self.assertEqual(rendered, self.expected_redaction)
        self.assertNotIn(self.plaintext, rendered)

    def test_percent_format_redacts(self):
        rendered = "%s" % self.decrypted  # noqa: UP031 - deliberately %-formatting, mirrors logger.info("%s", x)
        self.assertEqual(rendered, self.expected_redaction)
        self.assertNotIn(self.plaintext, rendered)

    def test_logging_call_redacts(self):
        logger_name = "apps.crypto.test_box.probe"
        with self.assertLogs(logger_name, level="INFO") as cm:
            logging.getLogger(logger_name).info("%s", self.decrypted)
        self.assertIn(self.expected_redaction, cm.output[0])
        self.assertNotIn(self.plaintext, cm.output[0])

    def test_reveal_gives_plaintext(self):
        self.assertEqual(self.decrypted.reveal(), self.plaintext)


class DualReadTest(BoxTestCase):
    def test_legacy_str_returns_verbatim(self):
        tenant = _create_tenant(suffix="legacy-str")
        mint_and_wrap_dek(tenant)

        result = box.decrypt(tenant.id, TABLE, COLUMN, "plain old text, never encrypted")

        self.assertIsInstance(result, RedactedStr)
        self.assertEqual(result.reveal(), "plain old text, never encrypted")

    def test_unmarked_nonempty_bytes_fail_closed(self):
        # A `bytea` `_enc` column never legitimately holds unmarked non-empty
        # bytes (legacy plaintext is a `str` in the OLD text column). Reading
        # them verbatim would be a fail-open auth bypass — FIX 3 makes it raise.
        tenant = _create_tenant(suffix="unmarked-bytes")
        mint_and_wrap_dek(tenant)

        with self.assertRaises(box.CryptoError):
            box.decrypt(tenant.id, TABLE, COLUMN, b"raw unmarked bytes, no marker")

    def test_legacy_read_never_touches_the_dek_cache(self):
        tenant = _create_tenant(suffix="legacy-no-cache")
        # Deliberately do NOT mint a DEK — if the legacy path touched the
        # cache/broker at all, this would raise TenantDek.DoesNotExist.
        result = box.decrypt(tenant.id, TABLE, COLUMN, "legacy, no DEK ever minted for this tenant")
        self.assertEqual(result.reveal(), "legacy, no DEK ever minted for this tenant")

    def test_b_empty_returns_empty_redacted_str(self):
        tenant = _create_tenant(suffix="legacy-empty")
        result = box.decrypt(tenant.id, TABLE, COLUMN, b"")
        self.assertEqual(result.reveal(), "")


class AadFailClosedTest(BoxTestCase):
    def test_wrong_tenant_raises_crypto_error(self):
        tenant_a = _create_tenant(suffix="aad-tenant-a")
        tenant_b = _create_tenant(suffix="aad-tenant-b")
        mint_and_wrap_dek(tenant_a)
        mint_and_wrap_dek(tenant_b)  # both must have a DEK so decrypt reaches the AEAD check

        blob = box.encrypt(tenant_a.id, TABLE, COLUMN, "tenant A's private message")

        with self.assertRaises(box.CryptoError):
            box.decrypt(tenant_b.id, TABLE, COLUMN, blob)

    def test_wrong_table_raises_crypto_error(self):
        tenant = _create_tenant(suffix="aad-table")
        mint_and_wrap_dek(tenant)

        blob = box.encrypt(tenant.id, "table_one", COLUMN, "secret")

        with self.assertRaises(box.CryptoError):
            box.decrypt(tenant.id, "table_two", COLUMN, blob)

    def test_wrong_column_raises_crypto_error(self):
        tenant = _create_tenant(suffix="aad-column")
        mint_and_wrap_dek(tenant)

        blob = box.encrypt(tenant.id, TABLE, "column_one", "secret")

        with self.assertRaises(box.CryptoError):
            box.decrypt(tenant.id, TABLE, "column_two", blob)

    def test_tampered_ciphertext_raises_crypto_error(self):
        tenant = _create_tenant(suffix="aad-tamper")
        mint_and_wrap_dek(tenant)

        blob = bytearray(box.encrypt(tenant.id, TABLE, COLUMN, "secret message"))
        blob[-1] ^= 0xFF

        with self.assertRaises(box.CryptoError):
            box.decrypt(tenant.id, TABLE, COLUMN, bytes(blob))

    def test_never_returns_garbage_on_failure(self):
        tenant = _create_tenant(suffix="aad-no-garbage")
        mint_and_wrap_dek(tenant)
        blob = bytearray(box.encrypt(tenant.id, TABLE, COLUMN, "secret message"))
        blob[-1] ^= 0xFF

        try:
            box.decrypt(tenant.id, TABLE, COLUMN, bytes(blob))
        except box.CryptoError:
            pass
        else:
            self.fail("expected CryptoError, got a return value instead")

    def test_malformed_envelope_too_short_raises_crypto_error(self):
        tenant = _create_tenant(suffix="aad-malformed")
        mint_and_wrap_dek(tenant)

        with self.assertRaises(box.CryptoError):
            box.decrypt(tenant.id, TABLE, COLUMN, bytes([0x01, 0x00, 0x00]))

    def test_unmarked_nonempty_bytes_raise_crypto_error(self):
        # FIX 3: unmarked non-empty bytes in a ciphertext column must fail
        # closed (not read back as attacker-chosen plaintext, no key needed).
        tenant = _create_tenant(suffix="aad-unmarked")
        mint_and_wrap_dek(tenant)

        with self.assertRaises(box.CryptoError):
            box.decrypt(tenant.id, TABLE, COLUMN, b"\x02attacker chosen plaintext")

    def test_unmarked_non_utf8_bytes_raise_crypto_error_not_unicode_error(self):
        # The non-UTF-8 crash the old verbatim-decode path could raise is
        # subsumed by FIX 3: the marker check rejects these BEFORE any decode,
        # so the failure is a CryptoError, never a raw UnicodeDecodeError.
        tenant = _create_tenant(suffix="aad-unmarked-binary")
        mint_and_wrap_dek(tenant)

        with self.assertRaises(box.CryptoError):
            box.decrypt(tenant.id, TABLE, COLUMN, b"\xff\xfe\xfd not utf-8 and unmarked")

    def test_purged_kek_raises_crypto_error_not_raw_lookup(self):
        # FIX 2: an envelope whose DEK can't be resolved (KEK purged / no
        # TenantDek row) must surface as CryptoError, not a raw
        # TenantDek.DoesNotExist / broker LookupError escaping the boundary.
        tenant = _create_tenant(suffix="aad-purged-kek")
        mint_and_wrap_dek(tenant)
        blob = box.encrypt(tenant.id, TABLE, COLUMN, "secret before the shred")

        # Force a cold DEK resolution on the next decrypt, then shred the KEK
        # so that resolution fails at the broker.
        cache._CACHE.pop((str(tenant.id), 0), None)
        azure_client.purge_kek(tenant.id)

        with self.assertRaises(box.CryptoError):
            box.decrypt(tenant.id, TABLE, COLUMN, blob)


class DecryptBulkTest(BoxTestCase):
    def test_one_unwrap_and_one_audit_event_for_the_whole_batch(self):
        tenant = _create_tenant(suffix="bulk")
        mint_and_wrap_dek(tenant)

        plaintexts = [f"message number {i}" for i in range(5)]
        blobs = [box.encrypt(tenant.id, TABLE, COLUMN, pt) for pt in plaintexts]

        # Encrypting already warmed the cache; drop that entry so
        # decrypt_bulk itself has to do the (one) cold unwrap.
        cache._CACHE.pop((str(tenant.id), 0), None)

        with patch(
            "apps.crypto.cache.azure_client.unwrap_dek",
            wraps=azure_client.unwrap_dek,
        ) as spy_unwrap:
            records = self._capture_audit(
                lambda: setattr(
                    self,
                    "results",
                    box.decrypt_bulk(tenant.id, TABLE, COLUMN, blobs, principal="admin"),
                ),
            )

        spy_unwrap.assert_called_once()
        self.assertEqual([r.reveal() for r in self.results], plaintexts)

        self.assertEqual(len(records), 1)
        payload = json.loads(records[0].getMessage())
        self.assertEqual(payload["row_count"], 5)
        self.assertEqual(payload["principal"], "admin")
        self.assertEqual(payload["tenant_id"], str(tenant.id))
        self.assertEqual(payload["table"], TABLE)
        self.assertEqual(payload["column"], COLUMN)

    def test_default_principal_system_is_silent(self):
        tenant = _create_tenant(suffix="bulk-silent")
        mint_and_wrap_dek(tenant)
        blobs = [box.encrypt(tenant.id, TABLE, COLUMN, "x")]

        records = self._capture_audit(
            lambda: box.decrypt_bulk(tenant.id, TABLE, COLUMN, blobs),
        )

        self.assertEqual(records, [])

    def test_empty_batch_returns_empty_list(self):
        tenant = _create_tenant(suffix="bulk-empty")
        mint_and_wrap_dek(tenant)

        self.assertEqual(box.decrypt_bulk(tenant.id, TABLE, COLUMN, []), [])

    def test_mixed_dual_read_batch(self):
        tenant = _create_tenant(suffix="bulk-mixed")
        mint_and_wrap_dek(tenant)

        blobs = [
            box.encrypt(tenant.id, TABLE, COLUMN, "encrypted one"),
            None,
            b"",
            "legacy plaintext",
        ]

        results = box.decrypt_bulk(tenant.id, TABLE, COLUMN, blobs)

        self.assertEqual(results[0].reveal(), "encrypted one")
        self.assertIsNone(results[1])
        self.assertEqual(results[2].reveal(), "")
        self.assertEqual(results[3].reveal(), "legacy plaintext")


class DecryptBulkPrincipalIsolationTest(BoxTestCase):
    """Regression for FIX 1: `decrypt_bulk`'s `principal=` audits THIS call as
    a one-shot override, and must NOT write the shared, process-lived
    `_PRINCIPAL` ContextVar. On a reused worker thread, writing the var would
    (a) false-attribute a later `system` decrypt as the bulk's principal, and
    (b) let a bulk defaulting to `system` silence a genuinely-ambient `admin`.
    """

    def test_bulk_admin_does_not_leave_admin_ambient(self):
        # Ambient principal is the default "system" (nothing set this test).
        tenant = _create_tenant(suffix="bulk-iso-a")
        mint_and_wrap_dek(tenant)
        blob = box.encrypt(tenant.id, TABLE, COLUMN, "x")

        bulk_records = self._capture_audit(
            lambda: box.decrypt_bulk(tenant.id, TABLE, COLUMN, [blob], principal="admin"),
        )
        self.assertEqual(len(bulk_records), 1)  # the bulk itself audits as admin

        # A following single decrypt in the SAME context must see ambient
        # "system" again — bulk must not have written "admin" into the var.
        after_records = self._capture_audit(
            lambda: box.decrypt(tenant.id, TABLE, COLUMN, blob),
        )
        self.assertEqual(after_records, [])

    def test_bulk_default_system_does_not_clobber_ambient_admin(self):
        self._set_principal("admin")  # a genuinely-ambient admin read context
        tenant = _create_tenant(suffix="bulk-iso-b")
        mint_and_wrap_dek(tenant)
        blob = box.encrypt(tenant.id, TABLE, COLUMN, "x")

        # decrypt_bulk with NO principal (defaults to "system"): its own event
        # is silent, but it must NOT overwrite the ambient "admin".
        bulk_records = self._capture_audit(
            lambda: box.decrypt_bulk(tenant.id, TABLE, COLUMN, [blob]),
        )
        self.assertEqual(bulk_records, [])

        # The ambient "admin" must survive — a following single decrypt audits.
        after_records = self._capture_audit(
            lambda: box.decrypt(tenant.id, TABLE, COLUMN, blob),
        )
        self.assertEqual(len(after_records), 1)
        self.assertEqual(json.loads(after_records[0].getMessage())["principal"], "admin")


class DecryptSingleAuditTest(BoxTestCase):
    def test_emits_row_count_1_for_admin(self):
        tenant = _create_tenant(suffix="single-audit-admin")
        mint_and_wrap_dek(tenant)
        blob = box.encrypt(tenant.id, TABLE, COLUMN, "x")

        self._set_principal("admin")
        records = self._capture_audit(lambda: box.decrypt(tenant.id, TABLE, COLUMN, blob))

        self.assertEqual(len(records), 1)
        payload = json.loads(records[0].getMessage())
        self.assertEqual(payload["row_count"], 1)

    def test_silent_for_default_system_principal(self):
        tenant = _create_tenant(suffix="single-audit-system")
        mint_and_wrap_dek(tenant)
        blob = box.encrypt(tenant.id, TABLE, COLUMN, "x")

        records = self._capture_audit(lambda: box.decrypt(tenant.id, TABLE, COLUMN, blob))

        self.assertEqual(records, [])
