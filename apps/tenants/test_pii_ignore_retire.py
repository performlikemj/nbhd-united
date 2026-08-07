"""Regression tests for Ignore retiring every matching PII binding."""

from __future__ import annotations

import secrets

from django.test import TestCase
from rest_framework.test import APIClient

from apps.pii.egress import _redact_known_values
from apps.pii.redactor import rehydrate_for_tenant
from apps.tenants.models import Tenant, User


def _make_user_with_tenant(*, entity_map: dict, denylist: dict | None = None) -> tuple[User, Tenant]:
    user = User.objects.create_user(
        username=f"u_{secrets.token_hex(4)}",
        email=f"{secrets.token_hex(4)}@example.com",
        password="hunter2-test",
    )
    tenant = Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE,
        container_fqdn="container.example.com",
        pii_entity_map=entity_map,
        pii_denylist=denylist or {},
    )
    return user, tenant


class PIIIgnoreRetireTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_ignore_retires_binding_without_breaking_historical_rehydration(self):
        user, tenant = _make_user_with_tenant(entity_map={"[PERSON_545]": "NBHD"})
        self.client.force_authenticate(user=user)
        self.assertEqual(_redact_known_values(tenant, "Ask NBHD today"), "Ask [PERSON_545] today")

        response = self.client.post(
            "/api/v1/tenants/settings/pii-denylist/",
            {"name": " nbhd "},
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["retired"], 1)
        tenant.refresh_from_db()
        self.assertIn("nbhd", tenant.pii_denylist)
        binding = tenant.pii_entity_map["[PERSON_545]"]
        self.assertIs(binding["retired"], True)
        self.assertTrue(binding["retired_at"])
        self.assertEqual(_redact_known_values(tenant, "Ask NBHD today"), "Ask NBHD today")
        self.assertEqual(rehydrate_for_tenant(tenant, "Ask [PERSON_545] today"), "Ask NBHD today")

        listing = self.client.get("/api/v1/tenants/settings/entity-registry/")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.json(), {"entries": []})

    def test_ignore_without_matching_binding_is_denylist_only(self):
        user, tenant = _make_user_with_tenant(entity_map={"[PERSON_1]": "Alice"})
        self.client.force_authenticate(user=user)

        response = self.client.post(
            "/api/v1/tenants/settings/pii-denylist/",
            {"name": "NBHD"},
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["retired"], 0)
        tenant.refresh_from_db()
        self.assertIn("nbhd", tenant.pii_denylist)
        self.assertEqual(tenant.pii_entity_map, {"[PERSON_1]": "Alice"})

    def test_ignore_retires_every_same_name_collision(self):
        user, tenant = _make_user_with_tenant(
            entity_map={
                "[PERSON_3]": {"name": "Alex"},
                "[PERSON_9]": {"name": " alex "},
            }
        )
        self.client.force_authenticate(user=user)

        response = self.client.post(
            "/api/v1/tenants/settings/pii-denylist/",
            {"name": "ALEX"},
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["retired"], 2)
        tenant.refresh_from_db()
        # Ignore applies to the word, unlike single-person DELETE, so every
        # canonical-name collision is intentionally retired.
        self.assertTrue(tenant.pii_entity_map["[PERSON_3]"]["retired"])
        self.assertTrue(tenant.pii_entity_map["[PERSON_9]"]["retired"])

    def test_bulk_ignore_retires_all_named_words(self):
        user, tenant = _make_user_with_tenant(
            entity_map={
                "[PERSON_1]": "Alice",
                "[PERSON_2]": {"name": "Bob"},
                "[PERSON_3]": {"name": "Carol"},
            }
        )
        self.client.force_authenticate(user=user)

        response = self.client.post(
            "/api/v1/tenants/settings/pii-denylist/bulk/",
            {"names": ["ALICE", " bob "]},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["retired"], 2)
        tenant.refresh_from_db()
        self.assertEqual(set(tenant.pii_denylist), {"alice", "bob"})
        self.assertTrue(tenant.pii_entity_map["[PERSON_1]"]["retired"])
        self.assertTrue(tenant.pii_entity_map["[PERSON_2]"]["retired"])
        self.assertEqual(tenant.pii_entity_map["[PERSON_3]"], {"name": "Carol"})

    def test_unignore_removes_key_without_restoring_binding(self):
        user, tenant = _make_user_with_tenant(
            entity_map={
                "[PERSON_1]": {
                    "name": "NBHD",
                    "retired": True,
                    "retired_at": "2026-08-03T00:00:00+00:00",
                }
            },
            denylist={"nbhd": {"reason": "manual"}},
        )
        self.client.force_authenticate(user=user)

        response = self.client.delete("/api/v1/tenants/settings/pii-denylist/nbhd/")

        self.assertEqual(response.status_code, 204)
        tenant.refresh_from_db()
        self.assertNotIn("nbhd", tenant.pii_denylist)
        self.assertTrue(tenant.pii_entity_map["[PERSON_1]"]["retired"])
        self.assertEqual(
            tenant.pii_entity_map["[PERSON_1]"]["retired_at"],
            "2026-08-03T00:00:00+00:00",
        )
