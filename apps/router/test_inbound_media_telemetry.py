"""Telemetry coverage for document arrival (Phase 1, §4 of
docs/document-information-keeping-directive.md).

``doc_ingest_attached`` is the upload-volume signal the directive's
enforcement/measurement section calls for. It must fire once per stored
document, carrying only a content-addressed hash + extension + byte count —
never the filename or extracted content (pii_mint no-raw-value discipline,
apps/pii/redactor.py) — and it must NOT fire for photo uploads, which are a
different, unrelated inbound-media path.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import SimpleTestCase

from apps.router.inbound_media import store_inbound_document, store_inbound_image

_PDF_BYTES = b"%PDF-1.4\n%fake pdf body for testing\n"
_JPEG_BYTES = b"\xff\xd8\xff\xe0fake jpeg body for testing"


class DocumentArrivalTelemetryTest(SimpleTestCase):
    @patch("apps.orchestrator.azure_client.upload_workspace_file_binary")
    def test_document_arrival_logs_doc_ingest_attached(self, mock_upload):
        with self.assertLogs("apps.router.inbound_media", level="INFO") as logs:
            store_inbound_document("tenant-abc", _PDF_BYTES, "pdf")

        mock_upload.assert_called_once()
        matching = [line for line in logs.output if "doc_ingest_attached" in line]
        self.assertEqual(len(matching), 1, logs.output)
        line = matching[0]
        self.assertIn("tenant=tenant-abc", line)
        self.assertIn("ext=pdf", line)
        self.assertIn(f"bytes={len(_PDF_BYTES)}", line)
        # No cleartext filename or content — only the 8-char hash prefix.
        self.assertNotIn(".pdf\n", line)
        self.assertNotIn("fake pdf body", line)

    @patch("apps.orchestrator.azure_client.upload_workspace_file_binary")
    def test_photo_arrival_does_not_log_doc_ingest_attached(self, mock_upload):
        with self.assertNoLogs("apps.router.inbound_media", level="INFO"):
            store_inbound_image("tenant-abc", _JPEG_BYTES, "jpg")

        mock_upload.assert_called_once()
