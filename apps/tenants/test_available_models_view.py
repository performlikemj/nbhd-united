"""Tests for the available-models picker endpoint.

GET /api/v1/tenants/settings/available-models/

Drives the iOS/web model picker. Must only ever offer models the tenant is
actually allowed to select (the same allowlist PreferredModelView validates
against), so the picker can't present a model the PATCH would 400 on.
"""

from __future__ import annotations

import secrets

from django.test import TestCase
from rest_framework.test import APIClient

from apps.tenants.models import Tenant, User


def _make_user_with_tenant(tier: str = "starter") -> tuple[User, Tenant]:
    user = User.objects.create_user(
        username=f"u_{secrets.token_hex(4)}",
        email=f"{secrets.token_hex(4)}@example.com",
        password="hunter2-test",
    )
    tenant = Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE,
        container_fqdn="container.example.com",
        model_tier=tier,
    )
    return user, tenant


class AvailableModelsViewTests(TestCase):
    URL = "/api/v1/tenants/settings/available-models/"

    def setUp(self):
        self.client = APIClient()

    def test_requires_authentication(self):
        self.assertEqual(self.client.get(self.URL).status_code, 401)

    def test_lists_tier_models_with_labels(self):
        user, tenant = _make_user_with_tenant(tier="starter")
        self.client.force_authenticate(user=user)
        resp = self.client.get(self.URL)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()

        self.assertGreaterEqual(len(data["models"]), 1)
        for entry in data["models"]:
            self.assertTrue(entry["id"])  # every option is selectable
            self.assertTrue(entry["label"])  # …and has a human label
        self.assertEqual(data["model_tier"], "starter")
        self.assertIn("morning_briefing", data["task_slugs"])
        self.assertEqual(data["preferred_model"], tenant.preferred_model or "")

    def test_options_match_the_preferred_model_allowlist(self):
        # Every offered id must be one the PreferredModelView would accept, and
        # an out-of-tier model must never appear.
        from apps.tenants.views import _get_allowed_models

        user, tenant = _make_user_with_tenant(tier="starter")
        self.client.force_authenticate(user=user)
        offered = {m["id"] for m in self.client.get(self.URL).json()["models"]}

        self.assertEqual(offered, set(_get_allowed_models(tenant).keys()))
        self.assertNotIn("gpt-5", offered)

    def test_reflects_current_preferred_model(self):
        user, tenant = _make_user_with_tenant(tier="starter")
        # Pick the first allowed model as the current default.
        from apps.tenants.views import _get_allowed_models

        chosen = next(iter(_get_allowed_models(tenant)))
        tenant.preferred_model = chosen
        tenant.save(update_fields=["preferred_model"])

        self.client.force_authenticate(user=user)
        data = self.client.get(self.URL).json()
        self.assertEqual(data["preferred_model"], chosen)
