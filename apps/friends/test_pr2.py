"""PR2 behavioral tests — the share pipeline, with the fail-closed scrub as the
centerpiece.

The 554MB DeBERTa model is NEVER loaded here: tests patch at the pipeline
boundary (``apps.pii.engine.get_pii_pipeline``) or at the scrub's own seams
(``_assert_ner_available`` / ``_redact_identities``).
"""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied
from rest_framework.test import APIClient

from apps.lessons.models import Lesson
from apps.tenants.models import Tenant, User

from . import access, scrub, services
from .models import Friendship, LessonShareGrant, NeighborProfile, PendingShare, SharedLesson


def _tenant(username: str, *, friends_enabled: bool = True) -> Tenant:
    user = User.objects.create_user(username=username, password="pass", display_name=username.title())
    return Tenant.objects.create(user=user, status="active", friends_enabled=friends_enabled)


def _client(user) -> APIClient:
    c = APIClient()
    c.force_authenticate(user=user)
    return c


def _lesson(tenant, text="Batch-cook Sundays so weeknight-me never negotiates.", *, tags=None, context="") -> Lesson:
    return Lesson.objects.create(
        tenant=tenant, text=text, context=context, source_type="experience", status="approved", tags=tags or []
    )


def _accepted_edge(a, b) -> Friendship:
    return Friendship.objects.create(requester=a, addressee=b, status=Friendship.Status.ACCEPTED)


def _fake_ner_pipe(text):
    """A stand-in DeBERTa pipeline that detects the probe PERSON (so
    ``_assert_ner_available`` passes) without loading the real model."""
    return [{"entity_group": "FIRSTNAME", "score": 0.99, "start": 0, "end": 7}]


# ── The fail-closed scrub (safety-critical) ──────────────────────────────────


class ScrubFailClosedTest(TestCase):
    def setUp(self):
        self.owner = _tenant("scrub_owner")
        self.lesson = _lesson(self.owner, context="from a chat with a friend")
        self.sl = access.ensure_shared_lesson(self.lesson, self.owner)

    def test_neutralizes_placeholders_no_map(self):
        with (
            mock.patch("apps.friends.scrub._assert_ner_available"),
            mock.patch("apps.friends.scrub._assert_output_clean"),
            mock.patch(
                "apps.friends.scrub._redact_identities",
                side_effect=lambda owner, text: "[PERSON_1] cooks with [PERSON_2] on Sundays." if text else "",
            ),
        ):
            result = scrub.scrub_shared_lesson(str(self.sl.id))
        self.assertEqual(result["reason"], "ready")
        self.sl.refresh_from_db()
        self.assertEqual(self.sl.scrub_status, "ready")
        # Every [TYPE_N] placeholder neutralized to a generic word; NO map exists.
        self.assertNotIn("[PERSON", self.sl.redacted_text)
        self.assertIn("someone", self.sl.redacted_text)
        self.assertFalse(hasattr(self.sl, "entity_map"))  # structurally no rehydration map
        self.assertTrue(self.sl.content_hash)
        self.assertEqual(self.sl.scrub_model_version, scrub.SCRUB_MODEL_VERSION)

    def test_fail_closed_when_ner_pipeline_unavailable(self):
        # Simulate the error-cached state: get_pii_pipeline raises. The silent
        # Presidio-only fallback must be treated as a HARD failure — never ready.
        with mock.patch("apps.pii.engine.get_pii_pipeline", side_effect=RuntimeError("model load failed")):
            result = scrub.scrub_shared_lesson(str(self.sl.id))
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "ner_unavailable")
        self.sl.refresh_from_db()
        self.assertEqual(self.sl.scrub_status, "failed")
        self.assertIn("NER", self.sl.scrub_error)
        self.assertEqual(self.sl.redacted_text, "")  # nothing published
        self.assertEqual(LessonShareGrant.objects.count(), 0)  # and never a grant

    def test_fail_closed_when_probe_detects_no_person(self):
        # Model "loads" but the entity pass yields no PERSON (degraded / wrong
        # model) → fail closed, do not fall through to Presidio-only.
        with mock.patch("apps.pii.engine.get_pii_pipeline", return_value=lambda text: []):
            result = scrub.scrub_shared_lesson(str(self.sl.id))
        self.assertEqual(result["reason"], "ner_unavailable")
        self.sl.refresh_from_db()
        self.assertEqual(self.sl.scrub_status, "failed")

    def test_assert_ner_available_raises_on_load_error(self):
        with (
            mock.patch("apps.pii.engine.get_pii_pipeline", side_effect=RuntimeError("gone")),
            self.assertRaises(scrub.NerUnavailable),
        ):
            scrub._assert_ner_available()

    def test_assert_ner_available_passes_with_probe_person(self):
        with mock.patch("apps.pii.engine.get_pii_pipeline", return_value=_fake_ner_pipe):
            scrub._assert_ner_available()  # must not raise

    def test_content_hash_stable_and_drift_detected(self):
        h1 = scrub._content_hash("hello", "world")
        self.assertEqual(h1, scrub._content_hash("hello", "world"))
        self.assertNotEqual(h1, scrub._content_hash("hello", "worlds"))

    def test_fail_closed_when_output_still_has_identities(self):
        """Belt 2: RedactionSession.redact() swallows per-call inference errors
        and returns near-raw text. Simulate that swallow (identity redaction is
        a no-op) with a working pipe that still sees the PERSON in the output —
        the scrub must fail closed, never publish."""

        def detecting_pipe(text):
            if "Sarah" in text:
                return [{"entity_group": "FIRSTNAME", "score": 0.98, "start": 0, "end": 5}]
            return []

        self.lesson.text = "Sarah taught me to batch-cook on Sundays."
        self.lesson.save(update_fields=["text"])
        with (
            mock.patch("apps.friends.scrub._assert_ner_available", return_value=detecting_pipe),
            mock.patch("apps.friends.scrub._redact_identities", side_effect=lambda owner, text: text),
        ):
            result = scrub.scrub_shared_lesson(str(self.sl.id))
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "output_not_clean")
        self.sl.refresh_from_db()
        self.assertEqual(self.sl.scrub_status, "failed")
        self.assertIn("identity entities", self.sl.scrub_error)
        self.assertEqual(self.sl.redacted_text, "")  # nothing published
        self.assertEqual(LessonShareGrant.objects.count(), 0)

    def test_output_check_ignores_low_confidence_and_passes_clean(self):
        """The output belt uses a 0.7 score floor (raw pipe has no redactor
        score tiers) and passes clean text through to ready."""

        def low_confidence_pipe(text):
            return [{"entity_group": "FIRSTNAME", "score": 0.4, "start": 0, "end": 5}] if text else []

        with (
            mock.patch("apps.friends.scrub._assert_ner_available", return_value=low_confidence_pipe),
            mock.patch("apps.friends.scrub._redact_identities", side_effect=lambda owner, text: text),
        ):
            result = scrub.scrub_shared_lesson(str(self.sl.id))
        self.assertTrue(result["ok"])
        self.sl.refresh_from_db()
        self.assertEqual(self.sl.scrub_status, "ready")


# ── Share intent → PendingShare + snapshot (no grant yet) ─────────────────────


class ShareIntentTest(TestCase):
    def setUp(self):
        self.a = _tenant("intent_a")
        self.b = _tenant("intent_b")
        NeighborProfile.objects.create(tenant=self.b, handle="bee", display_name="Bee")
        self.edge = _accepted_edge(self.a, self.b)
        self.lesson = _lesson(self.a)

    def test_share_creates_pending_and_pending_snapshot_no_grant(self):
        with mock.patch("apps.friends.services._enqueue_scrub") as enqueue:
            pending = services.share_lesson(self.a, self.a.user, self.lesson, str(self.edge.id))
        self.assertEqual(pending.proposed_by, "user")
        self.assertEqual(pending.status, "pending")
        sl = access.get_shared_lesson_for_lesson(self.lesson)
        self.assertIsNotNone(sl)
        self.assertEqual(sl.scrub_status, "pending")
        self.assertEqual(LessonShareGrant.objects.count(), 0)  # NO grant at share time
        enqueue.assert_called_once()

    def test_gravity_lesson_refused(self):
        finance_lesson = _lesson(self.a, text="Refi the loan at 4200/mo.", tags=["finance"])
        with self.assertRaises(PermissionDenied):
            services.share_lesson(self.a, self.a.user, finance_lesson, str(self.edge.id))

    def test_core_lesson_refused_over_http(self):
        core_lesson = _lesson(self.a, text="A breathing practice that helps.", tags=["meditation"])
        resp = _client(self.a.user).post(
            f"/api/v1/lessons/{core_lesson.id}/share/", {"friendship_id": str(self.edge.id)}, format="json"
        )
        self.assertEqual(resp.status_code, 403)

    def test_share_to_non_neighbor_refused(self):
        # access.assert_neighbors raises Django's PermissionDenied (DRF's default
        # handler maps it to 403 over HTTP just the same).
        from django.core.exceptions import PermissionDenied as DjangoPermissionDenied

        stranger = _tenant("intent_stranger")
        edge = Friendship.objects.create(requester=self.a, addressee=stranger, status=Friendship.Status.PENDING)
        with self.assertRaises((PermissionDenied, DjangoPermissionDenied)):
            services.share_lesson(self.a, self.a.user, self.lesson, str(edge.id))

    def test_content_hash_drift_reenqueues_scrub(self):
        with mock.patch("apps.friends.services._enqueue_scrub") as enqueue:
            services.share_lesson(self.a, self.a.user, self.lesson, str(self.edge.id))
            sl = access.get_shared_lesson_for_lesson(self.lesson)
            # Mark it ready at the ORIGINAL content hash.
            access.save_scrub_ready(
                sl, redacted_text="someone cooks", content_hash=scrub._content_hash(self.lesson.text, "")
            )
            enqueue.reset_mock()
            # Owner edits the source lesson → hash drifts → re-share re-scrubs.
            self.lesson.text = "Batch-cook Mondays instead."
            self.lesson.save(update_fields=["text"])
            services.share_lesson(self.a, self.a.user, self.lesson, str(self.edge.id))
        enqueue.assert_called_once()


# ── Preview → approve → publish → shared_star_qs; revoke ─────────────────────


class PreviewApprovePublishTest(TestCase):
    def setUp(self):
        self.a = _tenant("pub_a")
        self.b = _tenant("pub_b")
        NeighborProfile.objects.create(tenant=self.b, handle="beep", display_name="Beep")
        self.edge = _accepted_edge(self.a, self.b)
        self.lesson = _lesson(self.a)
        with mock.patch("apps.friends.services._enqueue_scrub"):
            self.pending = services.share_lesson(self.a, self.a.user, self.lesson, str(self.edge.id))
        self.sl = access.get_shared_lesson_for_lesson(self.lesson)

    def _make_ready(self, text="someone batch-cooks on Sundays"):
        access.save_scrub_ready(self.sl, redacted_text=text, content_hash=scrub._content_hash(self.lesson.text, ""))

    def test_preview_202_while_pending(self):
        payload, code = services.preview_share(self.a, self.lesson.id, str(self.edge.id))
        self.assertEqual(code, 202)

    def test_preview_409_when_failed(self):
        access.save_scrub_failed(self.sl, "NER unavailable")
        payload, code = services.preview_share(self.a, self.lesson.id, str(self.edge.id))
        self.assertEqual(code, 409)

    def test_preview_equals_published_bytes_and_visibility(self):
        self._make_ready("someone batch-cooks on Sundays")
        payload, code = services.preview_share(self.a, self.lesson.id, str(self.edge.id))
        self.assertEqual(code, 200)
        self.assertEqual(payload["redacted_text"], "someone batch-cooks on Sundays")
        self.assertEqual(payload["residuals_banner"], "We hide names — but not amounts, dates, or company names.")
        self.assertEqual(payload["audience"], "Beep")

        # Not visible to the neighbor until approved (no grant yet).
        self.assertEqual(access.shared_star_qs(self.b, self.a).count(), 0)

        result, acode = services.approve_share(self.a, str(self.pending.id))
        self.assertEqual(acode, 200)
        self.assertEqual(result["status"], "approved")

        # Now visible — and the PUBLISHED bytes equal the PREVIEW bytes.
        visible = list(access.shared_star_qs(self.b, self.a))
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0].redacted_text, payload["redacted_text"])

    def test_approve_202_when_still_scrubbing(self):
        result, code = services.approve_share(self.a, str(self.pending.id))
        self.assertEqual(code, 202)
        self.assertEqual(LessonShareGrant.objects.count(), 0)

    def test_approve_409_when_scrub_failed(self):
        access.save_scrub_failed(self.sl, "NER unavailable")
        result, code = services.approve_share(self.a, str(self.pending.id))
        self.assertEqual(code, 409)
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.status, "blocked")
        self.assertEqual(LessonShareGrant.objects.count(), 0)

    def test_reject_creates_no_grant(self):
        self._make_ready()
        services.reject_share(self.a, str(self.pending.id))
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.status, "rejected")
        self.assertEqual(LessonShareGrant.objects.count(), 0)

    def test_revoke_hides_instantly_and_deletes_orphan_snapshot(self):
        self._make_ready()
        services.approve_share(self.a, str(self.pending.id))
        self.assertEqual(access.shared_star_qs(self.b, self.a).count(), 1)
        grant = LessonShareGrant.objects.get()
        services.revoke_share(self.a, self.lesson, str(grant.id))
        self.assertEqual(access.shared_star_qs(self.b, self.a).count(), 0)
        # Snapshot with zero active grants is deleted (zero residue).
        self.assertFalse(SharedLesson.objects.filter(id=self.sl.id).exists())

    def test_edit_reenqueues_rescrub_and_holds_grant(self):
        self._make_ready()
        with mock.patch("apps.friends.services._enqueue_scrub") as enqueue:
            result, code = services.approve_share(self.a, str(self.pending.id), final_text="A cleaner rewrite.")
        self.assertEqual(code, 202)
        self.assertEqual(result["status"], "rescrubbing")
        self.assertEqual(LessonShareGrant.objects.count(), 0)  # no grant until re-preview + approve
        enqueue.assert_called_once()
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.final_text, "A cleaner rewrite.")


# ── Grants: dedup + human-approve-only ────────────────────────────────────────


class GrantInvariantsTest(TestCase):
    def setUp(self):
        self.a = _tenant("grant_a")
        self.b = _tenant("grant_b")
        self.edge = _accepted_edge(self.a, self.b)
        self.lesson = _lesson(self.a)
        self.sl = access.ensure_shared_lesson(self.lesson, self.a)
        access.save_scrub_ready(self.sl, redacted_text="someone did a thing", content_hash="h")

    def test_create_grant_is_idempotent(self):
        g1 = access.create_grant(self.sl, self.edge, granted_by=self.a.user)
        g2 = access.create_grant(self.sl, self.edge, granted_by=self.a.user)
        self.assertEqual(g1.id, g2.id)
        self.assertEqual(LessonShareGrant.objects.filter(shared_lesson=self.sl).count(), 1)

    def test_per_edge_partial_unique_enforced_at_db(self):
        LessonShareGrant.objects.create(shared_lesson=self.sl, friendship=self.edge)
        with self.assertRaises(IntegrityError), transaction.atomic():
            LessonShareGrant.objects.create(shared_lesson=self.sl, friendship=self.edge)

    def test_agent_pending_share_never_publishes_without_human_approve(self):
        # An agent proposal creates a PendingShare — but NO grant. Only the human
        # approve path creates the grant.
        pending = PendingShare.objects.create(
            tenant=self.a,
            source_lesson=self.lesson,
            proposed_by="agent",
            target_friendship=self.edge,
            status=PendingShare.Status.PENDING,
            expires_at=timezone.now() + timedelta(days=7),
        )
        self.assertEqual(LessonShareGrant.objects.count(), 0)
        # The human approves → grant appears.
        _result, code = services.approve_share(self.a, str(pending.id))
        self.assertEqual(code, 200)
        self.assertEqual(LessonShareGrant.objects.count(), 1)


# ── HTTP surface + flag gating ────────────────────────────────────────────────


class ShareHttpTest(TestCase):
    def setUp(self):
        self.a = _tenant("http_a")
        self.b = _tenant("http_b")
        NeighborProfile.objects.create(tenant=self.b, handle="hbee", display_name="HBee")
        self.edge = _accepted_edge(self.a, self.b)
        self.lesson = _lesson(self.a)

    def test_share_endpoint_creates_pending(self):
        with mock.patch("apps.friends.services._enqueue_scrub"):
            resp = _client(self.a.user).post(
                f"/api/v1/lessons/{self.lesson.id}/share/", {"friendship_id": str(self.edge.id)}, format="json"
            )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["status"], "pending")

    def test_pending_queue_lists_share(self):
        with mock.patch("apps.friends.services._enqueue_scrub"):
            services.share_lesson(self.a, self.a.user, self.lesson, str(self.edge.id))
        resp = _client(self.a.user).get("/api/v1/friends/shares/pending/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), 1)
        self.assertEqual(resp.json()[0]["audience"], "HBee")

    def test_preview_endpoint_flag_off_403(self):
        off = _tenant("http_off", friends_enabled=False)
        resp = _client(off.user).get("/api/v1/friends/shares/preview/?lesson_id=1&friendship_id=x")
        self.assertEqual(resp.status_code, 403)

    def test_revoke_endpoint(self):
        with mock.patch("apps.friends.services._enqueue_scrub"):
            pending = services.share_lesson(self.a, self.a.user, self.lesson, str(self.edge.id))
        sl = access.get_shared_lesson_for_lesson(self.lesson)
        access.save_scrub_ready(sl, redacted_text="someone", content_hash="h")
        services.approve_share(self.a, str(pending.id))
        grant = LessonShareGrant.objects.get()
        resp = _client(self.a.user).delete(f"/api/v1/lessons/{self.lesson.id}/share/{grant.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(access.shared_star_qs(self.b, self.a).count(), 0)


class SharePreviewContract404Test(TestCase):
    """The exact contract the iOS in-app 'Share a spark' flow depends on
    (NeighborProfileSheet → LessonPickerSheet → SharePreviewSheet): the preview
    GET the client polls must never 404 while a share is genuinely in progress.

    Regression for the prod 404 on 2026-07-10 (canary tenant 148ccf1c sharing
    lesson 899 to the accepted MJ↔Kiho edge): the owner's own SharedLesson read
    fail-closed to ``None`` under a transient RLS ``app.tenant_id`` GUC flicker on
    a pooled connection and surfaced as "No share in progress" (404) mid-scrub,
    dead-ending the trust surface. CI runs as a BYPASSRLS role, so these lock the
    *contract* (never 404 while a pending share exists) and the service-context
    read path — not the live-RLS behaviour itself."""

    def setUp(self):
        self.owner = _tenant("preview_owner")
        self.other = _tenant("preview_other")
        self.edge = _accepted_edge(self.owner, self.other)
        self.lesson = _lesson(self.owner)

    def _preview_url(self) -> str:
        # The literal path + query params NeighborhoodViewModel.previewShare builds.
        return f"/api/v1/friends/shares/preview/?lesson_id={self.lesson.id}&friendship_id={self.edge.id}"

    def test_preview_in_progress_is_202_not_404(self):
        # Share started, snapshot still PENDING (scrub not run) → keep polling (202).
        with mock.patch("apps.friends.services._enqueue_scrub"):
            services.share_lesson(self.owner, self.owner.user, self.lesson, str(self.edge.id))
        resp = _client(self.owner.user).get(self._preview_url())
        self.assertEqual(resp.status_code, 202, resp.content)

    def test_preview_404_only_when_no_share_started(self):
        # No share started at all → a genuine 404 is correct.
        resp = _client(self.owner.user).get(self._preview_url())
        self.assertEqual(resp.status_code, 404, resp.content)

    def test_preview_survives_unreadable_snapshot_while_pending_exists(self):
        # Reproduces the prod symptom in the BYPASSRLS test role: the snapshot read
        # comes back empty (here the row is simply absent) while a PendingShare
        # exists → the owner must keep polling (202), never dead-end on a 404.
        with mock.patch("apps.friends.services._enqueue_scrub"):
            services.share_lesson(self.owner, self.owner.user, self.lesson, str(self.edge.id))
        SharedLesson.objects.filter(source_lesson_id=self.lesson.id, owner_tenant=self.owner).delete()
        _payload, code = services.preview_share(self.owner, str(self.lesson.id), friendship_id=str(self.edge.id))
        self.assertEqual(code, 202)

    def test_owner_snapshot_read_uses_service_context(self):
        # (A) The owner reads their OWN snapshot under backstop_service_context so a
        # momentarily-unset app.tenant_id GUC cannot hide it. Guards against a
        # revert to a GUC-dependent read of shared_lessons.
        with mock.patch("apps.friends.services._enqueue_scrub"):
            services.share_lesson(self.owner, self.owner.user, self.lesson, str(self.edge.id))
        with mock.patch("apps.friends.access.backstop_service_context", wraps=access.backstop_service_context) as spy:
            snap = access.get_shared_lesson_by_lesson_id(str(self.lesson.id), self.owner)
        self.assertTrue(spy.called)
        self.assertIsNotNone(snap)
