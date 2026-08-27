"""Lifecycle tests for provisional PII bindings."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

from django.test import TestCase

from apps.pii.provisional import transition_binding
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

    def test_denylist_beats_promotion(self):
        tenant = _tenant(
            entry={"name": "Fakenamealpha", "provisional": True},
            denylist={"fakenamealpha": {"reason": "fixture"}},
        )
        result = transition_binding(tenant, "[PERSON_1]", "promote", now=self.NOW)
        self.assertFalse(result.changed)
        self.assertEqual(result.outcome, "blocked")
