"""Tests for the tier-2 PII review-queue settings views.

GET  /api/v1/tenants/settings/pii-review-queue/
POST /api/v1/tenants/settings/pii-review-queue/keep/

The queue surfaces the PERSON_*/LOCATION_* bindings the assistant is hiding
that the user has not yet judged (no ``reviewed_at`` stamp). "Keep" stamps
reviewed_at so the entry drops out of the queue; "clean" reuses the existing
entity-registry bulk-delete with ``deny=true`` (covered by that view's tests).
This is a privacy surface: every test confirms a caller can only ever read /
mutate their own tenant's map.
"""

from __future__ import annotations

import secrets

from django.test import TestCase
from rest_framework.test import APIClient

from apps.pii.entity_registry import coerce
from apps.tenants.models import Tenant, User

_QUEUE_URL = "/api/v1/tenants/settings/pii-review-queue/"
_KEEP_URL = "/api/v1/tenants/settings/pii-review-queue/keep/"


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


class PIIReviewQueueGetTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_requires_authentication(self):
        resp = self.client.get(_QUEUE_URL)
        self.assertEqual(resp.status_code, 401)

    def test_returns_only_unreviewed_person_and_location(self):
        # Mixed map: two unreviewed PERSON/LOCATION spans, one already-kept
        # PERSON (reviewed_at present), and a high-precision EMAIL_ADDRESS that
        # the tier-2 flow never queues.
        user, _ = _make_user_with_tenant(
            entity_map={
                "[PERSON_1]": {"name": "Alice", "relationship": "friend"},
                "[LOCATION_2]": {"name": "Osaka"},
                "[PERSON_3]": {"name": "Bob", "reviewed_at": "2026-01-01T00:00:00+00:00"},
                "[EMAIL_ADDRESS_4]": {"name": "a@example.com"},
            }
        )
        self.client.force_authenticate(user=user)
        resp = self.client.get(_QUEUE_URL)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        placeholders = {e["placeholder"] for e in body["entries"]}
        self.assertEqual(placeholders, {"[PERSON_1]", "[LOCATION_2]"})
        self.assertEqual(body["total"], 2)
        # Metadata rides along so the review row can show context.
        alice = next(e for e in body["entries"] if e["placeholder"] == "[PERSON_1]")
        self.assertEqual(alice["name"], "Alice")
        self.assertEqual(alice["relationship"], "friend")
        self.assertEqual(alice["notes"], "")

    def test_orders_newest_placeholder_first(self):
        user, _ = _make_user_with_tenant(
            entity_map={
                "[PERSON_2]": {"name": "Two"},
                "[PERSON_10]": {"name": "Ten"},
                "[PERSON_1]": {"name": "One"},
            }
        )
        self.client.force_authenticate(user=user)
        resp = self.client.get(_QUEUE_URL)
        order = [e["placeholder"] for e in resp.json()["entries"]]
        self.assertEqual(order, ["[PERSON_10]", "[PERSON_2]", "[PERSON_1]"])

    def test_legacy_bare_string_entries_are_queued(self):
        # Pre-registry map values are bare strings, not dicts — they have no
        # reviewed_at, so they belong in the queue.
        user, _ = _make_user_with_tenant(entity_map={"[PERSON_1]": "Nana"})
        self.client.force_authenticate(user=user)
        resp = self.client.get(_QUEUE_URL)
        body = resp.json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["entries"][0]["name"], "Nana")

    def test_empty_map_returns_empty_queue(self):
        user, _ = _make_user_with_tenant(entity_map={})
        self.client.force_authenticate(user=user)
        resp = self.client.get(_QUEUE_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"entries": [], "total": 0})

    def test_caps_entries_but_reports_full_total(self):
        # More than the 200 cap of unreviewed spans: entries is capped, total
        # is the true backlog so the UI can say "hiding N values".
        big_map = {f"[PERSON_{i}]": {"name": f"P{i}"} for i in range(1, 251)}
        user, _ = _make_user_with_tenant(entity_map=big_map)
        self.client.force_authenticate(user=user)
        resp = self.client.get(_QUEUE_URL)
        body = resp.json()
        self.assertEqual(body["total"], 250)
        self.assertEqual(len(body["entries"]), 200)
        # Newest-first: the top of the page is the highest-numbered mint.
        self.assertEqual(body["entries"][0]["placeholder"], "[PERSON_250]")


class PIIReviewQueueKeepTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_requires_authentication(self):
        resp = self.client.post(_KEEP_URL, {"placeholders": ["[PERSON_1]"]}, format="json")
        self.assertEqual(resp.status_code, 401)

    def test_keep_stamps_reviewed_at_and_removes_from_queue(self):
        user, tenant = _make_user_with_tenant(
            entity_map={
                "[PERSON_1]": {"name": "Alice"},
                "[LOCATION_2]": {"name": "Osaka"},
            }
        )
        self.client.force_authenticate(user=user)
        resp = self.client.post(_KEEP_URL, {"placeholders": ["[PERSON_1]"]}, format="json")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["kept"], ["[PERSON_1]"])
        self.assertEqual(body["not_found"], [])

        tenant.refresh_from_db()
        # reviewed_at now stamped on the kept entry, absent on the other.
        self.assertTrue(coerce(tenant.pii_entity_map["[PERSON_1]"]).get("reviewed_at"))
        self.assertFalse(coerce(tenant.pii_entity_map["[LOCATION_2]"]).get("reviewed_at"))

        # A subsequent queue read no longer includes the kept entry.
        queue = self.client.get(_QUEUE_URL).json()
        self.assertEqual({e["placeholder"] for e in queue["entries"]}, {"[LOCATION_2]"})
        self.assertEqual(queue["total"], 1)

    def test_keep_preserves_existing_metadata(self):
        user, tenant = _make_user_with_tenant(
            entity_map={
                "[PERSON_1]": {
                    "name": "Alice",
                    "relationship": "sister",
                    "notes": "loves hiking",
                    "arbiter_judged_at": "2026-01-01T00:00:00+00:00",
                }
            }
        )
        self.client.force_authenticate(user=user)
        self.client.post(_KEEP_URL, {"placeholders": ["[PERSON_1]"]}, format="json")

        tenant.refresh_from_db()
        entry = coerce(tenant.pii_entity_map["[PERSON_1]"])
        self.assertEqual(entry["relationship"], "sister")
        self.assertEqual(entry["notes"], "loves hiking")
        self.assertEqual(entry["arbiter_judged_at"], "2026-01-01T00:00:00+00:00")
        self.assertTrue(entry.get("reviewed_at"))

    def test_keep_reports_not_found(self):
        user, _ = _make_user_with_tenant(entity_map={"[PERSON_1]": {"name": "Alice"}})
        self.client.force_authenticate(user=user)
        resp = self.client.post(
            _KEEP_URL,
            {"placeholders": ["[PERSON_1]", "[PERSON_99]"]},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["kept"], ["[PERSON_1]"])
        self.assertEqual(body["not_found"], ["[PERSON_99]"])

    def test_keep_rejects_bad_bodies(self):
        user, _ = _make_user_with_tenant(entity_map={})
        self.client.force_authenticate(user=user)
        for bad in [{"placeholders": "[PERSON_1]"}, {"placeholders": None}, {}, {"placeholders": []}]:
            resp = self.client.post(_KEEP_URL, bad, format="json")
            self.assertEqual(resp.status_code, 400, f"failed body={bad!r}")

    def test_keep_rejects_non_string_element(self):
        user, _ = _make_user_with_tenant(entity_map={"[PERSON_1]": {"name": "Alice"}})
        self.client.force_authenticate(user=user)
        resp = self.client.post(_KEEP_URL, {"placeholders": ["[PERSON_1]", 42]}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_keep_rejects_oversized_batch(self):
        user, _ = _make_user_with_tenant(entity_map={})
        self.client.force_authenticate(user=user)
        resp = self.client.post(
            _KEEP_URL,
            {"placeholders": [f"[PERSON_{i}]" for i in range(1001)]},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_tenants_isolated(self):
        # Tenant A owns [PERSON_1]; Tenant B's keep for the same placeholder
        # must not stamp A's entry.
        _, tenant_a = _make_user_with_tenant(entity_map={"[PERSON_1]": {"name": "Alice"}})
        user_b, tenant_b = _make_user_with_tenant(entity_map={"[PERSON_1]": {"name": "Bob"}})
        self.client.force_authenticate(user=user_b)
        resp = self.client.post(_KEEP_URL, {"placeholders": ["[PERSON_1]"]}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["kept"], ["[PERSON_1]"])

        tenant_a.refresh_from_db()
        tenant_b.refresh_from_db()
        # A untouched (still unreviewed); B stamped.
        self.assertFalse(coerce(tenant_a.pii_entity_map["[PERSON_1]"]).get("reviewed_at"))
        self.assertTrue(coerce(tenant_b.pii_entity_map["[PERSON_1]"]).get("reviewed_at"))

    def test_queue_isolated_between_tenants(self):
        _, tenant_a = _make_user_with_tenant(entity_map={"[PERSON_1]": {"name": "Alice"}})
        user_b, _ = _make_user_with_tenant(entity_map={"[PERSON_5]": {"name": "Bob"}})
        self.client.force_authenticate(user=user_b)
        queue = self.client.get(_QUEUE_URL).json()
        self.assertEqual({e["placeholder"] for e in queue["entries"]}, {"[PERSON_5]"})
