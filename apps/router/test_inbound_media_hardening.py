"""P1-1 PDF structure hardening (docs/upload-security-threat-model.md).

Pins the structural gate `decode_and_validate_document` runs AFTER the
`%PDF-` magic sniff and BEFORE storage: reject active-content tokens,
encrypted trailers, and object-/page-count bombs, while letting an ordinary
small multi-page PDF through untouched. All fixtures are hand-built raw PDF
byte strings — no PDF library needed to exercise the byte scanner.
"""

from __future__ import annotations

import base64
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.router.inbound_media import (
    MAX_APP_DOCUMENT_BYTES,
    decode_and_validate_document,
    sniff_document_type,
)


def _b64(data: bytes) -> str:
    """``decode_and_validate_document`` takes a base64 string / data URL, not
    raw bytes — encode a fixture before handing it to the function under
    test (mirrors what the JSON request body actually carries).
    """
    return base64.b64encode(data).decode("ascii")


def _pdf_with(catalog_extra: bytes = b"", trailer_extra: bytes = b"", page_count: int = 1) -> bytes:
    """Build a minimal-but-valid synthetic PDF with ``page_count`` pages.

    Not a spec-perfect PDF (no working xref table), but sufficient for the
    byte-scanner under test, which never parses xref/object structure — it
    only regex-scans the raw bytes for tokens, `obj` markers, and `/Type
    /Page` occurrences.
    """
    kids = " ".join(f"{3 + i} 0 R" for i in range(page_count))
    body = b"%PDF-1.4\n"
    body += f"1 0 obj\n<< /Type /Catalog /Pages 2 0 R{catalog_extra.decode()} >>\nendobj\n".encode()
    body += f"2 0 obj\n<< /Type /Pages /Kids [{kids}] /Count {page_count} >>\nendobj\n".encode()
    for i in range(page_count):
        body += f"{3 + i} 0 obj\n<< /Type /Page /Parent 2 0 R >>\nendobj\n".encode()
    body += f"trailer\n<< /Root 1 0 R{trailer_extra.decode()} >>\n%%EOF".encode()
    return body


def _pdf_with_stream_body(stream_body: bytes) -> bytes:
    """A minimal PDF whose page content stream carries ``stream_body`` as its
    opaque BINARY payload — used to prove a byte sequence that merely
    *looks like* an active-content token, but sits inside `stream...
    endstream` data, is never live PDF syntax (a real reader treats it as
    compressed/binary content, not a dictionary key) and must not trip the
    active-content scan.
    """
    return (
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /Contents 4 0 R >>\nendobj\n"
        b"4 0 obj\n<< /Length "
        + str(len(stream_body)).encode()
        + b" >>\nstream\n"
        + stream_body
        + b"\nendstream\nendobj\n"
        b"trailer\n<< /Root 1 0 R >>\n%%EOF"
    )


_CLEAN_PDF = _pdf_with()
_CLEAN_MULTI_PAGE_PDF = _pdf_with(page_count=5)


class ActiveContentRejectionTests(SimpleTestCase):
    """Each active-content token must reject, matched only as `/Token`."""

    def _assert_rejects(self, data: bytes):
        result_data, ext, error = decode_and_validate_document(_b64(data))
        self.assertIsNone(result_data)
        self.assertIsNone(ext)
        self.assertEqual(error, "unsafe_document")

    def test_clean_pdf_passes(self):
        data, ext, error = decode_and_validate_document(_b64(_CLEAN_PDF))
        self.assertIsNone(error)
        self.assertEqual(ext, "pdf")
        self.assertEqual(data, _CLEAN_PDF)

    def test_open_action_alone_does_not_reject(self):
        # FIX 3: deliberately excluded. Bare /OpenAction also fires on benign
        # LaTeX/hyperref "open at page N" PDFs; a genuinely malicious one
        # must reference a /JavaScript, /JS, or /Launch action — caught
        # independently below.
        data = _pdf_with(catalog_extra=b" /OpenAction 5 0 R")
        _, ext, error = decode_and_validate_document(_b64(data))
        self.assertIsNone(error)
        self.assertEqual(ext, "pdf")

    def test_open_action_referencing_javascript_still_rejects(self):
        # The realistic malicious shape: OpenAction -> a JS action. Caught
        # via the /JS token itself, not via /OpenAction.
        data = _pdf_with(catalog_extra=b" /OpenAction << /S /JavaScript /JS (app.alert(1)) >>")
        _, ext, error = decode_and_validate_document(_b64(data))
        self.assertIsNone(ext)
        self.assertEqual(error, "unsafe_document")

    def test_additional_actions_rejects(self):
        self._assert_rejects(_pdf_with(catalog_extra=b" /AA << /WC 5 0 R >>"))

    def test_launch_rejects(self):
        self._assert_rejects(_pdf_with(catalog_extra=b" /Launch 5 0 R"))

    def test_javascript_rejects(self):
        self._assert_rejects(_pdf_with(catalog_extra=b" /JavaScript 5 0 R"))

    def test_js_rejects(self):
        self._assert_rejects(_pdf_with(catalog_extra=b" /JS (app.alert(1))"))

    def test_embedded_file_rejects(self):
        self._assert_rejects(_pdf_with(catalog_extra=b" /EmbeddedFile 5 0 R"))

    def test_plain_body_text_javascript_does_not_reject(self):
        # "JavaScript" with no leading slash is body TEXT, not a PDF name
        # token, and must not trip the scan (false-positive guard).
        data = _CLEAN_PDF.replace(b"/Catalog", b"/Catalog (mentions JavaScript in prose)")
        _, ext, error = decode_and_validate_document(_b64(data))
        self.assertIsNone(error)
        self.assertEqual(ext, "pdf")

    def test_longer_unrelated_name_does_not_reject(self):
        # `/JavaScriptFoo` is a longer, unrelated name — the boundary check
        # must not truncate-match it as `/JavaScript`.
        data = _pdf_with(catalog_extra=b" /JavaScriptFoo 5 0 R")
        _, ext, error = decode_and_validate_document(_b64(data))
        self.assertIsNone(error)
        self.assertEqual(ext, "pdf")

    def test_js_backup_does_not_reject(self):
        # FIX 4: `_` is a legal PDF name character, so a boundary of "not
        # [A-Za-z0-9]" would wrongly treat `/JS_backup` as `/JS` + boundary.
        # The real boundary is "next byte is whitespace/delimiter".
        data = _pdf_with(catalog_extra=b" /JS_backup 5 0 R")
        _, ext, error = decode_and_validate_document(_b64(data))
        self.assertIsNone(error)
        self.assertEqual(ext, "pdf")

    def test_aa_colon_custom_does_not_reject(self):
        # Same boundary fix, `:` variant.
        data = _pdf_with(catalog_extra=b" /AA:custom 5 0 R")
        _, ext, error = decode_and_validate_document(_b64(data))
        self.assertIsNone(error)
        self.assertEqual(ext, "pdf")

    def test_pages_node_does_not_reject_as_page_bomb(self):
        # Sanity: /Type /Pages (the tree root) must not itself trip anything.
        _, ext, error = decode_and_validate_document(_b64(_CLEAN_PDF))
        self.assertIsNone(error)
        self.assertEqual(ext, "pdf")

    def test_active_content_token_inside_stream_body_does_not_reject(self):
        # BLOCKER 1 regression: /AA and /JS occurring by byte-coincidence
        # inside a stream...endstream BINARY payload are not live PDF
        # syntax and must not trip the scan. Fails without stream masking.
        for token in (b"/AA ", b"/JS "):
            with self.subTest(token=token):
                data = _pdf_with_stream_body(b"\x00\x01random binary junk " + token + b" more binary junk\xff\xfe")
                _, ext, error = decode_and_validate_document(_b64(data))
                self.assertIsNone(error)
                self.assertEqual(ext, "pdf")

    def test_nul_separated_tokens_still_reject(self):
        # Fable-5 verification follow-up: NUL (`\x00`) is legal PDF
        # whitespace (ISO 32000-1 §7.2.2) that terminates a name token for a
        # compliant reader, but plain `\s` doesn't include it — `/JS\x00(...)`
        # is honored as the `/JS` name yet would slip past a `\s`-only
        # boundary. `_NAME_BOUNDARY` must be NUL-inclusive so all five
        # tokens still reject when NUL-separated from what follows.
        cases = (
            b" /JS\x00(app.alert(1))",
            b" /Launch\x00 5 0 R",
            b" /JavaScript\x00 5 0 R",
            b" /AA\x00 << /WC 5 0 R >>",
            b" /EmbeddedFile\x00 5 0 R",
        )
        for catalog_extra in cases:
            with self.subTest(catalog_extra=catalog_extra):
                data = _pdf_with(catalog_extra=catalog_extra)
                _, ext, error = decode_and_validate_document(_b64(data))
                self.assertIsNone(ext)
                self.assertEqual(error, "unsafe_document")


class EncryptedPdfRejectionTests(SimpleTestCase):
    def test_encrypt_trailer_rejects(self):
        data = _pdf_with(trailer_extra=b" /Encrypt 6 0 R")
        result_data, ext, error = decode_and_validate_document(_b64(data))
        self.assertIsNone(result_data)
        self.assertIsNone(ext)
        self.assertEqual(error, "unsafe_document")

    def test_unrelated_key_does_not_false_positive(self):
        # A key that merely shares a prefix with /Encrypt must not trip the
        # boundary-guarded regex on its own (it only appears alongside a
        # real /Encrypt dict in practice, exercised above).
        data = _pdf_with(trailer_extra=b" /EncryptMetadata true")
        _, ext, error = decode_and_validate_document(_b64(data))
        self.assertIsNone(error)
        self.assertEqual(ext, "pdf")


class ObjectAndPageCountBombTests(SimpleTestCase):
    """Patch the ceilings low so fixtures stay tiny and tests stay fast; the
    production constants (50,000 objects / 2,000 pages) are exercised
    separately via the normal multi-page fixture staying well under them.
    """

    def test_object_count_bomb_rejects(self):
        # 6 objects (1 catalog + 5 orphan bomb objects), ceiling patched to 5.
        bomb = b"%PDF-1.4\n" + b"".join(f"{i} 0 obj\nendobj\n".encode() for i in range(1, 7))
        bomb += b"trailer\n<< /Root 1 0 R >>\n%%EOF"
        with patch("apps.router.inbound_media._MAX_PDF_OBJECTS", 5):
            _, ext, error = decode_and_validate_document(_b64(bomb))
        self.assertIsNone(ext)
        self.assertEqual(error, "document_too_complex")

    def test_object_count_under_ceiling_passes(self):
        with patch("apps.router.inbound_media._MAX_PDF_OBJECTS", 5):
            _, ext, error = decode_and_validate_document(_b64(_CLEAN_PDF))
        self.assertIsNone(error)
        self.assertEqual(ext, "pdf")

    def test_page_count_bomb_rejects(self):
        bomb = _pdf_with(page_count=10)
        with patch("apps.router.inbound_media._MAX_PDF_PAGES", 9):
            _, ext, error = decode_and_validate_document(_b64(bomb))
        self.assertIsNone(ext)
        self.assertEqual(error, "document_too_complex")

    def test_page_count_under_ceiling_passes(self):
        with patch("apps.router.inbound_media._MAX_PDF_PAGES", 9):
            _, ext, error = decode_and_validate_document(_b64(_CLEAN_MULTI_PAGE_PDF))
        self.assertIsNone(error)
        self.assertEqual(ext, "pdf")

    def test_production_ceilings_are_generous_for_normal_document(self):
        # No patching: the real 50,000 object / 2,000 page ceilings must not
        # reject an ordinary small multi-page statement/invoice.
        data, ext, error = decode_and_validate_document(_b64(_CLEAN_MULTI_PAGE_PDF))
        self.assertIsNone(error)
        self.assertEqual(ext, "pdf")
        self.assertEqual(data, _CLEAN_MULTI_PAGE_PDF)

    def test_pages_tree_root_not_counted_toward_page_ceiling(self):
        # FIX 6 pin: _CLEAN_PDF has exactly ONE real /Type /Page plus one
        # /Type /Pages tree root. If /Pages were mistakenly counted as a
        # page, the count would be 2 and this ceiling-of-1 would reject.
        with patch("apps.router.inbound_media._MAX_PDF_PAGES", 1):
            _, ext, error = decode_and_validate_document(_b64(_CLEAN_PDF))
        self.assertIsNone(error)
        self.assertEqual(ext, "pdf")

    def test_production_page_ceiling_rejects_2001_pages(self):
        # Real 2,000-page ceiling, NOT patched — a 2,001-page fixture is only
        # ~130 KB, cheap enough to build and prove the real constant works,
        # not just a mock-patched stand-in.
        bomb = _pdf_with(page_count=2001)
        _, ext, error = decode_and_validate_document(_b64(bomb))
        self.assertIsNone(ext)
        self.assertEqual(error, "document_too_complex")


class StructuralGateOrderingTests(SimpleTestCase):
    """The structural gate runs strictly after the magic-byte sniff and
    strictly before storage — a non-PDF payload never reaches the scanner,
    and a rejected PDF never returns bytes for the caller to store.
    """

    def test_non_pdf_bytes_fail_magic_sniff_before_structure_scan(self):
        self.assertIsNone(sniff_document_type(b"not a pdf at all"))
        _, ext, error = decode_and_validate_document(_b64(b"not a pdf at all"))
        self.assertIsNone(ext)
        self.assertEqual(error, "unsupported_document_type")

    def test_rejected_document_returns_no_bytes(self):
        data = _pdf_with(catalog_extra=b" /Launch 5 0 R")
        result_data, ext, error = decode_and_validate_document(_b64(data))
        self.assertIsNone(result_data)
        self.assertIsNone(ext)
        self.assertEqual(error, "unsafe_document")

    def test_fixtures_stay_under_size_cap(self):
        # Sanity check on the fixtures themselves so a future edit doesn't
        # accidentally make a "clean" fixture too large to reach the
        # structural scan at all.
        self.assertLess(len(_CLEAN_PDF), MAX_APP_DOCUMENT_BYTES)
        self.assertLess(len(_CLEAN_MULTI_PAGE_PDF), MAX_APP_DOCUMENT_BYTES)


class RejectTelemetryTests(SimpleTestCase):
    """``doc_ingest_rejected`` (BLOCKER 2 of the Fable review) is the P1-1
    canary's reject-rate safety net — it must fire exactly once per
    structural reject, carry only tenant/code/byte-count (no raw content,
    mirroring ``doc_ingest_attached``'s discipline), and must NOT fire for a
    document that passes.
    """

    def test_active_content_reject_logs_doc_ingest_rejected(self):
        data = _pdf_with(catalog_extra=b" /Launch 5 0 R")
        with self.assertLogs("apps.router.inbound_media", level="INFO") as logs:
            _, ext, error = decode_and_validate_document(_b64(data), tenant_id="tenant-xyz")
        self.assertIsNone(ext)
        self.assertEqual(error, "unsafe_document")
        matching = [line for line in logs.output if "doc_ingest_rejected" in line]
        self.assertEqual(len(matching), 1, logs.output)
        line = matching[0]
        self.assertIn("tenant=tenant-xyz", line)
        self.assertIn("code=unsafe_document", line)
        self.assertIn(f"bytes={len(data)}", line)

    def test_object_count_bomb_reject_logs_correct_code(self):
        bomb = b"%PDF-1.4\n" + b"".join(f"{i} 0 obj\nendobj\n".encode() for i in range(1, 7))
        bomb += b"trailer\n<< /Root 1 0 R >>\n%%EOF"
        with (
            patch("apps.router.inbound_media._MAX_PDF_OBJECTS", 5),
            self.assertLogs("apps.router.inbound_media", level="INFO") as logs,
        ):
            _, ext, error = decode_and_validate_document(_b64(bomb), tenant_id="tenant-abc")
        self.assertIsNone(ext)
        self.assertEqual(error, "document_too_complex")
        matching = [line for line in logs.output if "doc_ingest_rejected" in line]
        self.assertEqual(len(matching), 1, logs.output)
        self.assertIn("code=document_too_complex", matching[0])
        self.assertIn("tenant=tenant-abc", matching[0])

    def test_clean_document_does_not_log_reject(self):
        with self.assertNoLogs("apps.router.inbound_media", level="INFO"):
            data, ext, error = decode_and_validate_document(_b64(_CLEAN_PDF), tenant_id="tenant-clean")
        self.assertIsNone(error)
        self.assertEqual(ext, "pdf")
        self.assertEqual(data, _CLEAN_PDF)

    def test_missing_tenant_id_still_rejects_without_crashing(self):
        # tenant_id is optional (default None) so existing call sites that
        # don't pass it keep working — the log line just carries tenant=None.
        data = _pdf_with(catalog_extra=b" /Launch 5 0 R")
        _, ext, error = decode_and_validate_document(_b64(data))
        self.assertIsNone(ext)
        self.assertEqual(error, "unsafe_document")
