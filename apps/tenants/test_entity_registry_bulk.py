"""Tests for the entity-registry bulk-delete settings view.

POST /api/v1/tenants/settings/entity-registry/bulk/

Backs the People settings page's "Delete N selected" action. Deleting
bindings does not stop future redaction on its own — the ``deny`` flag
additionally adds each deleted entry's name to the tenant denylist so the
value stops being re-minted. This is a privacy surface: every test must
confirm the caller can only ever mutate their own tenant's data.
"""

from __future__ import annotations

import secrets

from django.test import TestCase
from rest_framework.test import APIClient

from apps.tenants.models import Tenant, User

_URL = "/api/v1/tenants/settings/entity-registry/bulk/"


def _make_user_with_tenant(
    entity_map: dict | None = None,
    denylist: dict | None = None,
) -> tuple[User, Tenant]:
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
    update_fields = []
    if entity_map is not None:
        tenant.pii_entity_map = entity_map
        update_fields.append("pii_entity_map")
    if denylist is not None:
        tenant.pii_denylist = denylist
        update_fields.append("pii_denylist")
    if update_fields:
        tenant.save(update_fields=update_fields)
    return user, tenant


class EntityRegistryBulkDeleteViewTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_requires_authentication(self):
        resp = self.client.post(_URL, {"placeholders": ["[PERSON_1]"]}, format="json")
        self.assertEqual(resp.status_code, 401)

    def test_happy_path_multi_delete(self):
        user, tenant = _make_user_with_tenant(
            entity_map={
                "[PERSON_1]": {"name": "Alice"},
                "[PERSON_2]": {"name": "Bob"},
                "[LOCATION_3]": {"name": "Osaka"},
            }
        )
        self.client.force_authenticate(user=user)
        resp = self.client.post(
            _URL,
            {"placeholders": ["[PERSON_1]", "[PERSON_2]"]},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(set(body["deleted"]), {"[PERSON_1]", "[PERSON_2]"})
        self.assertEqual(body["not_found"], [])
        self.assertEqual(set(body["denied"]), {"alice", "bob"})

        tenant.refresh_from_db()
        self.assertEqual(set(tenant.pii_entity_map.keys()), {"[PERSON_1]", "[PERSON_2]", "[LOCATION_3]"})
        self.assertTrue(tenant.pii_entity_map["[PERSON_1]"]["retired"])
        self.assertEqual(tenant.pii_entity_map["[PERSON_1]"]["retired_reason"], "owner")
        self.assertNotIn("retired", tenant.pii_entity_map["[LOCATION_3]"])
        self.assertEqual(set(tenant.pii_denylist), {"alice", "bob"})

    def test_partial_not_found(self):
        user, tenant = _make_user_with_tenant(
            entity_map={"[PERSON_1]": {"name": "Alice"}},
        )
        self.client.force_authenticate(user=user)
        resp = self.client.post(
            _URL,
            {"placeholders": ["[PERSON_1]", "[PERSON_99]"]},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["deleted"], ["[PERSON_1]"])
        self.assertEqual(body["not_found"], ["[PERSON_99]"])

        tenant.refresh_from_db()
        self.assertTrue(tenant.pii_entity_map["[PERSON_1]"]["retired"])

    def test_deny_true_adds_canonical_keys_and_leaves_untouched_entries_intact(self):
        user, tenant = _make_user_with_tenant(
            entity_map={
                "[PERSON_1]": {"name": "Alice"},
                "[PERSON_2]": {"name": "Bob"},
                "[PERSON_3]": {"name": "Carol"},
            },
            denylist={"already": {"reason": "manual", "decided_at": "2026-01-01"}},
        )
        self.client.force_authenticate(user=user)
        resp = self.client.post(
            _URL,
            {"placeholders": ["[PERSON_1]", "[PERSON_2]"], "deny": True},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(set(body["deleted"]), {"[PERSON_1]", "[PERSON_2]"})
        self.assertEqual(set(body["denied"]), {"alice", "bob"})

        tenant.refresh_from_db()
        # Retired bindings stay for rehydration; unrequested binding stays active.
        self.assertEqual(set(tenant.pii_entity_map.keys()), {"[PERSON_1]", "[PERSON_2]", "[PERSON_3]"})
        self.assertTrue(tenant.pii_entity_map["[PERSON_1]"]["retired"])
        self.assertNotIn("retired", tenant.pii_entity_map["[PERSON_3]"])
        # New canonical keys added with bulk-delete metadata.
        self.assertEqual(tenant.pii_denylist["alice"]["reason"], "bulk-delete-retired")
        self.assertIsNotNone(tenant.pii_denylist["alice"]["decided_at"])
        self.assertIn("bob", tenant.pii_denylist)
        # Pre-existing denylist entry untouched.
        self.assertEqual(tenant.pii_denylist["already"]["decided_at"], "2026-01-01")

    def test_deny_true_with_legacy_bare_string_entries(self):
        # Legacy pre-registry map values are bare strings, not dicts.
        user, tenant = _make_user_with_tenant(
            entity_map={
                "[PERSON_1]": "Nana",
                "[PERSON_2]": {"name": "Bob"},
            },
        )
        self.client.force_authenticate(user=user)
        resp = self.client.post(
            _URL,
            {"placeholders": ["[PERSON_1]", "[PERSON_2]"], "deny": True},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(set(body["deleted"]), {"[PERSON_1]", "[PERSON_2]"})
        self.assertEqual(set(body["denied"]), {"nana", "bob"})

        tenant.refresh_from_db()
        self.assertTrue(tenant.pii_entity_map["[PERSON_1]"]["retired"])
        self.assertEqual(tenant.pii_entity_map["[PERSON_1]"]["name"], "Nana")
        self.assertEqual(tenant.pii_denylist["nana"]["reason"], "bulk-delete-retired")

    def test_deny_defaults_false_still_denies_retired_value(self):
        user, tenant = _make_user_with_tenant(
            entity_map={"[PERSON_1]": {"name": "Alice"}},
            denylist={},
        )
        self.client.force_authenticate(user=user)
        resp = self.client.post(_URL, {"placeholders": ["[PERSON_1]"]}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["denied"], ["alice"])
        tenant.refresh_from_db()
        self.assertIn("alice", tenant.pii_denylist)

    def test_retiring_one_duplicate_does_not_deny_active_name(self):
        user, tenant = _make_user_with_tenant(
            entity_map={
                "[PERSON_3]": {"name": "Alex"},
                "[PERSON_9]": {"name": "Alex"},
            },
            denylist={},
        )
        self.client.force_authenticate(user=user)

        resp = self.client.post(_URL, {"placeholders": ["[PERSON_9]"]}, format="json")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["denied"], [])
        tenant.refresh_from_db()
        self.assertNotIn("alex", tenant.pii_denylist)
        self.assertNotIn("retired", tenant.pii_entity_map["[PERSON_3]"])
        self.assertTrue(tenant.pii_entity_map["[PERSON_9]"]["retired"])

    def test_retiring_all_duplicates_denies_name_after_batch(self):
        user, tenant = _make_user_with_tenant(
            entity_map={
                "[PERSON_3]": {"name": "Alex"},
                "[PERSON_9]": {"name": "Alex"},
            },
            denylist={},
        )
        self.client.force_authenticate(user=user)

        resp = self.client.post(
            _URL,
            {"placeholders": ["[PERSON_3]", "[PERSON_9]"]},
            format="json",
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["denied"], ["alex"])
        tenant.refresh_from_db()
        self.assertIn("alex", tenant.pii_denylist)
        self.assertTrue(tenant.pii_entity_map["[PERSON_3]"]["retired"])
        self.assertTrue(tenant.pii_entity_map["[PERSON_9]"]["retired"])

    def test_rejects_oversized_batch(self):
        user, _ = _make_user_with_tenant(entity_map={})
        self.client.force_authenticate(user=user)
        resp = self.client.post(
            _URL,
            {"placeholders": [f"[PERSON_{i}]" for i in range(1001)]},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_rejects_non_list_body(self):
        user, _ = _make_user_with_tenant(entity_map={})
        self.client.force_authenticate(user=user)
        for bad in [{"placeholders": "[PERSON_1]"}, {"placeholders": None}, {}]:
            resp = self.client.post(_URL, bad, format="json")
            self.assertEqual(resp.status_code, 400, f"failed body={bad!r}")

    def test_rejects_empty_list(self):
        user, _ = _make_user_with_tenant(entity_map={})
        self.client.force_authenticate(user=user)
        resp = self.client.post(_URL, {"placeholders": []}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_rejects_non_string_element(self):
        # The contract is a list *of strings* — a stray non-string is a
        # hard 400, not a per-item skip.
        user, _ = _make_user_with_tenant(entity_map={"[PERSON_1]": {"name": "Alice"}})
        self.client.force_authenticate(user=user)
        resp = self.client.post(_URL, {"placeholders": ["[PERSON_1]", 42]}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_tenants_isolated(self):
        # Tenant A owns [PERSON_1]; Tenant B's bulk-delete for the same
        # placeholder must not touch A's map.
        _, tenant_a = _make_user_with_tenant(entity_map={"[PERSON_1]": {"name": "Alice"}})
        user_b, tenant_b = _make_user_with_tenant(entity_map={"[PERSON_1]": {"name": "Bob"}})
        self.client.force_authenticate(user=user_b)
        resp = self.client.post(_URL, {"placeholders": ["[PERSON_1]"]}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["deleted"], ["[PERSON_1]"])

        tenant_a.refresh_from_db()
        tenant_b.refresh_from_db()
        # A untouched; B's binding is a tenant-local tombstone.
        self.assertEqual(set(tenant_a.pii_entity_map.keys()), {"[PERSON_1]"})
        self.assertEqual(set(tenant_b.pii_entity_map.keys()), {"[PERSON_1]"})
        self.assertTrue(tenant_b.pii_entity_map["[PERSON_1]"]["retired"])
