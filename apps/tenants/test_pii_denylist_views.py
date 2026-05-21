"""Tests for the PII denylist settings views.

GET / POST  /api/v1/tenants/settings/pii-denylist/
DELETE      /api/v1/tenants/settings/pii-denylist/<key>/

The denylist is the user's manual lever for #660 — words they've marked
as "not PII for me". These endpoints back the People settings page
"Ignore" / "Re-enable redaction" actions.
"""

from __future__ import annotations

import secrets

from django.test import TestCase
from rest_framework.test import APIClient

from apps.tenants.models import Tenant, User


def _make_user_with_tenant(denylist: dict | None = None) -> tuple[User, Tenant]:
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
    if denylist is not None:
        tenant.pii_denylist = denylist
        tenant.save(update_fields=["pii_denylist"])
    return user, tenant


class PIIDenylistListViewTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_requires_authentication(self):
        resp = self.client.get("/api/v1/tenants/settings/pii-denylist/")
        self.assertEqual(resp.status_code, 401)

    def test_returns_empty_entries_when_denylist_empty(self):
        user, _ = _make_user_with_tenant(denylist={})
        self.client.force_authenticate(user=user)
        resp = self.client.get("/api/v1/tenants/settings/pii-denylist/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"entries": []})

    def test_lists_entries_sorted_by_key(self):
        user, _ = _make_user_with_tenant(
            denylist={
                "calendar": {"reason": "manual", "decided_at": "2026-05-21T10:00:00"},
                "goal": {"reason": "manual"},
            }
        )
        self.client.force_authenticate(user=user)
        resp = self.client.get("/api/v1/tenants/settings/pii-denylist/")
        self.assertEqual(resp.status_code, 200)
        entries = resp.json()["entries"]
        self.assertEqual([e["key"] for e in entries], ["calendar", "goal"])
        self.assertEqual(entries[0]["reason"], "manual")
        self.assertEqual(entries[0]["decided_at"], "2026-05-21T10:00:00")
        # Missing decided_at is surfaced as None (frontend treats as "unknown")
        self.assertIsNone(entries[1]["decided_at"])

    def test_post_adds_canonical_key(self):
        user, tenant = _make_user_with_tenant(denylist={})
        self.client.force_authenticate(user=user)
        resp = self.client.post(
            "/api/v1/tenants/settings/pii-denylist/",
            {"name": "Goal"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["key"], "goal")
        tenant.refresh_from_db()
        self.assertIn("goal", tenant.pii_denylist)
        self.assertEqual(tenant.pii_denylist["goal"]["reason"], "manual")

    def test_post_rejects_empty_name(self):
        user, _ = _make_user_with_tenant()
        self.client.force_authenticate(user=user)
        for body in [{}, {"name": ""}, {"name": "   "}, {"name": None}]:
            resp = self.client.post("/api/v1/tenants/settings/pii-denylist/", body, format="json")
            self.assertEqual(resp.status_code, 400, f"failed body={body!r}")

    def test_post_rejects_non_string_name(self):
        user, _ = _make_user_with_tenant()
        self.client.force_authenticate(user=user)
        resp = self.client.post(
            "/api/v1/tenants/settings/pii-denylist/",
            {"name": 42},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_post_enforces_max_length(self):
        user, _ = _make_user_with_tenant()
        self.client.force_authenticate(user=user)
        resp = self.client.post(
            "/api/v1/tenants/settings/pii-denylist/",
            {"name": "x" * 201},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_post_overwrites_existing_entry_with_fresh_decided_at(self):
        # Re-adding the same word is fine; we just refresh the metadata.
        user, tenant = _make_user_with_tenant(denylist={"goal": {"reason": "manual", "decided_at": "2026-01-01"}})
        self.client.force_authenticate(user=user)
        resp = self.client.post(
            "/api/v1/tenants/settings/pii-denylist/",
            {"name": "goal"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        tenant.refresh_from_db()
        self.assertNotEqual(tenant.pii_denylist["goal"]["decided_at"], "2026-01-01")


class PIIDenylistItemViewTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_requires_authentication(self):
        resp = self.client.delete("/api/v1/tenants/settings/pii-denylist/goal/")
        self.assertEqual(resp.status_code, 401)

    def test_delete_removes_entry(self):
        user, tenant = _make_user_with_tenant(denylist={"goal": {}, "calendar": {}})
        self.client.force_authenticate(user=user)
        resp = self.client.delete("/api/v1/tenants/settings/pii-denylist/goal/")
        self.assertEqual(resp.status_code, 204)
        tenant.refresh_from_db()
        self.assertNotIn("goal", tenant.pii_denylist)
        self.assertIn("calendar", tenant.pii_denylist)

    def test_delete_unknown_key_returns_404(self):
        user, _ = _make_user_with_tenant(denylist={"goal": {}})
        self.client.force_authenticate(user=user)
        resp = self.client.delete("/api/v1/tenants/settings/pii-denylist/unknown/")
        self.assertEqual(resp.status_code, 404)

    def test_tenants_isolated(self):
        # Tenant A puts "goal" on their denylist; Tenant B's DELETE
        # for "goal" must 404, not affect A's data.
        user_a, tenant_a = _make_user_with_tenant(denylist={"goal": {}})
        user_b, _ = _make_user_with_tenant(denylist={})
        self.client.force_authenticate(user=user_b)
        resp = self.client.delete("/api/v1/tenants/settings/pii-denylist/goal/")
        self.assertEqual(resp.status_code, 404)
        tenant_a.refresh_from_db()
        self.assertIn("goal", tenant_a.pii_denylist)
