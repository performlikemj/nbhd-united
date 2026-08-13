"""Async PDF extraction: text-layer read, share artifacts, delivery turn.

Covers the Phase-1 slice described in ``CONTINUITY_async_pdf_extraction.md``:
the PDF read moves off the agent's turn into a QStash task, the extracted text
is written to the tenant share, and the result comes back as a system turn the
agent answers the user's original request with.

Fixture PDFs are built programmatically (``_build_pdf``) rather than committed
as binaries — one with a real text layer, one image-only (a filled rectangle, no
text objects). Both are deliberately valid enough to pass the ingress structural
gate, so the extraction tests exercise documents that could really have been
stored.
"""

from __future__ import annotations

import secrets
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from apps.router.models import ProactiveOutbound
from apps.router.pdf_extraction import (
    DELIVERY_MARKER_SUFFIX,
    EXTRACTED_TEXT_SUFFIX,
    MAX_INLINE_CHARS,
    build_extraction_fallback_turn,
    build_extraction_ready_turn,
    delivery_marker_path,
    extract_pdf_text,
    extracted_text_path,
    extraction_dedup_id,
    redact_extracted_document_text,
)
from apps.router.tasks import extract_inbound_document_task
from apps.tenants.models import Tenant, User

_DOC_PATH = "workspace/media/inbound/doc_ab12cd34.pdf"
_TEXT_LAYER_CONTENT = "Invoice total 4200 USD due Friday"


def _build_pdf(content_stream: bytes, *, with_font: bool = True) -> bytes:
    """Assemble a minimal single-page PDF with a correct xref table.

    Hand-built rather than pypdf-written because pypdf has no text-drawing API —
    a text layer has to come from a real content stream (``BT ... Tj ... ET``).
    Byte offsets are computed as the objects are appended so the xref is genuinely
    valid and pypdf takes the normal parse path, not its damaged-file repair path.
    """
    resources = b"<< /Font << /F1 5 0 R >> >>" if with_font else b"<< >>"
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources " + resources + b" >>",
        b"<< /Length " + str(len(content_stream)).encode() + b" >>\nstream\n" + content_stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += str(index).encode() + b" 0 obj\n" + body + b"\nendobj\n"

    xref_at = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += b"trailer\n<< /Size " + str(len(objects) + 1).encode() + b" /Root 1 0 R >>\n"
    out += b"startxref\n" + str(xref_at).encode() + b"\n%%EOF\n"
    return bytes(out)


TEXT_LAYER_PDF = _build_pdf(f"BT /F1 24 Tf 72 720 Td ({_TEXT_LAYER_CONTENT}) Tj ET".encode())
IMAGE_ONLY_PDF = _build_pdf(b"0.2 0.4 0.6 rg 72 600 200 120 re f", with_font=False)


def _make_tenant() -> Tenant:
    user = User.objects.create_user(
        username=f"pdf_{secrets.token_hex(4)}",
        email=f"{secrets.token_hex(4)}@example.com",
        preferred_channel="telegram",
    )
    return Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE,
        container_fqdn="oc-pdf.example.com",
        # has_entitlement is a computed property; budget-exempt is the
        # no-Stripe-row way to make it True.
        is_budget_exempt=True,
    )


def _ok_turn_response(text: str = "Here's what the invoice says."):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"choices": [{"message": {"content": text}}], "usage": {}, "model": "test"}
    resp.raise_for_status = MagicMock()
    return resp


class ExtractPdfTextTest(TestCase):
    """The pure extraction function — no share, no tenant, no network."""

    def test_text_layer_pdf_yields_its_text(self):
        self.assertIn(_TEXT_LAYER_CONTENT, extract_pdf_text(TEXT_LAYER_PDF))

    def test_image_only_pdf_yields_empty_string(self):
        # No text objects at all → Phase 1 declines and the caller falls back to
        # the in-turn pdf tool. An empty string is the ONLY "no text layer"
        # signal; the task branches on it.
        self.assertEqual(extract_pdf_text(IMAGE_ONLY_PDF), "")

    def test_garbage_bytes_raise_rather_than_pretending(self):
        # A non-PDF can't reach the task (ingress magic-byte gate), but if it
        # ever did we want a loud failure routed to the honest fallback turn,
        # not a silent empty extraction indistinguishable from a scanned page.
        with self.assertRaises(Exception):
            extract_pdf_text(b"definitely not a pdf")

    def test_pii_seam_is_a_pass_through_but_exists(self):
        # Pins the seam's presence and signature so wiring redaction later is a
        # one-call-site change (docs/pii-coverage-audit-2026-08-04.md).
        self.assertEqual(redact_extracted_document_text(None, "Fiona paid 4200"), "Fiona paid 4200")


class ExtractionPathsAndDedupTest(TestCase):
    def test_artifact_paths_hang_off_the_document_path(self):
        self.assertEqual(extracted_text_path(_DOC_PATH), _DOC_PATH + EXTRACTED_TEXT_SUFFIX)
        self.assertEqual(delivery_marker_path(_DOC_PATH), _DOC_PATH + DELIVERY_MARKER_SUFFIX)

    def test_dedup_id_carries_no_character_qstash_rejects(self):
        # Invariant 6: QStash 400s on ':' or whitespace in a deduplication id.
        dedup = extraction_dedup_id("148ccf1c-0000-4000-8000-000000000000", _DOC_PATH)
        for forbidden in (":", " ", "\t", "\n", "\r"):
            self.assertNotIn(forbidden, dedup)

    def test_dedup_id_survives_publish_tasks_own_validator(self):
        # The real gate is publish_task's eager check — assert against it, not a
        # reimplementation of it, so this test tracks the validator if it moves.
        from apps.cron.publish import _DEDUP_FORBIDDEN

        dedup = extraction_dedup_id("148ccf1c-0000-4000-8000-000000000000", _DOC_PATH)
        self.assertFalse([c for c in _DEDUP_FORBIDDEN if c in dedup])

    def test_dedup_id_distinguishes_same_basename_in_different_dirs(self):
        a = extraction_dedup_id("t1", "workspace/media/inbound/doc_aa.pdf")
        b = extraction_dedup_id("t1", "workspace/other/doc_aa.pdf")
        self.assertNotEqual(a, b)


class DeliveryTurnTextTest(TestCase):
    def test_ready_turn_inlines_the_text_and_names_the_share_path(self):
        turn = build_extraction_ready_turn(workspace_path=_DOC_PATH, text=_TEXT_LAYER_CONTENT)
        # Inline, because the chat-context tool policy strips fs read
        # (invariant 16) — an agent told only "the text is at <path>" is stuck.
        self.assertIn(_TEXT_LAYER_CONTENT, turn)
        self.assertIn("/home/node/.openclaw/" + _DOC_PATH + EXTRACTED_TEXT_SUFFIX, turn)
        self.assertIn("do NOT call the pdf tool", turn)
        self.assertIn("UNTRUSTED DATA", turn)

    def test_ready_turn_clamps_a_long_document(self):
        turn = build_extraction_ready_turn(workspace_path=_DOC_PATH, text="x" * (MAX_INLINE_CHARS + 5_000))
        self.assertIn("truncated", turn)
        # The clamp bounds the inline body, not the whole turn (framing +
        # untrusted notice ride along), so allow generous headroom over the cap.
        self.assertLess(len(turn), MAX_INLINE_CHARS + 4_000)

    def test_fallback_turn_hands_the_document_back_to_the_pdf_tool(self):
        turn = build_extraction_fallback_turn(workspace_path=_DOC_PATH, reason="it is a scanned PDF")
        self.assertIn("pdf tool", turn)
        self.assertIn("/home/node/.openclaw/" + _DOC_PATH, turn)
        self.assertIn("it is a scanned PDF", turn)
        # No extracted-text artifact is referenced — there isn't one.
        self.assertNotIn(EXTRACTED_TEXT_SUFFIX, turn)


@override_settings(NBHD_INTERNAL_API_KEY="test-key", NBHD_DISABLE_BACKGROUND_THREADS=True)
class ExtractInboundDocumentTaskTest(TestCase):
    """The QStash task: share reads/writes, idempotency, delivery."""

    def setUp(self):
        self.tenant = _make_tenant()

    def _run(self, *, pdf_bytes=TEXT_LAYER_PDF, marker=None, post=None, thread_id="thread-1"):
        """Run the task with the share and the gateway both mocked.

        Returns ``(result, mocks)`` where ``mocks`` exposes ``upload``, ``post``
        and ``download`` for assertions.
        """
        post_mock = post or MagicMock(return_value=_ok_turn_response())
        with (
            patch("apps.orchestrator.azure_client.download_workspace_file", return_value=marker) as download,
            patch("apps.orchestrator.azure_client.download_workspace_file_binary", return_value=pdf_bytes),
            patch("apps.orchestrator.azure_client.upload_workspace_file") as upload,
            patch("apps.router.tasks.httpx.post", post_mock),
        ):
            result = extract_inbound_document_task(str(self.tenant.id), _DOC_PATH, thread_id)
        return result, {"upload": upload, "post": post_mock, "download": download}

    def test_text_layer_document_is_extracted_written_and_delivered(self):
        result, mocks = self._run()

        self.assertEqual(result["status"], "extracted")
        self.assertGreater(result["chars"], 0)

        # The extracted text goes to the share through upload_workspace_file —
        # i.e. _put_share_file's sanitize chokepoint (invariant 2), never a
        # hand-rolled upload.
        written = {call.args[1]: call.args[2] for call in mocks["upload"].call_args_list}
        self.assertIn(_DOC_PATH + EXTRACTED_TEXT_SUFFIX, written)
        self.assertIn(_TEXT_LAYER_CONTENT, written[_DOC_PATH + EXTRACTED_TEXT_SUFFIX])

        # And the done-marker lands so a redelivery can't re-send.
        self.assertIn(_DOC_PATH + DELIVERY_MARKER_SUFFIX, written)

    def test_delivery_turn_targets_the_users_own_thread_session(self):
        # The whole point of the follow-up turn is that the agent answers the
        # request the user already made. That request lives in the
        # thread:<id> session — a turn sent anywhere else arrives contextless.
        _, mocks = self._run(thread_id="abc-123")

        mocks["post"].assert_called_once()
        _, kwargs = mocks["post"].call_args
        self.assertEqual(kwargs["json"]["user"], "thread:abc-123")
        self.assertEqual(kwargs["headers"]["X-Channel"], "ios")
        self.assertEqual(kwargs["headers"]["X-OpenClaw-Message-Channel"], "ios")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-key")
        self.assertIn(_TEXT_LAYER_CONTENT, kwargs["json"]["messages"][0]["content"])

    def test_reply_reaches_the_user_as_a_proactive_outbound(self):
        # Same delivery leg first-session-welcome uses: there is no
        # AppChatMessage row behind a Django-initiated turn, so the agent's
        # answer reaches the ?since= feed (and the APNs push) via this row.
        self._run(post=MagicMock(return_value=_ok_turn_response("The invoice is 4200 USD.")))

        row = ProactiveOutbound.objects.get(tenant=self.tenant)
        self.assertEqual(row.channel, "app")
        self.assertEqual(row.job_name, "_document_extraction")
        self.assertIn("4200", row.message_text)

    def test_already_delivered_document_is_a_clean_no_op(self):
        # Done-marker present → QStash redelivery must not re-post the turn or
        # re-write the share. This is the whole idempotency story (no DB rows).
        result, mocks = self._run(marker="extracted 2026-08-06T00:00:00+00:00\n")

        self.assertEqual(result["status"], "already_delivered")
        mocks["post"].assert_not_called()
        mocks["upload"].assert_not_called()
        self.assertFalse(ProactiveOutbound.objects.filter(tenant=self.tenant).exists())

    def test_scanned_document_falls_back_to_the_in_turn_pdf_tool(self):
        result, mocks = self._run(pdf_bytes=IMAGE_ONLY_PDF)

        self.assertEqual(result["status"], "no_text_layer")
        # No extracted-text artifact — there is no text. The marker still lands.
        written = {call.args[1] for call in mocks["upload"].call_args_list}
        self.assertNotIn(_DOC_PATH + EXTRACTED_TEXT_SUFFIX, written)
        self.assertIn(_DOC_PATH + DELIVERY_MARKER_SUFFIX, written)
        # The user is NOT left hanging: the agent is told to read it itself.
        turn = mocks["post"].call_args.kwargs["json"]["messages"][0]["content"]
        self.assertIn("pdf tool", turn)

    def test_unreadable_document_still_produces_an_honest_turn(self):
        # Extraction blowing up must never end in silence — failure honesty is a
        # hard requirement of the directive.
        result, mocks = self._run(pdf_bytes=b"not a pdf at all")

        self.assertEqual(result["status"], "failed")
        turn = mocks["post"].call_args.kwargs["json"]["messages"][0]["content"]
        self.assertIn("pdf tool", turn)

    def test_missing_share_file_produces_an_honest_turn(self):
        result, mocks = self._run(pdf_bytes=None)

        self.assertEqual(result["status"], "missing")
        self.assertIn("pdf tool", mocks["post"].call_args.kwargs["json"]["messages"][0]["content"])

    def test_failed_delivery_raises_and_leaves_no_done_marker(self):
        # QStash retries on the raise. If the marker had been written first, the
        # retry would short-circuit and the user would wait forever for a
        # follow-up that can never be sent.
        with (
            patch("apps.orchestrator.azure_client.download_workspace_file", return_value=None),
            patch("apps.orchestrator.azure_client.download_workspace_file_binary", return_value=TEXT_LAYER_PDF),
            patch("apps.orchestrator.azure_client.upload_workspace_file") as upload,
            patch("apps.router.tasks.httpx.post", MagicMock(side_effect=RuntimeError("gateway down"))),
        ):
            with self.assertRaises(RuntimeError):
                extract_inbound_document_task(str(self.tenant.id), _DOC_PATH, "t1")
            written = {call.args[1] for call in upload.call_args_list}
        self.assertNotIn(_DOC_PATH + DELIVERY_MARKER_SUFFIX, written)

    def test_inactive_tenant_is_skipped_without_touching_the_share(self):
        self.tenant.status = Tenant.Status.SUSPENDED
        self.tenant.save(update_fields=["status"])

        result, mocks = self._run()

        self.assertEqual(result["status"], "not_active")
        mocks["post"].assert_not_called()
        mocks["download"].assert_not_called()

    def test_unknown_tenant_is_skipped(self):
        with patch("apps.router.tasks.httpx.post") as post:
            result = extract_inbound_document_task("00000000-0000-4000-8000-000000000000", _DOC_PATH, "t1")
        self.assertEqual(result["status"], "no_tenant")
        post.assert_not_called()


@override_settings(
    NBHD_INTERNAL_API_KEY="test-key",
    QSTASH_TOKEN="test-qstash-token",
    API_BASE_URL="https://api.example.com",
)
class IngressEnqueueAndMarkerTest(TestCase):
    """The ingress side: publish the task, and tell the agent what to expect."""

    _FAKE_STORE = (
        "/home/node/.openclaw/workspace/media/inbound/doc_test.pdf",
        "workspace/media/inbound/doc_test.pdf",
    )

    def setUp(self):
        from rest_framework.test import APIClient

        self.tenant = _make_tenant()
        self.client = APIClient()
        self.client.force_authenticate(user=self.tenant.user)

    def _post_document(self, *, client_msg_id: str, publish_side_effect=None):
        """POST a PDF with QStash publishing mocked out.

        Mocking ``publish_task`` stops the drain from running too, which is what
        we want here: the ``PendingMessage`` row survives the request (the drain
        is what hard-deletes it), so the queued marker text can be read straight
        from the database instead of snapshotted mid-flight.
        """
        import base64

        from apps.router.models import PendingMessage

        with (
            patch("apps.router.chat_views.store_inbound_document", return_value=self._FAKE_STORE),
            patch("apps.cron.publish.publish_task", side_effect=publish_side_effect) as publish,
        ):
            resp = self.client.post(
                "/api/v1/chat/messages/",
                {
                    "text": "summarize this",
                    "document": base64.b64encode(TEXT_LAYER_PDF).decode(),
                    "client_msg_id": client_msg_id,
                },
                format="json",
            )

        row = PendingMessage.objects.filter(tenant=self.tenant).order_by("created_at").last()
        extraction_calls = [c for c in publish.call_args_list if c.args and c.args[0] == "extract_inbound_document"]
        return resp, row, extraction_calls

    def test_upload_publishes_the_extraction_task_with_a_safe_dedup_id(self):
        resp, _, extraction_calls = self._post_document(client_msg_id="docx1")
        self.assertEqual(resp.status_code, 201, resp.content)

        self.assertEqual(len(extraction_calls), 1)
        args, kwargs = extraction_calls[0]
        self.assertEqual(args[1], str(self.tenant.id))
        self.assertEqual(args[2], "workspace/media/inbound/doc_test.pdf")
        # The thread id rides along so the delivery turn lands in the session
        # holding the user's original request.
        self.assertTrue(args[3])
        self.assertNotIn(":", kwargs["idempotency_key"])

    def test_marker_tells_the_agent_to_wait_rather_than_read(self):
        _, row, _ = self._post_document(client_msg_id="docx2")

        text = row.payload["message_text"]
        self.assertIn("extraction is running in the background", text)
        self.assertIn("do NOT call the pdf tool", text)
        # The untrusted-content framing is never traded away for the new copy.
        self.assertIn("UNTRUSTED DATA", text)

    def test_publish_failure_degrades_to_the_original_in_turn_marker(self):
        # An "extraction in progress" promise we can't keep would leave the
        # agent waiting on a follow-up that never arrives. Fall back to the
        # slow-but-correct in-turn read instead.
        resp, row, _ = self._post_document(
            client_msg_id="docx3",
            publish_side_effect=RuntimeError("qstash down"),
        )

        # The turn still goes through — a broken publish is not a broken upload.
        self.assertEqual(resp.status_code, 201, resp.content)
        text = row.payload["message_text"]
        self.assertNotIn("extraction is running in the background", text)
        self.assertIn("[Document attached:", text)


class TaskRegistrationTest(TestCase):
    def test_task_is_reachable_from_the_qstash_trigger_map(self):
        # A published task whose name isn't in TASK_MAP 400s at the trigger
        # endpoint and dies in DLQ — silently, from the user's point of view.
        from apps.cron.views import TASK_MAP

        self.assertEqual(
            TASK_MAP["extract_inbound_document"],
            "apps.router.tasks.extract_inbound_document_task",
        )
