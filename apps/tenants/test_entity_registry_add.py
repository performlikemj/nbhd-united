"""Tests for the manual entity-registry add endpoint.

POST /api/v1/tenants/settings/entity-registry/

The "hide this too" front door: a user names a person/place the detector never
caught and it is minted into the SAME ``pii_entity_map`` + ``pii_type_counters``
the redactor drives, so it redacts / rehydrates / chips / review-queues exactly
like a detector-minted binding. This is a privacy surface — every test must
confirm the caller only ever mutates their own tenant, that minting reuses the
monotonic high-water counter (numbers never recycled), that a duplicate name
merges instead of double-minting, that adding clears any denylist key, and that
the common-word/fragment footgun screen fires before any write.

All fixtures use throwaway values ("Alice", "Osaka") — never real PII.
"""

from __future__ import annotations

import secrets

from django.test import TestCase
from rest_framework.test import APIClient

from apps.tenants.models import Tenant, User

_URL = "/api/v1/tenants/settings/entity-registry/"


def _make_user_with_tenant(
    entity_map: dict | None = None,
    denylist: dict | None = None,
    counters: dict | None = None,
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
    if counters is not None:
        tenant.pii_type_counters = counters
        update_fields.append("pii_type_counters")
    if update_fields:
        tenant.save(update_fields=update_fields)
    return user, tenant


class EntityRegistryAddViewTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    # -- auth ----------------------------------------------------------------

    def test_requires_authentication(self):
        resp = self.client.post(_URL, {"name": "Alice"}, format="json")
        self.assertEqual(resp.status_code, 401)

    # -- mint ----------------------------------------------------------------

    def test_mint_creates_binding_with_default_person_type(self):
        user, tenant = _make_user_with_tenant(entity_map={})
        self.client.force_authenticate(user=user)
        resp = self.client.post(
            _URL,
            {"name": "Alice", "relationship": "sister", "notes": "lives in Kyoto"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertEqual(body["placeholder"], "[PERSON_1]")
        self.assertEqual(body["name"], "Alice")
        self.assertEqual(body["relationship"], "sister")
        self.assertEqual(body["notes"], "lives in Kyoto")
        self.assertFalse(body["denylist_removed"])

        tenant.refresh_from_db()
        self.assertEqual(tenant.pii_entity_map["[PERSON_1]"]["name"], "Alice")
        self.assertEqual(tenant.pii_entity_map["[PERSON_1]"]["relationship"], "sister")
        # Counter advanced and persisted.
        self.assertEqual(tenant.pii_type_counters, {"PERSON": 1})

    def test_mint_location_type(self):
        user, tenant = _make_user_with_tenant(entity_map={})
        self.client.force_authenticate(user=user)
        resp = self.client.post(_URL, {"name": "Osaka", "entity_type": "LOCATION"}, format="json")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["placeholder"], "[LOCATION_1]")
        tenant.refresh_from_db()
        self.assertEqual(tenant.pii_type_counters, {"LOCATION": 1})

    def test_mint_uses_stored_high_water_over_map_max(self):
        # Map has only [PERSON_2] (max suffix 2) but the monotonic counter says
        # 539 was the highest EVER minted — a deleted 540 must not be reissued.
        # The next mint must jump to 540, proving the counter (not the map max)
        # drives numbering.
        user, tenant = _make_user_with_tenant(
            entity_map={"[PERSON_2]": {"name": "Bob"}},
            counters={"PERSON": 539},
        )
        self.client.force_authenticate(user=user)
        resp = self.client.post(_URL, {"name": "Alice"}, format="json")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["placeholder"], "[PERSON_540]")
        tenant.refresh_from_db()
        self.assertEqual(tenant.pii_type_counters["PERSON"], 540)
        self.assertIn("[PERSON_540]", tenant.pii_entity_map)

    def test_mint_seeds_from_map_max_when_no_stored_counter(self):
        # Legacy tenant: no pii_type_counters, so numbering falls back to the map
        # maxima + 1.
        user, tenant = _make_user_with_tenant(
            entity_map={"[PERSON_7]": {"name": "Bob"}},
            counters={},
        )
        self.client.force_authenticate(user=user)
        resp = self.client.post(_URL, {"name": "Alice"}, format="json")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["placeholder"], "[PERSON_8]")
        tenant.refresh_from_db()
        self.assertEqual(tenant.pii_type_counters["PERSON"], 8)

    # -- duplicate name merges ----------------------------------------------

    def test_duplicate_name_returns_existing_placeholder_and_merges(self):
        user, tenant = _make_user_with_tenant(
            entity_map={"[PERSON_3]": {"name": "Alice"}},
            counters={"PERSON": 3},
        )
        self.client.force_authenticate(user=user)
        resp = self.client.post(
            _URL,
            {"name": "  alice  ", "relationship": "sister", "notes": "note"},
            format="json",
        )
        # Case-insensitive canonical match => 200, existing placeholder.
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["placeholder"], "[PERSON_3]")
        self.assertEqual(body["relationship"], "sister")
        self.assertEqual(body["notes"], "note")

        tenant.refresh_from_db()
        # No new placeholder minted; counter untouched.
        self.assertEqual(set(tenant.pii_entity_map.keys()), {"[PERSON_3]"})
        self.assertEqual(tenant.pii_type_counters, {"PERSON": 3})
        # Relationship/notes merged onto the existing entry.
        self.assertEqual(tenant.pii_entity_map["[PERSON_3]"]["relationship"], "sister")
        self.assertEqual(tenant.pii_entity_map["[PERSON_3]"]["notes"], "note")
        # Original name preserved (not overwritten by the padded input).
        self.assertEqual(tenant.pii_entity_map["[PERSON_3]"]["name"], "Alice")

    def test_duplicate_name_without_metadata_leaves_existing_metadata(self):
        user, tenant = _make_user_with_tenant(
            entity_map={"[PERSON_1]": {"name": "Alice", "relationship": "sister"}},
        )
        self.client.force_authenticate(user=user)
        resp = self.client.post(_URL, {"name": "Alice"}, format="json")
        self.assertEqual(resp.status_code, 200)
        tenant.refresh_from_db()
        # relationship not provided => left intact.
        self.assertEqual(tenant.pii_entity_map["[PERSON_1]"]["relationship"], "sister")

    # -- denylist clearing ---------------------------------------------------

    def test_add_clears_denylist_key(self):
        user, tenant = _make_user_with_tenant(
            entity_map={},
            denylist={"alice": {"reason": "manual", "decided_at": "2026-01-01"}},
        )
        self.client.force_authenticate(user=user)
        resp = self.client.post(_URL, {"name": "Alice"}, format="json")
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(resp.json()["denylist_removed"])
        tenant.refresh_from_db()
        self.assertNotIn("alice", tenant.pii_denylist)

    def test_add_no_denylist_key_reports_false(self):
        user, tenant = _make_user_with_tenant(
            entity_map={},
            denylist={"bob": {"reason": "manual", "decided_at": "2026-01-01"}},
        )
        self.client.force_authenticate(user=user)
        resp = self.client.post(_URL, {"name": "Alice"}, format="json")
        self.assertEqual(resp.status_code, 201)
        self.assertFalse(resp.json()["denylist_removed"])
        tenant.refresh_from_db()
        # Unrelated denylist key untouched.
        self.assertIn("bob", tenant.pii_denylist)

    def test_duplicate_add_also_clears_denylist(self):
        user, tenant = _make_user_with_tenant(
            entity_map={"[PERSON_1]": {"name": "Alice"}},
            denylist={"alice": {"reason": "manual", "decided_at": "2026-01-01"}},
        )
        self.client.force_authenticate(user=user)
        resp = self.client.post(_URL, {"name": "Alice"}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["denylist_removed"])
        tenant.refresh_from_db()
        self.assertNotIn("alice", tenant.pii_denylist)

    # -- footgun screen ------------------------------------------------------

    def test_junk_name_returns_warning(self):
        user, tenant = _make_user_with_tenant(entity_map={})
        self.client.force_authenticate(user=user)
        # A dotted code identifier trips is_junk_span's "identifier" rule.
        resp = self.client.post(_URL, {"name": "redactor.py"}, format="json")
        self.assertEqual(resp.status_code, 422)
        self.assertIn("warning", resp.json())
        self.assertIn("confusing", resp.json()["warning"])
        # Nothing written.
        tenant.refresh_from_db()
        self.assertEqual(tenant.pii_entity_map, {})
        self.assertEqual(tenant.pii_type_counters, {})

    def test_junk_name_with_acknowledge_mints(self):
        user, tenant = _make_user_with_tenant(entity_map={})
        self.client.force_authenticate(user=user)
        resp = self.client.post(
            _URL,
            {"name": "redactor.py", "acknowledge_warning": True},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["placeholder"], "[PERSON_1]")
        tenant.refresh_from_db()
        self.assertIn("[PERSON_1]", tenant.pii_entity_map)

    def test_numeric_junk_name_returns_warning(self):
        user, _ = _make_user_with_tenant(entity_map={})
        self.client.force_authenticate(user=user)
        resp = self.client.post(_URL, {"name": "140kg"}, format="json")
        self.assertEqual(resp.status_code, 422)
        self.assertIn("warning", resp.json())

    # -- 400 validation ------------------------------------------------------

    def test_empty_name_is_400(self):
        user, _ = _make_user_with_tenant(entity_map={})
        self.client.force_authenticate(user=user)
        for bad in ["", "   ", None, 42]:
            resp = self.client.post(_URL, {"name": bad}, format="json")
            self.assertEqual(resp.status_code, 400, f"failed name={bad!r}")

    def test_missing_name_is_400(self):
        user, _ = _make_user_with_tenant(entity_map={})
        self.client.force_authenticate(user=user)
        resp = self.client.post(_URL, {"relationship": "sister"}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_too_long_name_is_400(self):
        user, _ = _make_user_with_tenant(entity_map={})
        self.client.force_authenticate(user=user)
        resp = self.client.post(_URL, {"name": "A" * 257}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_bad_entity_type_is_400(self):
        user, _ = _make_user_with_tenant(entity_map={})
        self.client.force_authenticate(user=user)
        for bad in ["EMAIL_ADDRESS", "banana", 5]:
            resp = self.client.post(_URL, {"name": "Alice", "entity_type": bad}, format="json")
            self.assertEqual(resp.status_code, 400, f"failed entity_type={bad!r}")

    def test_non_string_relationship_is_400(self):
        user, _ = _make_user_with_tenant(entity_map={})
        self.client.force_authenticate(user=user)
        resp = self.client.post(_URL, {"name": "Alice", "relationship": 5}, format="json")
        self.assertEqual(resp.status_code, 400)

    # -- misc ----------------------------------------------------------------

    def test_lowercase_entity_type_normalized(self):
        user, tenant = _make_user_with_tenant(entity_map={})
        self.client.force_authenticate(user=user)
        resp = self.client.post(_URL, {"name": "Osaka", "entity_type": "location"}, format="json")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["placeholder"], "[LOCATION_1]")

    def test_counter_advances_across_two_mints(self):
        user, tenant = _make_user_with_tenant(entity_map={})
        self.client.force_authenticate(user=user)
        r1 = self.client.post(_URL, {"name": "Alice"}, format="json")
        r2 = self.client.post(_URL, {"name": "Bob"}, format="json")
        self.assertEqual(r1.json()["placeholder"], "[PERSON_1]")
        self.assertEqual(r2.json()["placeholder"], "[PERSON_2]")
        tenant.refresh_from_db()
        self.assertEqual(tenant.pii_type_counters, {"PERSON": 2})

    # -- tenant isolation ----------------------------------------------------

    def test_tenants_isolated(self):
        _, tenant_a = _make_user_with_tenant(entity_map={}, counters={})
        user_b, tenant_b = _make_user_with_tenant(entity_map={}, counters={})
        self.client.force_authenticate(user=user_b)
        resp = self.client.post(_URL, {"name": "Alice"}, format="json")
        self.assertEqual(resp.status_code, 201)

        tenant_a.refresh_from_db()
        tenant_b.refresh_from_db()
        # A untouched; B got the binding.
        self.assertEqual(tenant_a.pii_entity_map, {})
        self.assertEqual(tenant_a.pii_type_counters, {})
        self.assertIn("[PERSON_1]", tenant_b.pii_entity_map)
