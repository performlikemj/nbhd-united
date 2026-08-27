"""Lifecycle tests for provisional PII bindings."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.pii.config import resolve_positive_int
from apps.pii.provisional import PiiIngress, record_provisional_sightings, transition_binding
from apps.pii.redactor import DetectedEntity, redact_user_message
from apps.tenants.models import Tenant, User


def _tenant(*, entry: dict, denylist: dict | None = None) -> Tenant:
    user = User.objects.create_user(
        username=f"u_{secrets.token_hex(4)}",
        email=f"{secrets.token_hex(4)}@example.com",
        password="fixture-password",
    )
    return Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE,
        container_fqdn="container.example.com",
        pii_entity_map={"[PERSON_1]": entry},
        pii_denylist=denylist or {},
    )


class ProvisionalConfigTests(TestCase):
    def test_ttl_must_be_positive(self):
        self.assertEqual(resolve_positive_int("72", name="PII_PROVISIONAL_TTL_HOURS"), 72)
        for invalid in ("0", "-1", "invalid", None):
            with self.assertRaises(ValueError):
                resolve_positive_int(invalid, name="PII_PROVISIONAL_TTL_HOURS")


class TransitionBindingTests(TestCase):
    NOW = datetime(2026, 8, 28, 1, 2, 3, tzinfo=UTC)

    def test_keep_promotes_and_preserves_opaque_fields(self):
        tenant = _tenant(
            entry={
                "name": "Fakenamealpha",
                "provisional": True,
                "first_seen_at": "2026-08-27T00:00:00+00:00",
                "future_field": "fixture",
            }
        )
        result = transition_binding(tenant, "[PERSON_1]", "keep", now=self.NOW)
        self.assertTrue(result.changed)
        self.assertEqual(result.outcome, "promoted")
        self.assertFalse(result.entry["provisional"])
        self.assertEqual(result.entry["promoted_by"], "owner")
        self.assertEqual(result.entry["future_field"], "fixture")

    def test_owner_retirement_beats_reactivation(self):
        tenant = _tenant(
            entry={
                "name": "Fakenamealpha",
                "provisional": True,
                "retired": True,
                "retired_reason": "owner",
            }
        )
        result = transition_binding(tenant, "[PERSON_1]", "reactivate", now=self.NOW)
        self.assertFalse(result.changed)
        self.assertEqual(result.outcome, "blocked")

    def test_denylist_beats_promotion(self):
        tenant = _tenant(
            entry={"name": "Fakenamealpha", "provisional": True},
            denylist={"fakenamealpha": {"reason": "fixture"}},
        )
        result = transition_binding(tenant, "[PERSON_1]", "promote", now=self.NOW)
        self.assertFalse(result.changed)
        self.assertEqual(result.outcome, "blocked")


class ProvisionalMintTests(TestCase):
    NOW = datetime(2026, 8, 28, 1, 2, 3, tzinfo=UTC)

    def setUp(self):
        self.tenant = _tenant(entry={"name": "Existingfixture"})
        self.tenant.pii_entity_map = {}
        self.tenant.save(update_fields=["pii_entity_map"])
        self.ingress = PiiIngress(channel="fixture", provider_event_id="event-1", occurred_at=self.NOW)

    @override_settings(PII_PROVISIONAL_TENANT_IDS=frozenset())
    def test_flag_off_mints_permanent(self):
        detected = [DetectedEntity("PERSON", 0, 13, 0.99)]
        with patch("apps.pii.redactor._detect_pii", return_value=detected):
            redact_user_message("Fakenamealpha", self.tenant, ingress=self.ingress)
        self.tenant.refresh_from_db()
        self.assertNotIn("provisional", self.tenant.pii_entity_map["[PERSON_1]"])

    def test_allowlisted_single_token_person_mints_provisional(self):
        detected = [DetectedEntity("PERSON", 0, 13, 0.99)]
        with (
            override_settings(PII_PROVISIONAL_TENANT_IDS=frozenset({str(self.tenant.pk)})),
            patch("apps.pii.redactor._detect_pii", return_value=detected),
        ):
            redact_user_message("Fakenamealpha", self.tenant, ingress=self.ingress)
        self.tenant.refresh_from_db()
        entry = self.tenant.pii_entity_map["[PERSON_1]"]
        self.assertTrue(entry["provisional"])
        self.assertEqual(entry["first_seen_at"], self.NOW.isoformat())
        self.assertEqual(entry["seen_events"], [])

    def test_multi_token_person_remains_permanent(self):
        detected = [DetectedEntity("PERSON", 0, 27, 0.99)]
        with (
            override_settings(PII_PROVISIONAL_TENANT_IDS=frozenset({str(self.tenant.pk)})),
            patch("apps.pii.redactor._detect_pii", return_value=detected),
        ):
            redact_user_message("Fakenamealpha Fakenamesigma", self.tenant, ingress=self.ingress)
        self.tenant.refresh_from_db()
        self.assertNotIn("provisional", self.tenant.pii_entity_map["[PERSON_1]"])

    @override_settings(PII_PROVISIONAL_TENANT_IDS=frozenset())
    def test_expired_binding_reactivates_before_current_redaction(self):
        self.tenant.pii_entity_map = {
            "[PERSON_1]": {
                "name": "Fakenamealpha",
                "provisional": True,
                "last_seen_at": "2026-08-20T00:00:00+00:00",
                "retired": True,
                "retired_at": "2026-08-24T00:00:00+00:00",
                "retired_reason": "provisional-expired",
            }
        }
        self.tenant.save(update_fields=["pii_entity_map"])
        with patch("apps.pii.redactor._detect_pii", return_value=[]):
            redacted = redact_user_message("Fakenamealpha", self.tenant, ingress=self.ingress)
        record_provisional_sightings(self.tenant, "Fakenamealpha", self.ingress)
        self.tenant.refresh_from_db()
        self.assertEqual(redacted, "[PERSON_1]")
        self.assertEqual(set(self.tenant.pii_entity_map), {"[PERSON_1]"})
        self.assertFalse(self.tenant.pii_entity_map["[PERSON_1]"].get("retired", False))
        self.assertEqual(len(self.tenant.pii_entity_map["[PERSON_1]"]["seen_events"]), 1)


class SightingRecorderTests(TestCase):
    def setUp(self):
        self.tenant = _tenant(
            entry={
                "name": "Fakenamealpha",
                "provisional": True,
                "first_seen_at": "2026-08-27T00:00:00+00:00",
                "last_seen_at": "2026-08-27T00:00:00+00:00",
                "seen_events": [],
                "seen_dates": [],
            }
        )

    def _record(self, event_id: str, occurred_at: datetime, text: str = "Fakenamealpha arrived"):
        return record_provisional_sightings(
            self.tenant,
            text,
            PiiIngress(channel="fixture", provider_event_id=event_id, occurred_at=occurred_at),
        )

    def test_dedupes_one_provider_event(self):
        now = datetime(2026, 8, 28, 1, tzinfo=UTC)
        self._record("event-1", now)
        self._record("event-1", now)
        self.tenant.refresh_from_db()
        entry = self.tenant.pii_entity_map["[PERSON_1]"]
        self.assertEqual(len(entry["seen_events"]), 1)
        self.assertEqual(entry["seen_dates"], ["2026-08-28"])

    def test_promotes_at_three_events_across_two_local_dates(self):
        self._record("event-1", datetime(2026, 8, 28, 1, tzinfo=UTC))
        self._record("event-2", datetime(2026, 8, 28, 2, tzinfo=UTC))
        results = self._record("event-3", datetime(2026, 8, 29, 1, tzinfo=UTC))
        self.assertEqual(results[0].outcome, "promoted")
        self.tenant.refresh_from_db()
        entry = self.tenant.pii_entity_map["[PERSON_1]"]
        self.assertFalse(entry["provisional"])
        self.assertEqual(entry["promoted_by"], "recurrence")

    def test_uses_substitution_boundary_rules(self):
        self.assertEqual(self._record("event-1", datetime(2026, 8, 28, 1, tzinfo=UTC), "XFakenamealphaY"), [])

    def test_channel_participates_in_event_digest(self):
        occurred_at = datetime(2026, 8, 28, 1, tzinfo=UTC)
        record_provisional_sightings(
            self.tenant,
            "Fakenamealpha",
            PiiIngress(channel="telegram", provider_event_id="same", occurred_at=occurred_at),
        )
        record_provisional_sightings(
            self.tenant,
            "Fakenamealpha",
            PiiIngress(channel="ios", provider_event_id="same", occurred_at=occurred_at),
        )
        self.tenant.refresh_from_db()
        self.assertEqual(len(self.tenant.pii_entity_map["[PERSON_1]"]["seen_events"]), 2)

    def test_recurrence_telemetry_contains_no_content_identifiers(self):
        ingress = PiiIngress(
            channel="fixture",
            provider_event_id="secret-event-fixture",
            occurred_at=datetime(2026, 8, 28, 1, tzinfo=UTC),
        )
        with self.assertLogs("apps.pii.provisional", level="INFO") as captured:
            record_provisional_sightings(self.tenant, "Fakenamealpha arrived", ingress)
        rendered = "\n".join(captured.output)
        self.assertIn("pii_policy_recurrence", rendered)
        self.assertNotIn("Fakenamealpha", rendered)
        self.assertNotIn("[PERSON_1]", rendered)
        self.assertNotIn("secret-event-fixture", rendered)
