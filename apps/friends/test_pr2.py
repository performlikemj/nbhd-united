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
        self.sl, _ = access.ensure_shared_lesson(self.lesson, self.owner)

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


class Belt2PrecisionTest(TestCase):
    """Belt-2 must judge the scrubbed output with the SAME deliberate-skip
    semantics the redaction pass applied — never fail-close on policy residue,
    never pass a real leak.

    Pins the prod lesson-899 failure (canary, 2026-07-10): the raw pipeline
    flagged sub-word FRAGMENTS of a tenant-denylisted brand name plus the
    owner's own display name as PERSON (≥0.7) in a *correctly* scrubbed
    output, so every scrub attempt fail-closed and the share was unshareable
    by design contradiction. Synthetic equivalents of the same shape — the raw
    text stays out of fixtures."""

    def setUp(self):
        self.owner = _tenant("belt_owner")
        self.owner.user.display_name = "Mika"
        self.owner.user.save(update_fields=["display_name"])
        self.owner.pii_denylist = {"braveno": {"reason": "brand, not a person"}}
        self.owner.save(update_fields=["pii_denylist"])

    def test_denylisted_brand_fragments_do_not_fail_close(self):
        # The lesson-899 shape: the raw pipe reports sub-word fragments of a
        # coined brand ("B", "ave") — snapping recovers "Braveno", which the
        # tenant explicitly denylisted as not-PII. Policy residue, not a leak.
        out = "Carving out time for project work (Braveno, NBHD) keeps momentum alive."
        i = out.index("Braveno")

        def pipe(text):
            if text != out:
                return []
            return [
                {"entity_group": "FIRSTNAME", "score": 0.86, "start": i, "end": i + 1},
                {"entity_group": "FIRSTNAME", "score": 0.85, "start": i + 2, "end": i + 5},
            ]

        scrub._assert_output_clean(pipe, [out], owner_tenant=self.owner)  # must not raise

    def test_owner_display_name_does_not_fail_close(self):
        # The share publishes AS the owner, name attached — their own name in
        # the text adds zero identity information (mirrors _redact allow_names).
        out = "Mika worked on the project between family pickups."

        def pipe(text):
            return [{"entity_group": "FIRSTNAME", "score": 0.97, "start": 0, "end": 4}] if text == out else []

        scrub._assert_output_clean(pipe, [out], owner_tenant=self.owner)  # must not raise

    def test_real_name_still_fails_closed(self):
        # THE NON-NEGOTIABLE: a real leaked name (redaction degraded) matches
        # no excuse and still fail-closes.
        out = "Talked with Sarah about batch-cooking."
        i = out.index("Sarah")

        def pipe(text):
            return [{"entity_group": "FIRSTNAME", "score": 0.99, "start": i, "end": i + 5}] if text == out else []

        with self.assertRaises(scrub.NerUnavailable):
            scrub._assert_output_clean(pipe, [out], owner_tenant=self.owner)

    def test_real_name_fragment_snaps_and_still_fails_closed(self):
        # A FRAGMENT of a real name can't slip through the snapping path: it
        # expands to the full word, which matches no excuse.
        out = "Talked with Sarah about batch-cooking."
        i = out.index("Sarah")

        def pipe(text):
            return [{"entity_group": "FIRSTNAME", "score": 0.99, "start": i + 1, "end": i + 4}] if text == out else []

        with self.assertRaises(scrub.NerUnavailable):
            scrub._assert_output_clean(pipe, [out], owner_tenant=self.owner)

    def test_no_owner_stays_strict(self):
        # owner_tenant=None applies no owner-specific excuse — strictly MORE
        # fail-closed than the owner-aware call, never less.
        out = "Project work (Braveno) continues."
        i = out.index("Braveno")

        def pipe(text):
            return [{"entity_group": "FIRSTNAME", "score": 0.86, "start": i + 2, "end": i + 5}] if text == out else []

        with self.assertRaises(scrub.NerUnavailable):
            scrub._assert_output_clean(pipe, [out])

    def test_scrub_ends_ready_on_lesson_899_shape(self):
        # End-to-end: the synthetic 899 shape (denylisted brand + owner's own
        # name + a dangling [PERSON_N] placeholder) scrubs to READY, and the
        # placeholder is neutralized — never published raw.
        lesson = _lesson(
            self.owner,
            "Even with family pickup and errands, project work (Braveno, NBHD) keeps momentum alive.",
            context="Mika worked on Braveno and NBHD between pickups with [PERSON_61] at work",
        )
        sl, _ = access.ensure_shared_lesson(lesson, self.owner)

        def fragment_pipe(text):
            hits = []
            for token in ("Braveno", "Mika"):
                j = text.find(token)
                if j >= 0:
                    hits.append({"entity_group": "FIRSTNAME", "score": 0.9, "start": j + 1, "end": j + 3})
            return hits

        with (
            mock.patch("apps.friends.scrub._assert_ner_available", return_value=fragment_pipe),
            mock.patch("apps.friends.scrub._redact_identities", side_effect=lambda owner, text: text),
        ):
            result = scrub.scrub_shared_lesson(str(sl.id))
        self.assertTrue(result["ok"], result)
        sl.refresh_from_db()
        self.assertEqual(sl.scrub_status, "ready")
        self.assertNotIn("[PERSON_61]", sl.redacted_context)


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

    def test_double_submit_share_returns_existing_not_500(self):
        """A rapid double-POST of the same lesson share (prod 2026-07-11: the
        user's first real spark) must not 500. The winner inserts the
        ``shared_lessons`` OneToOne snapshot; the loser's get-or-create of that
        snapshot hits the ``source_lesson`` unique violation. It must return a
        share (never re-raise) and must NOT re-enqueue the winner's scrub.

        FAILS on main's logic: there ``ensure_shared_lesson`` calls
        ``get_or_create``, whose losing INSERT surfaces the ``IntegrityError``
        here (its own re-get rode a request connection that fail-closed in
        prod), so the second POST 500s instead of returning the winner's row."""
        client = _client(self.a.user)
        with mock.patch("apps.friends.services._enqueue_scrub") as enqueue:
            first = client.post(
                f"/api/v1/lessons/{self.lesson.id}/share/",
                {"friendship_id": str(self.edge.id)},
                format="json",
            )
            self.assertEqual(first.status_code, 201, first.content)
            # The loser: its get-or-create of the OneToOne snapshot collides on
            # the source_lesson unique constraint (the winner just inserted it).
            with mock.patch.object(
                SharedLesson.objects, "get_or_create", side_effect=IntegrityError("dup source_lesson")
            ):
                loser = client.post(
                    f"/api/v1/lessons/{self.lesson.id}/share/",
                    {"friendship_id": str(self.edge.id)},
                    format="json",
                )
        self.assertEqual(loser.status_code, 201, loser.content)
        self.assertTrue(loser.json().get("pending_share_id"))
        # One snapshot, one PendingShare per POST, one scrub — no double-enqueue.
        self.assertEqual(access.get_shared_lesson_for_lesson(self.lesson).scrub_status, "pending")
        self.assertEqual(PendingShare.objects.filter(source_lesson=self.lesson).count(), 2)
        enqueue.assert_called_once()

    def test_ensure_shared_lesson_returns_winner_on_insert_collision(self):
        """Unit cover for the recovery seam: when the existence check misses (the
        winner isn't visible on this connection yet) AND the INSERT collides, the
        loser catches the violation and re-fetches the winner under the service
        context — returning ``(winner, created=False)``, never re-raising."""
        winner, created = access.ensure_shared_lesson(self.lesson, self.a)
        self.assertTrue(created)
        with (
            mock.patch.object(SharedLesson.objects, "get", side_effect=SharedLesson.DoesNotExist),
            mock.patch.object(SharedLesson.objects, "create", side_effect=IntegrityError("dup source_lesson")),
        ):
            row, created_again = access.ensure_shared_lesson(self.lesson, self.a)
        self.assertFalse(created_again)
        self.assertEqual(row.id, winner.id)


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
        self.sl, _ = access.ensure_shared_lesson(self.lesson, self.a)
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
