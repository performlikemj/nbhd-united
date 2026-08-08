"""P3 W2b — the Document family through the Layer-1 authoring chokepoint.

Three properties, per writer class, per seam:

1. **Flag OFF preserves pre-P3 behavior.** Runtime/background writes stay
   byte-identical passthroughs; owner writes keep the legacy unchecked
   redaction they already ran (``bypass``/``legacy-redact`` receipt).
2. **Flag ON stores placeholder-space text plus an honest receipt.** Owner
   writes mint; runtime writes never mint and record what they could not
   redact as ``residual``.
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

from apps.journal.document_authoring import merge_field_receipt
from apps.journal.models import Document, DocumentChunk, DocumentIngestion, DocumentIngestionArtifact
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
        with patch("apps.pii.redactor._detect_pii", return_value=[]):
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
        with patch("apps.pii.redactor._detect_pii", return_value=[]):
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

    def test_put_records_a_model_composed_name_as_residual(self):
        """MINT_NEVER without residual detection would store a raw name under a
        receipt that reads clean forever — the A7 fence skips ``placeholder``."""
        detection = [type("R", (), {"entity_type": "PERSON", "start": 0, "end": 3, "score": 0.99})()]
        with (
            patch("apps.pii.redactor._detect_pii", return_value=[]),
            patch("apps.pii.authoring._detect_pii", return_value=detection),
            patch("apps.pii.authoring._filter_results", side_effect=lambda results, *a, **kw: results),
        ):
            self.client.put(
                f"/api/v1/integrations/runtime/{self.tenant.id}/document/",
                {"kind": "project", "slug": "reno", "markdown": "Bob was here"},
                format="json",
                **self._runtime_headers(),
            )

        doc = Document.objects.get(tenant=self.tenant, kind="project", slug="reno")
        self.assertEqual(doc.pii_receipts["markdown"]["state"], "residual")
        self.assertEqual(doc.pii_receipts["markdown"]["residual_spans"]["kinds"], {"PERSON": 1})

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
        # The pre-existing body carried no receipt, so the merged state is the
        # pessimistic one — an unchecked half must not read as verified.
        self.assertEqual(doc.pii_receipts["markdown"]["state"], "bypass")

    def test_append_to_a_document_this_seam_created_stays_verified(self):
        """The counterpart: a body this seam authored on creation is checked.

        Without authoring the default body, the flagship append surface would
        carry a permanent ``bypass`` receipt and the owner would never get entity
        affordances on their daily notes.
        """
        with patch("apps.pii.redactor._detect_pii", return_value=[]):
            self.client.post(
                f"/api/v1/integrations/runtime/{self.tenant.id}/document/append/",
                {"kind": "ideas", "slug": "ideas", "content": "Alice suggested it"},
                format="json",
                **self._runtime_headers(),
            )

        doc = Document.objects.get(tenant=self.tenant, kind="ideas", slug="ideas")
        self.assertEqual(doc.pii_receipts["markdown"]["state"], "placeholder")
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
            self.assertEqual(doc.pii_receipts["markdown"]["state"], "placeholder")

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
        with patch("apps.pii.redactor._detect_pii", return_value=[]):
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
