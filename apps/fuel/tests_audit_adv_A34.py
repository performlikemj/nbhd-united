"""Adversarial-audit regression tests for cluster A34.

fuel#3: WorkoutSerializer.original_workout was not in read_only_fields,
allowing an authenticated tenant to:
  (a) persist a cross-tenant FK (their workout's original_workout pointing at
      another tenant's workout), and
  (b) use the endpoint as a weak existence oracle for foreign workout UUIDs.

Fix: add "original_workout" to WorkoutSerializer.read_only_fields so the
field is always derived from the model and never accepted from request.data.
"""

from datetime import date

from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.fuel.models import Workout
from apps.fuel.serializers import WorkoutSerializer
from apps.tenants.services import create_tenant


class WorkoutSerializerOriginalWorkoutReadOnlyTests(TestCase):
    """original_workout must be in read_only_fields (fuel#3)."""

    def test_original_workout_is_read_only(self):
        """Serializer must declare original_workout as read-only at the Meta
        level so DRF never generates a writable PK field backed by the global
        (cross-tenant) queryset."""
        self.assertIn(
            "original_workout",
            WorkoutSerializer.Meta.read_only_fields,
            "original_workout must be in WorkoutSerializer.Meta.read_only_fields "
            "to prevent cross-tenant FK injection via the API.",
        )

    def test_original_workout_field_is_not_writable(self):
        """The generated field must be read-only (not a writable RelatedField)."""
        serializer = WorkoutSerializer()
        field = serializer.fields["original_workout"]
        self.assertTrue(
            getattr(field, "read_only", False),
            "original_workout field must be read_only=True; "
            "a writable RelatedField would allow cross-tenant FK injection.",
        )


@override_settings(
    SIMPLE_JWT={"SIGNING_KEY": "test-secret-key-not-for-prod"},
    NBHD_INTERNAL_API_KEY="test-internal-key-a34",
)
class WorkoutOriginalWorkoutCrossTenantAPITests(TestCase):
    """POST/PATCH must not accept original_workout from request.data (fuel#3)."""

    def setUp(self):
        self.tenant_a = create_tenant(display_name="Tenant A34-A", telegram_chat_id=834001)
        self.tenant_b = create_tenant(display_name="Tenant A34-B", telegram_chat_id=834002)

        # A workout owned by tenant_b — the cross-tenant target.
        self.tenant_b_workout = Workout.objects.create(
            tenant=self.tenant_b,
            date=date(2026, 6, 1),
            category="strength",
            activity="Bench Press",
        )

        # Client authenticated as tenant_a.
        self.client = APIClient()
        refresh = RefreshToken.for_user(self.tenant_a.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

    # ------------------------------------------------------------------
    # POST /api/v1/fuel/workouts/ — create path
    # ------------------------------------------------------------------

    def test_post_ignores_original_workout_cross_tenant(self):
        """Supplying a cross-tenant original_workout UUID in POST body must NOT
        write it to the created workout — field must be silently ignored."""
        resp = self.client.post(
            "/api/v1/fuel/workouts/",
            data={
                "date": "2026-06-10",
                "category": "strength",
                "activity": "Squat",
                "status": "done",
                "original_workout": str(self.tenant_b_workout.id),
            },
            format="json",
        )
        self.assertIn(resp.status_code, [200, 201], resp.data)

        # The FK must not have been written.
        created_id = resp.data["id"]
        created = Workout.objects.get(id=created_id)
        self.assertIsNone(
            created.original_workout_id,
            "original_workout must be None after POST — the field must not accept a caller-supplied cross-tenant FK.",
        )

    # ------------------------------------------------------------------
    # PATCH /api/v1/fuel/workouts/<id>/ — update path
    # ------------------------------------------------------------------

    def test_patch_ignores_original_workout_cross_tenant(self):
        """Supplying a cross-tenant original_workout UUID in PATCH body must NOT
        write it — field must be silently ignored."""
        # A workout owned by tenant_a.
        own_workout = Workout.objects.create(
            tenant=self.tenant_a,
            date=date(2026, 6, 10),
            category="cardio",
            activity="Run",
        )
        resp = self.client.patch(
            f"/api/v1/fuel/workouts/{own_workout.id}/",
            data={"original_workout": str(self.tenant_b_workout.id)},
            format="json",
        )
        self.assertIn(resp.status_code, [200, 201], resp.data)

        own_workout.refresh_from_db()
        self.assertIsNone(
            own_workout.original_workout_id,
            "original_workout must remain None after PATCH — the field must not "
            "accept a caller-supplied cross-tenant FK.",
        )

    def test_post_with_own_tenant_original_workout_also_ignored(self):
        """Even a same-tenant original_workout UUID must be ignored because the
        field is now unconditionally read-only (setting it via API is unsupported)."""
        own_original = Workout.objects.create(
            tenant=self.tenant_a,
            date=date(2026, 6, 5),
            category="strength",
            activity="Original Workout",
        )
        resp = self.client.post(
            "/api/v1/fuel/workouts/",
            data={
                "date": "2026-06-10",
                "category": "strength",
                "activity": "Rescheduled Workout",
                "status": "done",
                "original_workout": str(own_original.id),
            },
            format="json",
        )
        self.assertIn(resp.status_code, [200, 201], resp.data)

        created_id = resp.data["id"]
        created = Workout.objects.get(id=created_id)
        self.assertIsNone(
            created.original_workout_id,
            "original_workout must be None — the field is read-only and "
            "cannot be set via the API even with a same-tenant UUID.",
        )
