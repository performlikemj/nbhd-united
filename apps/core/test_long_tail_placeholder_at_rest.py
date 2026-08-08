"""P3 W3b real writer/read seams for Core and meditation content."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.tenants.services import create_tenant
from apps.tenants.test_utils import seed_internal_key

from . import compose, services
from .models import CoreProfile, MeditationSession, MeditationStatus
from .serializers import CoreProfileSerializer, MeditationSessionSerializer


@contextmanager
def _checked_detection():
    with (
        patch("apps.pii.redactor._detect_pii", return_value=[]),
        patch("apps.pii.authoring._detect_pii", return_value=[]),
    ):
        yield


def _manifest(name="Alice"):
    phase_data = (
        ("arrival", 60, f"Welcome {name}. Let yourself arrive."),
        ("breath_anchor", 75, f"Notice your breath beside {name}."),
        ("body_scan", 150, f"Let {name} soften your shoulders."),
        ("core_practice", 210, f"Set down what {name} asked you to carry."),
        ("integration", 60, f"Widen your awareness toward {name}."),
        ("closing", 45, f"Bring this stillness back to {name}."),
    )
    phases = []
    for phase, seconds, text in phase_data:
        segments = [{"type": "speech", "text": text, "tone": "warm"}]
        if phase == "closing":
            segments.append({"type": "speech", "text": f"Carry this ease toward {name}.", "tone": "warm"})
        else:
            segments.append({"type": "silence", "seconds": "flex"})
        phases.append(
            {
                "name": phase,
                "target_seconds": seconds,
                "segments": segments,
            }
        )
    return {
        "schema_version": 1,
        "title": f"Rest with {name}",
        "theme": f"release tension around {name}",
        "voice": "Achernar",
        "global_tone": "soft, slow, warm",
        "total_target_seconds": 600,
        "ambient": None,
        "phases": phases,
    }


@override_settings(NBHD_INTERNAL_API_KEY="test-internal-key", NBHD_DISABLE_BACKGROUND_THREADS=True)
class CoreLongTailPlaceholderTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="W3b Core", telegram_chat_id=880313)
        self.tenant.pii_entity_map = {"[PERSON_1]": {"name": "Alice"}}
        self.tenant.save(update_fields=["pii_entity_map"])
        seed_internal_key(self.tenant)
        CoreProfile.objects.create(tenant=self.tenant)
        self.client = APIClient()
        self.client.force_authenticate(user=self.tenant.user)
        self.runtime = APIClient()
        self.runtime_headers = {
            "HTTP_X_NBHD_INTERNAL_KEY": "test-internal-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }

    def _enable_placeholder_writes(self):
        self.tenant.layer1_placeholder_writes = True
        self.tenant.save(update_fields=["layer1_placeholder_writes"])

    def _runtime_meditation_url(self):
        return f"/api/v1/core/runtime/{self.tenant.id}/meditation/"

    def test_flag_off_owner_profile_and_runtime_meditation_preserve_bytes(self):
        context = "Reflect on Alice exactly"
        profile_response = self.client.patch(
            "/api/v1/core/profile/",
            {"additional_context": context},
            format="json",
        )
        manifest = _manifest()
        with patch("apps.cron.publish.publish_task"):
            meditation_response = self.runtime.post(
                self._runtime_meditation_url(),
                {"manifest": manifest},
                format="json",
                **self.runtime_headers,
            )

        profile = CoreProfile.objects.get(tenant=self.tenant)
        session = MeditationSession.objects.get(id=meditation_response.data["meditation_id"])
        self.assertEqual(profile_response.status_code, 200)
        self.assertEqual(profile.additional_context, context)
        self.assertEqual(profile.pii_receipts["additional_context"], {"state": "bypass", "writer": "owner"})
        self.assertEqual(session.title, manifest["title"])
        self.assertEqual(session.manifest, manifest)
        self.assertEqual(session.pii_receipts["manifest"], {"state": "bypass", "writer": "runtime"})

    def test_owner_and_runtime_profile_writes_keep_runtime_projection_placeholder_space(self):
        self._enable_placeholder_writes()
        with _checked_detection():
            owner_response = self.client.patch(
                "/api/v1/core/profile/",
                {
                    "additional_context": "Breathe with Alice",
                    "pii_receipts": {"additional_context": {"state": "forged"}},
                },
                format="json",
            )

        profile = CoreProfile.objects.get(tenant=self.tenant)
        self.assertEqual(profile.additional_context, "Breathe with [PERSON_1]")
        self.assertEqual(profile.pii_receipts["additional_context"]["writer"], "owner")
        self.assertEqual(owner_response.data["additional_context"], "Breathe with Alice")
        self.assertNotEqual(owner_response.data["pii_receipts"]["additional_context"]["state"], "forged")

        with _checked_detection():
            runtime_response = self.runtime.patch(
                f"/api/v1/core/runtime/{self.tenant.id}/profile/",
                {"additional_context": "Release [PERSON_1]'s request"},
                format="json",
                **self.runtime_headers,
            )
        profile.refresh_from_db()
        self.assertEqual(runtime_response.status_code, 200)
        self.assertEqual(runtime_response.data["additional_context"], "Release [PERSON_1]'s request")
        self.assertNotIn("pii_receipts", runtime_response.data)
        self.assertEqual(profile.pii_receipts["additional_context"]["writer"], "runtime")

        owner_read = self.client.get("/api/v1/core/profile/")
        self.assertEqual(owner_read.data["additional_context"], "Release Alice's request")
        self.assertEqual(owner_read.data["pii_receipts"]["additional_context"]["redactions"][0]["value"], "Alice")

    def test_runtime_meditation_and_owner_feedback_round_trip_with_receipts(self):
        self._enable_placeholder_writes()
        with (
            _checked_detection(),
            patch("apps.cron.publish.publish_task"),
        ):
            created = self.runtime.post(
                self._runtime_meditation_url(),
                {"manifest": _manifest()},
                format="json",
                **self.runtime_headers,
            )

        self.assertEqual(created.status_code, 201, created.data)
        session = MeditationSession.objects.get(id=created.data["meditation_id"])
        self.assertEqual(session.title, "Rest with [PERSON_1]")
        self.assertEqual(
            session.manifest["phases"][0]["segments"][0]["text"], "Welcome [PERSON_1]. Let yourself arrive."
        )
        self.assertEqual(session.pii_receipts["manifest"]["writer"], "runtime")

        runtime_read = self.runtime.get(
            f"{self._runtime_meditation_url()}{session.id}/",
            **self.runtime_headers,
        )
        self.assertEqual(runtime_read.data["title"], "Rest with [PERSON_1]")
        self.assertNotIn("pii_receipts", runtime_read.data)

        session.status = MeditationStatus.READY
        session.save(update_fields=["status"])
        owner_read = self.client.get(f"/api/v1/core/sessions/{session.id}/")
        self.assertEqual(owner_read.data["title"], "Rest with Alice")
        self.assertEqual(owner_read.data["pii_receipts"]["title"]["redactions"][0]["value"], "Alice")

        with _checked_detection():
            feedback = self.client.patch(
                f"/api/v1/core/sessions/{session.id}/",
                {
                    "feedback_note": "Alice's pacing helped",
                    "pii_receipts": {"feedback_note": {"state": "forged"}},
                },
                format="json",
            )
        session.refresh_from_db()
        self.assertEqual(session.feedback_note, "[PERSON_1]'s pacing helped")
        self.assertEqual(session.pii_receipts["feedback_note"]["writer"], "owner")
        self.assertEqual(feedback.data["feedback_note"], "Alice's pacing helped")

    def test_background_compose_authors_manifest_and_owner_read_rehydrates(self):
        self._enable_placeholder_writes()
        session = MeditationSession.objects.create(
            tenant=self.tenant,
            date=date.today(),
            status=MeditationStatus.PENDING,
        )
        with (
            _checked_detection(),
            patch.object(services, "gather_meditation_signals", return_value={}),
            patch.object(compose, "author_manifest", return_value=_manifest()),
            patch("apps.cron.publish.publish_task"),
        ):
            services.compose_meditation(session)

        session.refresh_from_db()
        self.assertEqual(session.title, "Rest with [PERSON_1]")
        self.assertEqual(session.pii_receipts["title"]["writer"], "background")
        self.assertEqual(session.pii_receipts["manifest"]["writer"], "background")
        owner_read = self.client.get(f"/api/v1/core/sessions/{session.id}/")
        self.assertEqual(owner_read.data["title"], "Rest with Alice")
        self.assertEqual(owner_read.data["pii_receipts"]["manifest"]["redactions"][0]["value"], "Alice")

    def test_background_persist_resume_authors_guidance_and_owner_read_rehydrates(self):
        self._enable_placeholder_writes()
        manifest = _manifest("[PERSON_1]")
        session = MeditationSession.objects.create(
            tenant=self.tenant,
            date=date.today(),
            status=MeditationStatus.RENDERING,
            title="Rest with [PERSON_1]",
            theme="release tension around [PERSON_1]",
            manifest=manifest,
            guidance_text="Breathe beside Alice",
            artifact_manifest_sha256=services._manifest_sha256(manifest),
        )

        with (
            _checked_detection(),
            patch.object(services, "_meditation_artifact_exists", side_effect=[True, False]),
        ):
            resumed = services._resume_uploaded_artifacts(
                session,
                manifest_sha256=services._manifest_sha256(manifest),
                voice="Achernar",
                model="test-model",
            )

        self.assertTrue(resumed)
        session.refresh_from_db()
        self.assertEqual(session.status, MeditationStatus.READY)
        self.assertEqual(session.guidance_text, "Breathe beside [PERSON_1]")
        self.assertEqual(session.pii_receipts["guidance_text"]["state"], "placeholder")
        self.assertEqual(session.pii_receipts["guidance_text"]["writer"], "background")

        owner_read = self.client.get(f"/api/v1/core/sessions/{session.id}/")
        self.assertEqual(owner_read.status_code, 200, owner_read.data)
        self.assertEqual(owner_read.data["guidance_text"], "Breathe beside Alice")
        self.assertEqual(
            owner_read.data["pii_receipts"]["guidance_text"]["redactions"],
            [{"placeholder": "[PERSON_1]", "value": "Alice"}],
        )

    def test_owner_receipt_fields_are_read_only(self):
        self.assertTrue(CoreProfileSerializer().fields["pii_receipts"].read_only)
        self.assertTrue(MeditationSessionSerializer().fields["pii_receipts"].read_only)
