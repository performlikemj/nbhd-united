"""P3 W2b — the Document family through the Layer-1 authoring chokepoint.

Three properties, per writer class, per seam:

1. **Flag OFF preserves pre-P3 behavior.** Runtime/background writes stay
   byte-identical passthroughs; owner writes keep the legacy unchecked
   redaction they already ran (``bypass``/``legacy-redact`` receipt).
2. **Flag ON stores placeholder-space text plus an honest receipt.** Owner
   writes mint; runtime request writes mask known values synchronously and
   defer deep classification under an ``unconfirmed`` receipt.
3. **The owner still sees real values** — reads rehydrate and carry per-field
   receipts, and searches for a real name still find the placeholder-stored
   document.

``_detect_pii`` is patched to return nothing in most tests so what is asserted
is the CHOKEPOINT's behavior (known-value substitution, minting policy, receipt
shape), not the local DeBERTa model's opinion of a fixture sentence. Tests that
specifically exercise detection say so.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.journal.document_authoring import as_fts_phrase, merge_field_receipt
from apps.journal.document_views import _default_markdown
from apps.journal.models import Document, DocumentChunk, DocumentIngestion, DocumentIngestionArtifact
from apps.pii.testsupport import neural_ran
from apps.tenants.models import Tenant, User
from apps.tenants.test_utils import seed_internal_key

_ENTITY_MAP = {"[PERSON_1]": {"name": "Alice"}}


class _DocumentPiiBase(TestCase):
    flag_on = True

    def setUp(self):
        self.user = User.objects.create_user(username=f"doc-pii-{id(self)}", password="x")
        self.tenant = Tenant.objects.create(
            user=self.user,
            status="active",
            layer1_placeholder_writes=self.flag_on,
            pii_entity_map=dict(_ENTITY_MAP),
        )
        seed_internal_key(self.tenant)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _runtime_headers(self):
        return {
            "HTTP_X_NBHD_INTERNAL_KEY": "test-runtime-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }


# ── Owner writes ───────────────────────────────────────────────────────────


class OwnerDocumentWriteTests(_DocumentPiiBase):
    def test_post_create_no_longer_stores_owner_text_raw(self):
        """The named "owner POST raw fix".

        POST was the one owner Document write that persisted its body verbatim
        while PATCH and append both re-redacted, so a document CREATED with a
        real name handed that name to the agent.
        """
        with patch("apps.pii.redactor._detect_pii", side_effect=neural_ran([])):
            resp = self.client.post(
                "/api/v1/journal/documents/",
                {"kind": "project", "slug": "reno", "title": "Alice reno", "markdown": "Call Alice today"},
                format="json",
            )

        self.assertEqual(resp.status_code, 201)
        doc = Document.objects.get(tenant=self.tenant, kind="project", slug="reno")
        self.assertEqual(doc.markdown, "Call [PERSON_1] today")
        self.assertEqual(doc.title, "[PERSON_1] reno")
        self.assertEqual(doc.pii_receipts["markdown"]["state"], "placeholder")
        self.assertEqual(doc.pii_receipts["markdown"]["writer"], "owner")
        self.assertEqual(doc.pii_receipts["title"]["redactions"], [{"placeholder": "[PERSON_1]"}])
        # The owner still reads their own names back.
        self.assertEqual(resp.data["markdown"], "Call Alice today")
        self.assertEqual(
            resp.data["pii_receipts"]["markdown"]["redactions"],
            [{"placeholder": "[PERSON_1]", "value": "Alice"}],
        )

    def test_post_create_without_body_earns_no_markdown_receipt(self):
        """A server-rendered template body is not owner text — no false receipt."""
        with patch("apps.pii.redactor._detect_pii", return_value=[]):
            resp = self.client.post(
                "/api/v1/journal/documents/",
                {"kind": "ideas", "slug": "ideas", "title": "Ideas"},
                format="json",
            )

        self.assertEqual(resp.status_code, 201)
        doc = Document.objects.get(tenant=self.tenant, kind="ideas", slug="ideas")
        self.assertIn("title", doc.pii_receipts)
        self.assertNotIn("markdown", doc.pii_receipts)

    def test_title_growth_truncates_without_splitting_a_placeholder(self):
        """Authoring makes text LONGER: "Alice" (5) becomes "[PERSON_1]" (10).

        A title that fit the 256-char column before authoring must still fit
        after, and the cut must never leave half a ``[PERSON_`` token behind.
        The receipt describes the STORED text, so it lists only placeholders
        that survived the cut.
        """
        # 24 mentions × +5 chars each pushes a 250-char title past the column.
        title = ("Alice " * 41).strip()
        self.assertLessEqual(len(title), 256)
        with patch("apps.pii.redactor._detect_pii", return_value=[]):
            self.client.post(
                "/api/v1/journal/documents/",
                {"kind": "project", "slug": "long", "title": title},
                format="json",
            )

        doc = Document.objects.get(tenant=self.tenant, kind="project", slug="long")
        self.assertLessEqual(len(doc.title), 256)
        self.assertNotIn("[PERSON_", doc.title.split("]")[-1])
        self.assertEqual(doc.pii_receipts["title"]["redactions"], [{"placeholder": "[PERSON_1]"}])

    def test_patch_replaces_receipt_for_the_whole_field(self):
        Document.objects.create(
            tenant=self.tenant,
            kind="project",
            slug="reno",
            title="Reno",
            markdown="old body",
            pii_receipts={"markdown": {"state": "placeholder", "redactions": [], "writer": "owner"}},
        )
        with patch("apps.pii.redactor._detect_pii", return_value=[]):
            resp = self.client.patch(
                "/api/v1/journal/documents/project/reno/",
                {"markdown": "Alice replaced it"},
                format="json",
            )

        self.assertEqual(resp.status_code, 200)
        doc = Document.objects.get(tenant=self.tenant, kind="project", slug="reno")
        self.assertEqual(doc.markdown, "[PERSON_1] replaced it")
        self.assertEqual(doc.pii_receipts["markdown"]["redactions"], [{"placeholder": "[PERSON_1]"}])

    def test_append_merges_the_fragment_receipt_over_the_whole_field(self):
        """Only the fragment is authored; the receipt still covers the column."""
        Document.objects.create(
            tenant=self.tenant,
            kind="daily",
            slug="2026-08-08",
            title="Day",
            markdown="Earlier: [PERSON_1] called",
            pii_receipts={
                "markdown": {
                    "state": "placeholder",
                    "redactions": [{"placeholder": "[PERSON_1]"}],
                    "writer": "owner",
                }
            },
        )
        with patch("apps.pii.redactor._detect_pii", side_effect=neural_ran([])):
            resp = self.client.post(
                "/api/v1/journal/documents/daily/2026-08-08/append/",
                {"content": "Alice again", "time": "10:00"},
                format="json",
            )

        self.assertEqual(resp.status_code, 201)
        doc = Document.objects.get(tenant=self.tenant, kind="daily", slug="2026-08-08")
        self.assertIn("[PERSON_1] again", doc.markdown)
        self.assertNotIn("Alice", doc.markdown)
        # One entry per distinct placeholder across the WHOLE body, not just the
        # appended fragment.
        self.assertEqual(doc.pii_receipts["markdown"]["redactions"], [{"placeholder": "[PERSON_1]"}])
        self.assertEqual(doc.pii_receipts["markdown"]["state"], "placeholder")

    def test_clear_resets_the_markdown_receipt(self):
        Document.objects.create(
            tenant=self.tenant,
            kind="ideas",
            slug="ideas",
            title="Ideas",
            markdown="[PERSON_1] idea",
            pii_receipts={
                "markdown": {
                    "state": "placeholder",
                    "redactions": [{"placeholder": "[PERSON_1]"}],
                    "writer": "owner",
                }
            },
        )
        resp = self.client.post("/api/v1/journal/documents/ideas/ideas/clear/")
        self.assertEqual(resp.status_code, 200)
        doc = Document.objects.get(tenant=self.tenant, kind="ideas", slug="ideas")
        self.assertEqual(doc.markdown, "")
        self.assertEqual(doc.pii_receipts["markdown"]["redactions"], [])


class OwnerDocumentWriteFlagOffTests(_DocumentPiiBase):
    flag_on = False

    def test_patch_keeps_legacy_unchecked_redaction(self):
        """A4: flag-off owner writes behave exactly as they did pre-P3."""
        with patch("apps.pii.redactor._detect_pii", return_value=[]):
            self.client.patch(
                "/api/v1/journal/documents/project/reno/",
                {"markdown": "Call Alice"},
                format="json",
            )

        doc = Document.objects.get(tenant=self.tenant, kind="project", slug="reno")
        self.assertEqual(doc.markdown, "Call [PERSON_1]")
        self.assertEqual(
            doc.pii_receipts["markdown"],
            {"state": "bypass", "mode": "legacy-redact", "writer": "owner"},
        )

    def test_default_body_is_a_byte_identical_passthrough(self):
        """A4: creating a daily note runs NO detection while the flag is off.

        The default body is authored as BACKGROUND, not owner. Owner would mean
        the legacy redactor here — real detection and real minting on a path
        that had neither before P3, on every first touch of every daily note.
        """
        template = _default_markdown("daily", "2026-08-08", tenant=self.tenant)
        before_map = dict(self.tenant.pii_entity_map)

        with patch("apps.pii.redactor._detect_pii") as detect:
            resp = self.client.post(
                "/api/v1/journal/documents/daily/2026-08-08/append/",
                {"content": "note", "time": "09:00"},
                format="json",
            )

        self.assertEqual(resp.status_code, 201)
        doc = Document.objects.get(tenant=self.tenant, kind="daily", slug="2026-08-08")
        # The template survives byte-for-byte at the head of the column.
        self.assertTrue(doc.markdown.startswith(template.rstrip()))
        self.assertEqual(doc.pii_receipts["markdown"]["state"], "bypass")
        # The DEFAULT BODY never reached the detector. The appended fragment
        # does — owner writes redacted before P3 and A4 keeps that — so this
        # asserts on the arguments rather than the call count.
        detected_texts = [call.args[0] for call in detect.call_args_list if call.args]
        self.assertNotIn(template, detected_texts)
        self.assertEqual(detected_texts, ["note"])
        # And nothing was minted from the template either.
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.pii_entity_map, before_map)

    def test_long_owner_title_still_fits_its_column_after_legacy_redaction(self):
        """Legacy redaction grows text too — "Alice" (5) → "[PERSON_1]" (10).

        The flag-off owner branch was passing the grown string straight to a
        CharField(256) and 500ing on the insert.
        """
        title = ("Alice " * 41).strip()
        self.assertLessEqual(len(title), 256)

        with patch("apps.pii.redactor._detect_pii", return_value=[]):
            resp = self.client.post(
                "/api/v1/journal/documents/",
                {"kind": "project", "slug": "longoff", "title": title},
                format="json",
            )

        self.assertEqual(resp.status_code, 201)
        doc = Document.objects.get(tenant=self.tenant, kind="project", slug="longoff")
        self.assertLessEqual(len(doc.title), 256)
        self.assertNotIn("[PERSON_", doc.title.split("]")[-1])

    def test_post_create_gets_the_same_legacy_redaction_as_patch(self):
        """The POST fix is deliberately unconditional.

        Flag-off does not introduce NEW detection anywhere else, but leaving
        create raw would keep a live leak open for every tenant not yet on the
        flag — and it only brings create up to what PATCH already did.
        """
        with patch("apps.pii.redactor._detect_pii", return_value=[]):
            self.client.post(
                "/api/v1/journal/documents/",
                {"kind": "project", "slug": "reno2", "title": "T", "markdown": "Call Alice"},
                format="json",
            )

        doc = Document.objects.get(tenant=self.tenant, kind="project", slug="reno2")
        self.assertEqual(doc.markdown, "Call [PERSON_1]")
        self.assertEqual(doc.pii_receipts["markdown"]["state"], "bypass")


# ── Runtime (M2M) writes ───────────────────────────────────────────────────


@override_settings(NBHD_INTERNAL_API_KEY="test-runtime-key")
class RuntimeDocumentWriteTests(_DocumentPiiBase):
    def test_put_substitutes_known_values_and_never_mints(self):
        with patch("apps.pii.redactor._detect_pii", return_value=[]):
            resp = self.client.put(
                f"/api/v1/integrations/runtime/{self.tenant.id}/document/",
                {"kind": "project", "slug": "reno", "title": "Alice reno", "markdown": "Alice and Bob met"},
                format="json",
                **self._runtime_headers(),
            )

        self.assertEqual(resp.status_code, 201)
        doc = Document.objects.get(tenant=self.tenant, kind="project", slug="reno")
        # Known value substituted; "Bob" has no binding and MINT_NEVER means the
        # runtime class does not coin one.
        self.assertEqual(doc.markdown, "[PERSON_1] and Bob met")
        self.assertEqual(doc.title, "[PERSON_1] reno")
        self.assertEqual(doc.pii_receipts["markdown"]["writer"], "runtime")
        self.assertEqual(self.tenant.pii_entity_map, _ENTITY_MAP)
        # The runtime read stays in placeholder space — no rehydration.
        self.assertEqual(resp.data["markdown"], "[PERSON_1] and Bob met")

    def test_put_defers_classification_of_a_model_composed_name(self):
        """Unknown runtime text stays repair-eligible without request-path NER."""
        detection = [type("R", (), {"entity_type": "PERSON", "start": 0, "end": 3, "score": 0.99})()]
        with (
            patch("apps.pii.redactor._detect_pii", return_value=[]) as redactor_detect,
            patch("apps.pii.authoring._detect_pii", return_value=detection) as authoring_detect,
        ):
            self.client.put(
                f"/api/v1/integrations/runtime/{self.tenant.id}/document/",
                {"kind": "project", "slug": "reno", "markdown": "Bob was here"},
                format="json",
                **self._runtime_headers(),
            )

        doc = Document.objects.get(tenant=self.tenant, kind="project", slug="reno")
        self.assertEqual(doc.pii_receipts["markdown"]["state"], "unconfirmed")
        self.assertEqual(doc.pii_receipts["markdown"]["reason"], "detector-deferred")
        redactor_detect.assert_not_called()
        authoring_detect.assert_not_called()

    def test_document_append_merges_receipt(self):
        Document.objects.create(tenant=self.tenant, kind="project", slug="reno", title="R", markdown="start")
        with patch("apps.pii.redactor._detect_pii", return_value=[]):
            resp = self.client.post(
                f"/api/v1/integrations/runtime/{self.tenant.id}/document/append/",
                {"kind": "project", "slug": "reno", "content": "Alice stopped by"},
                format="json",
                **self._runtime_headers(),
            )

        self.assertEqual(resp.status_code, 201)
        doc = Document.objects.get(tenant=self.tenant, kind="project", slug="reno")
        self.assertIn("[PERSON_1] stopped by", doc.markdown)
        # The deferred fragment makes the whole stored field repair-eligible;
        # an unchecked older half must not read as verified either.
        self.assertEqual(doc.pii_receipts["markdown"]["state"], "unconfirmed")
        self.assertEqual(doc.pii_receipts["markdown"]["reason"], "detector-deferred")

    def test_append_to_a_document_this_seam_created_stays_repair_eligible(self):
        """A first-touch body and fragment both carry deferred provenance.

        Without authoring the default body, the flagship append surface would
        carry a permanent ``bypass`` receipt and the owner would never get entity
        affordances on their daily notes. The runtime request must still avoid
        waiting on the detector, so hourly repair performs deep classification.
        """
        with patch("apps.pii.redactor._detect_pii", return_value=[]):
            self.client.post(
                f"/api/v1/integrations/runtime/{self.tenant.id}/document/append/",
                {"kind": "ideas", "slug": "ideas", "content": "Alice suggested it"},
                format="json",
                **self._runtime_headers(),
            )

        doc = Document.objects.get(tenant=self.tenant, kind="ideas", slug="ideas")
        self.assertEqual(doc.pii_receipts["markdown"]["state"], "unconfirmed")
        self.assertEqual(doc.pii_receipts["markdown"]["reason"], "detector-deferred")
        self.assertEqual(doc.pii_receipts["markdown"]["redactions"], [{"placeholder": "[PERSON_1]"}])

    def test_daily_note_append_authors_the_fragment(self):
        with patch("apps.pii.redactor._detect_pii", return_value=[]):
            resp = self.client.post(
                f"/api/v1/integrations/runtime/{self.tenant.id}/daily-note/append/",
                {"content": "Alice called", "date": "2026-08-08"},
                format="json",
                **self._runtime_headers(),
            )

        self.assertEqual(resp.status_code, 201)
        doc = Document.objects.get(tenant=self.tenant, kind="daily", slug="2026-08-08")
        self.assertIn("[PERSON_1] called", doc.markdown)
        self.assertNotIn("Alice", doc.markdown)
        self.assertEqual(doc.pii_receipts["markdown"]["writer"], "runtime")

    def test_memory_put_whole_document_and_section(self):
        with patch("apps.pii.redactor._detect_pii", return_value=[]):
            self.client.put(
                f"/api/v1/integrations/runtime/{self.tenant.id}/long-term-memory/",
                {"markdown": "## People\n\nAlice is a neighbour\n"},
                format="json",
                **self._runtime_headers(),
            )
            doc = Document.objects.get(tenant=self.tenant, kind="memory", slug="long-term")
            self.assertIn("[PERSON_1] is a neighbour", doc.markdown)
            self.assertEqual(doc.pii_receipts["markdown"]["state"], "unconfirmed")
            self.assertEqual(doc.pii_receipts["markdown"]["reason"], "detector-deferred")

            self.client.put(
                f"/api/v1/integrations/runtime/{self.tenant.id}/long-term-memory/",
                {"markdown": "Alice moved away", "section": "People"},
                format="json",
                **self._runtime_headers(),
            )

        doc.refresh_from_db()
        self.assertIn("[PERSON_1] moved away", doc.markdown)
        self.assertNotIn("Alice", doc.markdown)


@override_settings(NBHD_INTERNAL_API_KEY="test-runtime-key")
class RuntimeDocumentWriteFlagOffTests(_DocumentPiiBase):
    flag_on = False

    def test_put_is_a_byte_identical_passthrough(self):
        with patch("apps.pii.redactor._detect_pii", return_value=[]):
            self.client.put(
                f"/api/v1/integrations/runtime/{self.tenant.id}/document/",
                {"kind": "project", "slug": "reno", "title": "Alice reno", "markdown": "Alice and Bob met"},
                format="json",
                **self._runtime_headers(),
            )

        doc = Document.objects.get(tenant=self.tenant, kind="project", slug="reno")
        self.assertEqual(doc.markdown, "Alice and Bob met")
        self.assertEqual(doc.title, "Alice reno")
        self.assertEqual(doc.pii_receipts["markdown"], {"state": "bypass", "writer": "runtime"})


# ── Background writers ─────────────────────────────────────────────────────


class BackgroundDocumentWriteTests(_DocumentPiiBase):
    def test_reply_artifact_authors_the_body_but_never_the_marker(self):
        from apps.journal.reply_artifacts import upsert_reply_artifact

        with patch("apps.pii.redactor._detect_pii", return_value=[]):
            doc = upsert_reply_artifact(
                tenant=self.tenant,
                source="ios",
                dedup_key="k1",
                title="Alice table",
                markdown="| who |\n| Alice |\n",
            )

        self.assertTrue(doc.markdown.startswith("<!-- nbhd-reply-artifact:v1:"))
        self.assertIn("[PERSON_1]", doc.markdown)
        self.assertNotIn("Alice", doc.markdown)
        self.assertEqual(doc.title, "[PERSON_1] table")
        self.assertEqual(doc.pii_receipts["markdown"]["writer"], "background")

        # Re-upserting the same identity updates in place rather than colliding.
        with patch("apps.pii.redactor._detect_pii", return_value=[]):
            again = upsert_reply_artifact(
                tenant=self.tenant,
                source="ios",
                dedup_key="k1",
                title="Alice table",
                markdown="| who |\n| Alice |\n| Bob |\n",
            )
        self.assertEqual(again.id, doc.id)

    def test_session_project_document_authors_its_title(self):
        from apps.journal.session_views import _ensure_project_document

        with patch("apps.pii.redactor._detect_pii", return_value=[]):
            _ensure_project_document(self.tenant, "Alice renovation")

        doc = Document.objects.get(tenant=self.tenant, kind=Document.Kind.PROJECT)
        self.assertEqual(doc.title, "[PERSON_1] renovation")
        # The slug is identity, deliberately left in its slugified raw form.
        self.assertEqual(doc.slug, "alice-renovation")
        self.assertEqual(doc.pii_receipts["title"]["writer"], "background")

    def test_chunk_derivation_consumes_stored_text_and_earns_a_receipt(self):
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.DAILY,
            slug="2026-08-08",
            title="Day",
            markdown="## Log\n\n" + ("[PERSON_1] came over and we talked for a long while. " * 4),
        )
        with (
            patch("apps.pii.redactor._detect_pii", return_value=[]),
            patch("apps.lessons.services.generate_embedding", return_value=[0.0] * 1536),
        ):
            from apps.journal.embedding import embed_daily_note

            created = embed_daily_note(self.tenant, date(2026, 8, 8))

        self.assertEqual(created, 1)
        chunk = DocumentChunk.objects.get(tenant=self.tenant)
        # The chunk is a copy of the STORED (already-authored) body, so it
        # carries the same placeholders and never the real value.
        self.assertIn("[PERSON_1]", chunk.text)
        self.assertNotIn("Alice", chunk.text)
        self.assertEqual(chunk.pii_receipts["text"]["writer"], "background")

    def test_legacy_extraction_approve_then_undo_round_trips(self):
        """Approve authors the entry — so undo has to scrub the AUTHORED form.

        Matching on the raw text alone would leave the redacted line in the
        document forever while telling the user it was removed.
        """
        from django.utils import timezone as dj_timezone

        from apps.journal.models import PendingExtraction
        from apps.router.extraction_callbacks import _approve_task, _undo_task

        pending = PendingExtraction.objects.create(
            tenant=self.tenant,
            kind=PendingExtraction.Kind.TASK,
            text="Call Alice back",
            expires_at=dj_timezone.now(),
        )
        with patch("apps.pii.redactor._detect_pii", side_effect=neural_ran([])):
            _approve_task(pending)

        doc = Document.objects.get(tenant=self.tenant, kind=Document.Kind.TASKS)
        self.assertIn("- [ ] Call [PERSON_1] back", doc.markdown)
        self.assertNotIn("Alice", doc.markdown)
        # This seam still seeds its own unauthored "# Tasks" stub, so the folded
        # state is the pessimistic one. Deprecated path (typed lifecycle
        # supersedes it) and A7-safe: anything not `placeholder` migrates.
        self.assertEqual(doc.pii_receipts["markdown"]["state"], "bypass")

        _undo_task(pending)
        doc.refresh_from_db()
        self.assertNotIn("[PERSON_1]", doc.markdown)

    def test_ingestion_keep_authors_filename_and_excerpt(self):
        from apps.journal.document_ingestion import record_keep

        target = Document.objects.create(tenant=self.tenant, kind="project", slug="kept", title="Kept", markdown="body")
        with patch("apps.pii.redactor._detect_pii", return_value=[]):
            result = record_keep(
                self.tenant,
                source={"original_filename": "Alice-contract.pdf"},
                artifacts=[
                    {
                        "object_type": "journal.Document",
                        "object_id": str(target.id),
                        "kind": "document",
                        "excerpt": "Signed by Alice on Tuesday",
                    }
                ],
            )

        self.assertEqual(result["recorded"], 1)
        ingestion = DocumentIngestion.objects.get(tenant=self.tenant)
        artifact = DocumentIngestionArtifact.objects.get(tenant=self.tenant)
        self.assertEqual(ingestion.original_filename, "[PERSON_1]-contract.pdf")
        self.assertEqual(ingestion.pii_receipts["original_filename"]["writer"], "background")
        self.assertEqual(artifact.content_excerpt, "Signed by [PERSON_1] on Tuesday")
        self.assertEqual(artifact.pii_receipts["content_excerpt"]["writer"], "background")


class BackgroundDocumentWriteFlagOffTests(_DocumentPiiBase):
    flag_on = False

    def test_reply_artifact_is_a_byte_identical_passthrough(self):
        from apps.journal.reply_artifacts import upsert_reply_artifact

        with patch("apps.pii.redactor._detect_pii", return_value=[]):
            doc = upsert_reply_artifact(
                tenant=self.tenant,
                source="ios",
                dedup_key="k1",
                title="Alice table",
                markdown="| who |\n| Alice |\n",
            )

        self.assertIn("| Alice |", doc.markdown)
        self.assertEqual(doc.title, "Alice table")
        self.assertEqual(doc.pii_receipts["markdown"], {"state": "bypass", "writer": "background"})


# ── Owner reads ────────────────────────────────────────────────────────────


class OwnerDocumentReadTests(_DocumentPiiBase):
    def _make_doc(self, **kwargs):
        defaults = {
            "tenant": self.tenant,
            "kind": "project",
            "slug": "reno",
            "title": "[PERSON_1] reno",
            "markdown": "Call [PERSON_1]",
            "pii_receipts": {
                "title": {"state": "placeholder", "redactions": [{"placeholder": "[PERSON_1]"}], "writer": "owner"},
                "markdown": {"state": "placeholder", "redactions": [{"placeholder": "[PERSON_1]"}], "writer": "owner"},
            },
        }
        defaults.update(kwargs)
        return Document.objects.create(**defaults)

    def test_memory_view_rehydrates_and_carries_receipts(self):
        """This view returned ``doc.markdown`` RAW — the owner saw placeholders."""
        Document.objects.create(
            tenant=self.tenant,
            kind="memory",
            slug="long-term",
            title="Memory",
            markdown="[PERSON_1] lives next door",
            pii_receipts={
                "markdown": {
                    "state": "placeholder",
                    "redactions": [{"placeholder": "[PERSON_1]"}],
                    "writer": "runtime",
                }
            },
        )
        resp = self.client.get("/api/v1/journal/memory/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["markdown"], "Alice lives next door")
        self.assertEqual(
            resp.data["pii_receipts"]["markdown"]["redactions"],
            [{"placeholder": "[PERSON_1]", "value": "Alice"}],
        )

    def test_memory_view_put_authors_the_round_tripped_real_value(self):
        with patch("apps.pii.redactor._detect_pii", return_value=[]):
            resp = self.client.put(
                "/api/v1/journal/memory/",
                {"markdown": "Alice lives next door"},
                format="json",
            )

        self.assertEqual(resp.status_code, 200)
        doc = Document.objects.get(tenant=self.tenant, kind="memory", slug="long-term")
        self.assertEqual(doc.markdown, "[PERSON_1] lives next door")
        self.assertEqual(resp.data["markdown"], "Alice lives next door")

    def test_memory_round_trip_preserves_a_tombstoned_binding(self):
        """The memory editor renders what it will be handed back to save.

        A retired binding rehydrated here would come back as a real name on
        save with nothing left to substitute it — retirement removes the value
        from the known-value path — so the owner DELETING an entity would be the
        one action that writes its name back in plaintext. Active bindings
        rehydrate; a tombstoned one stays a literal token and round-trips
        byte-for-byte.
        """
        self.tenant.pii_entity_map = {
            "[PERSON_1]": {"name": "Alice"},
            "[PERSON_2]": {"name": "Bob", "retired": True},
        }
        self.tenant.save(update_fields=["pii_entity_map"])
        Document.objects.create(
            tenant=self.tenant,
            kind="memory",
            slug="long-term",
            title="Memory",
            markdown="[PERSON_1] knows [PERSON_2]",
        )
        map_before = dict(self.tenant.pii_entity_map)

        Document.objects.filter(tenant=self.tenant, kind="memory").update(
            pii_receipts={
                "markdown": {
                    "state": "placeholder",
                    "redactions": [{"placeholder": "[PERSON_1]"}, {"placeholder": "[PERSON_2]"}],
                    "writer": "runtime",
                }
            }
        )

        get_body = self.client.get("/api/v1/journal/memory/").data
        served = get_body["markdown"]
        # Active rehydrated, tombstoned left as the literal token.
        self.assertEqual(served, "Alice knows [PERSON_2]")
        # 0. Body and receipts agree: the active placeholder carries its value,
        # the tombstoned one carries NO value key — otherwise the client would
        # draw a "Bob" chip over a token the body shows literally.
        self.assertEqual(
            get_body["pii_receipts"]["markdown"]["redactions"],
            [{"placeholder": "[PERSON_1]", "value": "Alice"}, {"placeholder": "[PERSON_2]"}],
        )

        with patch("apps.pii.redactor._detect_pii", return_value=[]):
            put = self.client.put("/api/v1/journal/memory/", {"markdown": served}, format="json")
        self.assertEqual(put.status_code, 200)

        doc = Document.objects.get(tenant=self.tenant, kind="memory", slug="long-term")
        self.tenant.refresh_from_db()
        # 1. Nothing raw at rest — neither the active name nor the retired one.
        self.assertEqual(doc.markdown, "[PERSON_1] knows [PERSON_2]")
        self.assertNotIn("Alice", doc.markdown)
        self.assertNotIn("Bob", doc.markdown)
        # 2. No fresh mint: the map is byte-identical, same binding count.
        self.assertEqual(self.tenant.pii_entity_map, map_before)
        self.assertEqual(len(self.tenant.pii_entity_map), 2)
        # 3. The tombstone survived the round trip as a tombstone.
        self.assertTrue(self.tenant.pii_entity_map["[PERSON_2]"]["retired"])

    def test_detail_list_today_and_sidebar_all_carry_receipts(self):
        self._make_doc()

        detail = self.client.get("/api/v1/journal/documents/project/reno/")
        self.assertEqual(detail.data["title"], "Alice reno")
        self.assertEqual(
            detail.data["pii_receipts"]["markdown"]["redactions"],
            [{"placeholder": "[PERSON_1]", "value": "Alice"}],
        )

        listed = self.client.get("/api/v1/journal/documents/?kind=project").data[0]
        self.assertEqual(listed["title"], "Alice reno")
        self.assertEqual(
            listed["pii_receipts"]["title"]["redactions"],
            [{"placeholder": "[PERSON_1]", "value": "Alice"}],
        )
        # The list serializer omits the body, so it must not ship a body receipt.
        self.assertNotIn("markdown", listed["pii_receipts"])

        today = self.client.get("/api/v1/journal/today/")
        self.assertIn("pii_receipts", today.data)

        sidebar = self.client.get("/api/v1/journal/tree/")
        projects = [section for section in sidebar.data if section["kind"] == "project"][0]
        self.assertEqual(projects["items"][0]["title"], "Alice reno")
        self.assertEqual(
            projects["items"][0]["pii_receipts"]["title"]["redactions"],
            [{"placeholder": "[PERSON_1]", "value": "Alice"}],
        )

    def test_receipt_values_come_from_the_live_map_not_the_stored_receipt(self):
        self._make_doc(
            pii_receipts={
                "title": {
                    "state": "placeholder",
                    "redactions": [{"placeholder": "[PERSON_1]", "value": "STALE"}],
                    "writer": "owner",
                }
            }
        )
        detail = self.client.get("/api/v1/journal/documents/project/reno/")
        self.assertEqual(
            detail.data["pii_receipts"]["title"]["redactions"],
            [{"placeholder": "[PERSON_1]", "value": "Alice"}],
        )

    def test_unbound_placeholder_omits_the_value_key(self):
        self._make_doc(
            title="[PERSON_9] reno",
            pii_receipts={
                "title": {"state": "placeholder", "redactions": [{"placeholder": "[PERSON_9]"}], "writer": "owner"}
            },
        )
        detail = self.client.get("/api/v1/journal/documents/project/reno/")
        self.assertEqual(detail.data["pii_receipts"]["title"]["redactions"], [{"placeholder": "[PERSON_9]"}])


# ── Search translation (A5) ────────────────────────────────────────────────


@override_settings(NBHD_INTERNAL_API_KEY="test-runtime-key")
class DocumentSearchVariantTests(_DocumentPiiBase):
    def setUp(self):
        super().setUp()
        Document.objects.create(
            tenant=self.tenant,
            kind="project",
            slug="reno",
            title="Renovation",
            markdown="[PERSON_1] is doing the kitchen",
        )

    def test_fts_finds_a_placeholder_stored_doc_by_the_real_name(self):
        resp = self.client.get(
            f"/api/v1/integrations/runtime/{self.tenant.id}/journal/search/?q=Alice",
            **self._runtime_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([r["slug"] for r in resp.data["results"]], ["reno"])
        # The snippet anchors on the matched VARIANT, not the real-name query
        # that appears nowhere in the stored body.
        self.assertIn("[PERSON_1]", resp.data["results"][0]["snippet"])

    def test_fts_does_not_match_a_different_persons_placeholder(self):
        """Postgres lexes ``[PERSON_4]`` into ``person`` + ``4`` and ANDs them.

        Unquoted, a query variant for [PERSON_4] matches any document holding
        SOME ``[PERSON_…]`` token and SOME ``…_4`` token anywhere — a different
        person entirely. Quoting each token makes it an adjacency phrase.
        """
        self.tenant.pii_entity_map = {
            "[PERSON_1]": {"name": "Alice"},
            "[PERSON_4]": {"name": "Dana"},
        }
        self.tenant.save(update_fields=["pii_entity_map"])
        # Neighbour doc: has a PERSON token and a _4 token, but never [PERSON_4].
        Document.objects.create(
            tenant=self.tenant,
            kind="project",
            slug="decoy",
            title="Decoy",
            markdown="[PERSON_11] emailed [EMAIL_ADDRESS_4] about it",
        )
        Document.objects.create(
            tenant=self.tenant,
            kind="project",
            slug="real",
            title="Real",
            markdown="[PERSON_4] signed the lease",
        )

        resp = self.client.get(
            f"/api/v1/integrations/runtime/{self.tenant.id}/journal/search/?q=Dana",
            **self._runtime_headers(),
        )
        slugs = [r["slug"] for r in resp.data["results"]]
        self.assertIn("real", slugs)
        self.assertNotIn("decoy", slugs)

    def test_as_fts_phrase_quotes_multi_word_placeholder_types(self):
        """TYPE can itself contain underscores — EMAIL_ADDRESS, CREDIT_CARD.

        A ``[A-Z]+`` character class silently skips exactly those, leaving the
        types most likely to carry a real secret unquoted.
        """
        self.assertEqual(as_fts_phrase("[PERSON_4]"), '"[PERSON_4]"')
        self.assertEqual(as_fts_phrase("[EMAIL_ADDRESS_4]"), '"[EMAIL_ADDRESS_4]"')
        self.assertEqual(as_fts_phrase("call [PERSON_4] now"), 'call "[PERSON_4]" now')
        # A query with no placeholder is untouched.
        self.assertEqual(as_fts_phrase("kitchen renovation"), "kitchen renovation")

    def test_strict_match_wins_and_the_recall_floor_stays_unused(self):
        """When ``@@`` finds anything, the loose predicate never runs.

        The decoy scores a non-zero ``ts_rank`` for a ``[PERSON_4]`` variant
        (loose ``person`` + ``4`` overlap), so if the fallback fired whenever it
        could it would drag a different person's note back in.
        """
        self.tenant.pii_entity_map = {"[PERSON_4]": {"name": "Dana"}}
        self.tenant.save(update_fields=["pii_entity_map"])
        Document.objects.create(
            tenant=self.tenant,
            kind="project",
            slug="decoy",
            title="Decoy",
            markdown="[PERSON_11] emailed [EMAIL_ADDRESS_4] about it",
        )
        Document.objects.create(
            tenant=self.tenant, kind="project", slug="real", title="Real", markdown="[PERSON_4] signed the lease"
        )

        resp = self.client.get(
            f"/api/v1/integrations/runtime/{self.tenant.id}/journal/search/?q=Dana",
            **self._runtime_headers(),
        )
        slugs = [r["slug"] for r in resp.data["results"]]
        self.assertEqual(slugs, ["real"])
        self.assertNotIn("decoy", slugs)

    def test_recall_floor_answers_a_partial_term_prose_query(self):
        """``websearch_to_tsquery`` ANDs its terms — measured, not assumed.

        "kitchen renovation permits" requires all three, so a note carrying two
        of them matches NOTHING strictly. Rather than answer the agent with an
        empty result set, the pre-P3 ``rank > 0`` predicate runs as a floor.
        """
        Document.objects.create(
            tenant=self.tenant,
            kind="project",
            slug="partial",
            title="Kitchen plan",
            markdown="the kitchen renovation starts in March",
        )

        resp = self.client.get(
            f"/api/v1/integrations/runtime/{self.tenant.id}/journal/search/?q=kitchen+renovation+permits&kind=project",
            **self._runtime_headers(),
        )
        slugs = [r["slug"] for r in resp.data["results"]]
        self.assertIn("partial", slugs)
        # Still ranked, not an unordered dump.
        self.assertGreater(resp.data["results"][0]["rank"], 0.0)

    def test_no_match_either_way_returns_empty(self):
        """Neither pass may degrade into "return the corpus".

        The literal pre-P3 predicate would fail this: ``ts_rank`` returns
        ``1e-20`` rather than 0 for a query sharing no lexeme with the document,
        so ``rank > 0`` passed every row and an unmatched query dumped
        everything the tenant owns.
        """
        resp = self.client.get(
            f"/api/v1/integrations/runtime/{self.tenant.id}/journal/search/?q=xylophone+bassoon",
            **self._runtime_headers(),
        )
        self.assertEqual(resp.data["count"], 0)
        self.assertEqual(resp.data["results"], [])

    def test_snippet_anchors_on_the_quoted_variant_match(self):
        Document.objects.create(
            tenant=self.tenant,
            kind="project",
            slug="long",
            title="Long",
            markdown=("filler paragraph. " * 40) + "and then [PERSON_1] arrived at the end",
        )
        resp = self.client.get(
            f"/api/v1/integrations/runtime/{self.tenant.id}/journal/search/?q=Alice&kind=project",
            **self._runtime_headers(),
        )
        snippet = [r for r in resp.data["results"] if r["slug"] == "long"][0]["snippet"]
        # The snippet followed the placeholder to the tail of the document
        # rather than defaulting to offset 0.
        self.assertIn("[PERSON_1]", snippet)
        self.assertTrue(snippet.startswith("..."))

    def test_fts_still_answers_a_plain_query(self):
        resp = self.client.get(
            f"/api/v1/integrations/runtime/{self.tenant.id}/journal/search/?q=kitchen",
            **self._runtime_headers(),
        )
        self.assertEqual([r["slug"] for r in resp.data["results"]], ["reno"])

    def test_grounding_probe_shares_the_variant_translation(self):
        from apps.orchestrator.grounding_probe import journal_search, probe_grounding

        self.assertEqual([d.slug for d in journal_search(self.tenant, "Alice")], ["reno"])
        report = probe_grounding(self.tenant, "Alice", expect_terms=["kitchen"])
        self.assertTrue(report.grounded)


# ── Receipt folding ────────────────────────────────────────────────────────


@override_settings(NBHD_INTERNAL_API_KEY="test-runtime-key")
class ReplaceLaneReceiptCoherenceTests(_DocumentPiiBase):
    """The replace lanes merge onto the row read UNDER the lock.

    Interleaving: request A authors, request B commits a change to a DIFFERENT
    field, then A saves. A must not write its pre-lock snapshot back over B's
    receipt — B's text would survive next to A's stale receipt for it, and a
    receipt that disagrees with its text rehydrates the wrong binding.
    """

    def _doc(self, **kwargs):
        defaults = {
            "tenant": self.tenant,
            "kind": "project",
            "slug": "reno",
            "title": "original title",
            "markdown": "original body",
            "pii_receipts": {},
        }
        defaults.update(kwargs)
        return Document.objects.create(**defaults)

    def test_owner_patch_preserves_a_concurrent_receipt_for_an_untouched_field(self):
        doc = self._doc()
        b_receipt = {"state": "placeholder", "redactions": [{"placeholder": "[PERSON_1]"}], "writer": "runtime"}

        def _commit_b(*args, **kwargs):
            # Runs while request A is authoring, before A takes the lock.
            Document.objects.filter(pk=doc.pk).update(
                title="[PERSON_1] title",
                pii_receipts={"title": b_receipt},
            )
            return []

        with patch("apps.pii.redactor._detect_pii", side_effect=_commit_b):
            resp = self.client.patch(
                "/api/v1/journal/documents/project/reno/",
                {"markdown": "Alice rewrote the body"},
                format="json",
            )

        self.assertEqual(resp.status_code, 200)
        doc.refresh_from_db()
        # A's field landed…
        self.assertEqual(doc.markdown, "[PERSON_1] rewrote the body")
        self.assertEqual(doc.pii_receipts["markdown"]["writer"], "owner")
        # …and B's text AND its receipt both survived intact.
        self.assertEqual(doc.title, "[PERSON_1] title")
        self.assertEqual(doc.pii_receipts["title"], b_receipt)

    def test_runtime_put_preserves_a_concurrent_receipt_for_an_untouched_field(self):
        doc = self._doc()
        b_receipt = {"state": "residual", "redactions": [], "writer": "background"}

        def _commit_b(_tenant, text, **_kwargs):
            Document.objects.filter(pk=doc.pk).update(
                title="b title",
                pii_receipts={"title": b_receipt},
            )
            return text.replace("Alice", "[PERSON_1]")

        with patch("apps.pii.authoring._redact_active_known_values", side_effect=_commit_b):
            resp = self.client.put(
                f"/api/v1/integrations/runtime/{self.tenant.id}/document/",
                {"kind": "project", "slug": "reno", "markdown": "Alice rewrote it"},
                format="json",
                **self._runtime_headers(),
            )

        self.assertEqual(resp.status_code, 200)
        doc.refresh_from_db()
        self.assertEqual(doc.markdown, "[PERSON_1] rewrote it")
        self.assertEqual(doc.title, "b title")
        self.assertEqual(doc.pii_receipts["title"], b_receipt)

    def test_memory_put_preserves_a_concurrent_receipt_for_an_untouched_field(self):
        doc = self._doc(kind="memory", slug="long-term", title="Memory")
        b_receipt = {"state": "placeholder", "redactions": [], "writer": "runtime"}

        def _commit_b(*args, **kwargs):
            Document.objects.filter(pk=doc.pk).update(pii_receipts={"title": b_receipt})
            return []

        with patch("apps.pii.redactor._detect_pii", side_effect=_commit_b):
            resp = self.client.put("/api/v1/journal/memory/", {"markdown": "Alice note"}, format="json")

        self.assertEqual(resp.status_code, 200)
        doc.refresh_from_db()
        self.assertEqual(doc.markdown, "[PERSON_1] note")
        self.assertEqual(doc.pii_receipts["title"], b_receipt)


class MergeFieldReceiptTests(TestCase):
    def test_a_clean_fragment_never_upgrades_an_unchecked_field(self):
        merged = merge_field_receipt(
            {"markdown": {"state": "bypass", "writer": "owner"}},
            "markdown",
            {"state": "placeholder", "redactions": [], "writer": "runtime"},
            stored_text="old [PERSON_1] plus new",
        )
        self.assertEqual(merged["markdown"]["state"], "bypass")
        # A bypass receipt carries no placeholder data — nothing checked it.
        self.assertNotIn("redactions", merged["markdown"])
        self.assertEqual(merged["markdown"]["writer"], "runtime")

    def test_a_failed_fragment_downgrades_a_clean_field(self):
        merged = merge_field_receipt(
            {"markdown": {"state": "placeholder", "redactions": [], "writer": "owner"}},
            "markdown",
            {"state": "unconfirmed", "reason": "redaction-error", "redactions": [], "writer": "runtime"},
            stored_text="old [PERSON_1] plus new",
        )
        self.assertEqual(merged["markdown"]["state"], "unconfirmed")
        self.assertEqual(merged["markdown"]["reason"], "redaction-error")

    def test_terminal_receipt_is_sticky_against_unconfirmed_append(self):
        terminal = {
            "state": "terminal",
            "terminal_from": "unconfirmed",
            "terminal_reason": "repair-attempts-exhausted",
            "writer": "background",
        }
        merged = merge_field_receipt(
            {"markdown": terminal},
            "markdown",
            {"state": "unconfirmed", "reason": "redaction-error", "redactions": [], "writer": "runtime"},
            stored_text="old body plus raw append",
        )

        self.assertEqual(merged["markdown"]["state"], "terminal")
        self.assertEqual(merged["markdown"]["terminal_reason"], "repair-attempts-exhausted")
        self.assertEqual(merged["markdown"]["writer"], "runtime")

    def test_detector_deferred_append_reactivates_terminal_receipt(self):
        terminal = {
            "state": "terminal",
            "terminal_from": "unconfirmed",
            "terminal_reason": "repair-attempts-exhausted",
            "repair_attempts": 3,
            "writer": "background",
        }
        merged = merge_field_receipt(
            {"markdown": terminal},
            "markdown",
            {
                "state": "unconfirmed",
                "reason": "detector-deferred",
                "redactions": [],
                "writer": "runtime",
            },
            stored_text="old body plus [PERSON_1] in a new append",
        )

        self.assertEqual(
            merged["markdown"],
            {
                "state": "unconfirmed",
                "reason": "detector-deferred",
                "redactions": [{"placeholder": "[PERSON_1]"}],
                "writer": "runtime",
            },
        )

    def test_a_field_with_no_prior_receipt_enters_as_unchecked(self):
        merged = merge_field_receipt(
            {},
            "markdown",
            {"state": "placeholder", "redactions": [], "writer": "runtime"},
            stored_text="[PERSON_1]",
        )
        self.assertEqual(merged["markdown"]["state"], "bypass")

    def test_redactions_are_rebuilt_from_the_whole_column(self):
        merged = merge_field_receipt(
            {"markdown": {"state": "placeholder", "redactions": [{"placeholder": "[PERSON_1]"}], "writer": "owner"}},
            "markdown",
            {"state": "placeholder", "redactions": [{"placeholder": "[PERSON_2]"}], "writer": "owner"},
            stored_text="[PERSON_1] then [PERSON_2]",
        )
        self.assertEqual(
            merged["markdown"]["redactions"],
            [{"placeholder": "[PERSON_1]"}, {"placeholder": "[PERSON_2]"}],
        )
