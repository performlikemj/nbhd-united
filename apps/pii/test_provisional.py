"""Lifecycle tests for provisional PII bindings."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.pii.provisional import PiiIngress, transition_binding
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

    def test_denylist_beats_promotion(self):
        tenant = _tenant(
            entry={"name": "Fakenamealpha", "provisional": True},
            denylist={"fakenamealpha": {"reason": "fixture"}},
        )
        result = transition_binding(tenant, "[PERSON_1]", "promote", now=self.NOW)
        self.assertFalse(result.changed)
        self.assertEqual(result.outcome, "blocked")
