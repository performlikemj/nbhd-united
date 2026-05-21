"""Tests for the entity registry settings views.

GET  /api/v1/tenants/settings/entity-registry/
PATCH/DELETE  /api/v1/tenants/settings/entity-registry/<placeholder>/

These are privacy-sensitive endpoints — every test must confirm the
caller can only ever read or mutate their own tenant's data.
"""

from __future__ import annotations

import secrets

from django.test import TestCase
from rest_framework.test import APIClient

from apps.tenants.models import Tenant, User


def _make_user_with_tenant(entity_map: dict | None = None) -> tuple[User, Tenant]:
    user = User.objects.create_user(
        username=f"u_{secrets.token_hex(4)}",
        email=f"{secrets.token_hex(4)}@example.com",
        password="hunter2-test",
    )
    tenant = Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE,
        container_fqdn="container.example.com",
    )
    if entity_map is not None:
        tenant.pii_entity_map = entity_map
        tenant.save(update_fields=["pii_entity_map"])
    return user, tenant


class EntityRegistryListViewTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_requires_authentication(self):
        resp = self.client.get("/api/v1/tenants/settings/entity-registry/")
        self.assertEqual(resp.status_code, 401)

    def test_returns_empty_entries_for_unpopulated_map(self):
        user, _ = _make_user_with_tenant(entity_map={})
        self.client.force_authenticate(user=user)
        resp = self.client.get("/api/v1/tenants/settings/entity-registry/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"entries": []})

    def test_coerces_legacy_string_entries_to_uniform_shape(self):
        user, _ = _make_user_with_tenant(
            entity_map={
                "[PERSON_1]": "Alice",
                "[PERSON_2]": {"name": "Bob", "relationship": "spouse", "notes": "n"},
            }
        )
        self.client.force_authenticate(user=user)
        resp = self.client.get("/api/v1/tenants/settings/entity-registry/")
        self.assertEqual(resp.status_code, 200)
        entries = resp.json()["entries"]
        self.assertEqual(len(entries), 2)
        # Sorted by placeholder
        self.assertEqual(entries[0]["placeholder"], "[PERSON_1]")
        self.assertEqual(entries[0]["name"], "Alice")
        self.assertEqual(entries[0]["relationship"], "")
        self.assertEqual(entries[0]["notes"], "")
        self.assertEqual(entries[1]["name"], "Bob")
        self.assertEqual(entries[1]["relationship"], "spouse")

    def test_cannot_see_other_tenants_entries(self):
        user_a, tenant_a = _make_user_with_tenant(entity_map={"[PERSON_1]": "Alice"})
        user_b, _ = _make_user_with_tenant(entity_map={"[PERSON_99]": "OtherUser"})
        self.client.force_authenticate(user=user_a)
        resp = self.client.get("/api/v1/tenants/settings/entity-registry/")
        names = [e["name"] for e in resp.json()["entries"]]
        self.assertEqual(names, ["Alice"])
        self.assertNotIn("OtherUser", names)


class EntityRegistryPatchTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_requires_authentication(self):
        resp = self.client.patch(
            "/api/v1/tenants/settings/entity-registry/%5BPERSON_1%5D/",
            {"name": "X"},
            format="json",
        )
        self.assertEqual(resp.status_code, 401)

    def test_patches_relationship_and_notes_and_stamps_updated_at(self):
        user, tenant = _make_user_with_tenant(entity_map={"[PERSON_1]": "Alice"})
        self.client.force_authenticate(user=user)
        resp = self.client.patch(
            "/api/v1/tenants/settings/entity-registry/%5BPERSON_1%5D/",
            {"relationship": "spouse", "notes": "loves haiku"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["name"], "Alice")  # name preserved
        self.assertEqual(body["relationship"], "spouse")
        self.assertEqual(body["notes"], "loves haiku")
        self.assertIsNotNone(body["updated_at"])

        tenant.refresh_from_db()
        entry = tenant.pii_entity_map["[PERSON_1]"]
        self.assertEqual(entry["name"], "Alice")
        self.assertEqual(entry["relationship"], "spouse")
        self.assertEqual(entry["notes"], "loves haiku")
        self.assertIn("updated_at", entry)

    def test_patches_name_to_correct_wrong_rehydration(self):
        user, tenant = _make_user_with_tenant(entity_map={"[PERSON_1]": "WrongName"})
        self.client.force_authenticate(user=user)
        resp = self.client.patch(
            "/api/v1/tenants/settings/entity-registry/%5BPERSON_1%5D/",
            {"name": "RightName"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        tenant.refresh_from_db()
        self.assertEqual(tenant.pii_entity_map["[PERSON_1]"]["name"], "RightName")

    def test_returns_404_for_unknown_placeholder(self):
        user, _ = _make_user_with_tenant(entity_map={"[PERSON_1]": "Alice"})
        self.client.force_authenticate(user=user)
        resp = self.client.patch(
            "/api/v1/tenants/settings/entity-registry/%5BPERSON_99%5D/",
            {"name": "Whatever"},
            format="json",
        )
        self.assertEqual(resp.status_code, 404)

    def test_rejects_oversized_fields(self):
        user, _ = _make_user_with_tenant(entity_map={"[PERSON_1]": "Alice"})
        self.client.force_authenticate(user=user)
        resp = self.client.patch(
            "/api/v1/tenants/settings/entity-registry/%5BPERSON_1%5D/",
            {"notes": "x" * 1000},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_rejects_non_string_fields(self):
        user, _ = _make_user_with_tenant(entity_map={"[PERSON_1]": "Alice"})
        self.client.force_authenticate(user=user)
        resp = self.client.patch(
            "/api/v1/tenants/settings/entity-registry/%5BPERSON_1%5D/",
            {"name": 42},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_null_value_clears_field(self):
        user, tenant = _make_user_with_tenant(entity_map={"[PERSON_1]": {"name": "X", "relationship": "spouse"}})
        self.client.force_authenticate(user=user)
        resp = self.client.patch(
            "/api/v1/tenants/settings/entity-registry/%5BPERSON_1%5D/",
            {"relationship": None},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        tenant.refresh_from_db()
        # to_storage_value drops empty optionals, so no relationship key
        self.assertNotIn("relationship", tenant.pii_entity_map["[PERSON_1]"])

    def test_cannot_patch_other_tenants_entries(self):
        user_a, _ = _make_user_with_tenant(entity_map={"[PERSON_1]": "Alice"})
        user_b, tenant_b = _make_user_with_tenant(entity_map={"[PERSON_1]": "Bob"})
        self.client.force_authenticate(user=user_a)
        # User A can edit their own [PERSON_1] (which maps to "Alice").
        # That update must not leak into Tenant B's "[PERSON_1]" row.
        self.client.patch(
            "/api/v1/tenants/settings/entity-registry/%5BPERSON_1%5D/",
            {"name": "AliceUpdated"},
            format="json",
        )
        tenant_b.refresh_from_db()
        self.assertEqual(tenant_b.pii_entity_map["[PERSON_1]"], "Bob")


class EntityRegistryDeleteTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_deletes_entry(self):
        user, tenant = _make_user_with_tenant(
            entity_map={
                "[PERSON_1]": "Alice",
                "[PERSON_2]": "Bob",
            }
        )
        self.client.force_authenticate(user=user)
        resp = self.client.delete("/api/v1/tenants/settings/entity-registry/%5BPERSON_1%5D/")
        self.assertEqual(resp.status_code, 204)
        tenant.refresh_from_db()
        self.assertNotIn("[PERSON_1]", tenant.pii_entity_map)
        # Other entries preserved
        self.assertIn("[PERSON_2]", tenant.pii_entity_map)

    def test_returns_404_for_unknown_placeholder(self):
        user, _ = _make_user_with_tenant(entity_map={"[PERSON_1]": "Alice"})
        self.client.force_authenticate(user=user)
        resp = self.client.delete("/api/v1/tenants/settings/entity-registry/%5BPERSON_99%5D/")
        self.assertEqual(resp.status_code, 404)

    def test_requires_authentication(self):
        resp = self.client.delete("/api/v1/tenants/settings/entity-registry/%5BPERSON_1%5D/")
        self.assertEqual(resp.status_code, 401)
