"""Regression tests for cluster A32 — fuel#1 tombstone persistence gap.

Covers the case where deleted_external_ids contains an entry with no matching
DB row (HK sample deleted before it ever synced, or already removed on a
prior sync). Prior to the fix, the tombstone was never persisted in this
path — only the post_delete signal wrote it, which only fires when a row
actually exists. An anchor reset / app reinstall could then resurrect the
sample, contradicting the module invariant.
"""

from __future__ import annotations

from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.tenants.services import create_tenant

from .models import FuelProfile, Workout, WorkoutSource


class TombstonePersistenceNoMatchingRowTests(TestCase):
    """fuel#1 — tombstone must be persisted even when the deleted_external_id
    has no matching DB row (never-synced sample or already-removed row)."""

    def setUp(self):
        self.tenant = create_tenant(display_name="Tombstone Test", telegram_chat_id=900099)
        self.tenant.fuel_enabled = True
        self.tenant.save(update_fields=["fuel_enabled"])
        self.user = self.tenant.user
        self.user.timezone = "UTC"
        self.user.save(update_fields=["timezone"])
        FuelProfile.objects.create(tenant=self.tenant)
        self.client = APIClient()
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

    def _post(self, payload):
        return self.client.post("/api/v1/fuel/healthkit/sync/", payload, format="json")

    @staticmethod
    def _workout_item(external_id="hk-uuid-A32-001", **overrides):
        item = {
            "external_id": external_id,
            "activity": "Outdoor Run",
            "category": "cardio",
            "raw_type": "running",
            "source_bundle": "com.apple.health.workout",
            "started_at": "2026-06-10T00:00:00Z",
            "ended_at": "2026-06-10T00:42:00Z",
            "duration_minutes": 42,
            "metrics": {"distance_km": 5.0},
        }
        item.update(overrides)
        return item

    def test_tombstone_persisted_when_no_matching_row(self):
        """Sending deleted_external_ids for a sample that was never imported
        must still persist the tombstone so a later re-import is blocked."""
        external_id = "hk-uuid-A32-never-synced"

        # Confirm no row exists for this external_id.
        self.assertFalse(Workout.objects.filter(tenant=self.tenant, external_id=external_id).exists())

        resp = self._post({"deleted_external_ids": [external_id]})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["summary"]["deleted"], 0)

        # Tombstone must be persisted despite deleted_count == 0.
        profile = FuelProfile.objects.get(tenant=self.tenant)
        self.assertIn(external_id, profile.healthkit_tombstones)

    def test_anchor_reset_cannot_resurrect_never_synced_sample(self):
        """After a tombstone-with-no-matching-row is persisted, a subsequent
        import of that external_id (simulating anchor reset / reinstall) must
        be rejected as a duplicate."""
        external_id = "hk-uuid-A32-never-synced-2"

        # Delete (no matching row) — tombstone must be recorded.
        resp = self._post({"deleted_external_ids": [external_id]})
        self.assertEqual(resp.status_code, 200)

        # Simulate anchor reset: re-deliver the same sample.
        resp = self._post({"workouts": [self._workout_item(external_id=external_id)]})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["results"][0]["status"], "duplicate")
        self.assertFalse(Workout.objects.filter(tenant=self.tenant, external_id=external_id).exists())

    def test_tombstone_persisted_when_row_already_removed(self):
        """When a row existed previously but was already removed (e.g. double-
        delete), the tombstone for the external_id must still be persisted."""
        external_id = "hk-uuid-A32-double-delete"

        # Import then delete directly (via signal path).
        self._post({"workouts": [self._workout_item(external_id=external_id)]})
        Workout.objects.filter(tenant=self.tenant, external_id=external_id, source=WorkoutSource.HEALTHKIT).delete()

        # At this point signal already wrote the tombstone, but verify the
        # ingest path also handles a second delete-request gracefully.
        resp = self._post({"deleted_external_ids": [external_id]})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["summary"]["deleted"], 0)

        profile = FuelProfile.objects.get(tenant=self.tenant)
        self.assertIn(external_id, profile.healthkit_tombstones)

    def test_tombstone_cap_enforced(self):
        """The tombstone list must never grow beyond _TOMBSTONE_CAP (200)
        entries from the ingest path."""
        from apps.fuel.healthkit import _TOMBSTONE_CAP

        # Pre-fill with entries up to the cap.
        profile = FuelProfile.objects.get(tenant=self.tenant)
        profile.healthkit_tombstones = [f"old-{i}" for i in range(_TOMBSTONE_CAP)]
        profile.save(update_fields=["healthkit_tombstones", "updated_at"])

        # Adding one more via the never-synced path.
        resp = self._post({"deleted_external_ids": ["hk-uuid-A32-cap-test"]})
        self.assertEqual(resp.status_code, 200)

        profile.refresh_from_db()
        self.assertLessEqual(len(profile.healthkit_tombstones), _TOMBSTONE_CAP)
        self.assertIn("hk-uuid-A32-cap-test", profile.healthkit_tombstones)
