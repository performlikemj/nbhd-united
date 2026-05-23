"""Tests for the assistant-callable preferred-model runtime endpoint."""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase
from django.test.utils import override_settings

from apps.billing.constants import (
    ANTHROPIC_OPUS_MODEL,
    ANTHROPIC_SONNET_MODEL,
    DEEPSEEK_MODEL,
    MINIMAX_MODEL,
)
from apps.tenants.services import create_tenant


@override_settings(NBHD_INTERNAL_API_KEY="shared-key")
@patch("apps.tenants.views._enqueue_immediate_apply")
class RuntimePreferredModelViewTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Runtime PM", telegram_chat_id=818181)
        self.other_tenant = create_tenant(display_name="Other PM", telegram_chat_id=828282)
        self.endpoint = f"/api/v1/tenants/runtime/{self.tenant.id}/preferred-model/"

    def _headers(self, tenant_id: str | None = None, key: str = "shared-key") -> dict[str, str]:
        return {
            "HTTP_X_NBHD_INTERNAL_KEY": key,
            "HTTP_X_NBHD_TENANT_ID": tenant_id or str(self.tenant.id),
        }

    # ── Auth ───────────────────────────────────────────────────────────

    def test_get_requires_internal_auth(self, _enq):
        response = self.client.get(self.endpoint)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "internal_auth_failed")

    def test_post_requires_internal_auth(self, _enq):
        response = self.client.post(self.endpoint, data={"model_id": DEEPSEEK_MODEL}, content_type="application/json")
        self.assertEqual(response.status_code, 401)

    def test_rejects_tenant_scope_mismatch(self, _enq):
        response = self.client.get(self.endpoint, **self._headers(tenant_id=str(self.other_tenant.id)))
        self.assertEqual(response.status_code, 401)

    def test_unknown_tenant_returns_404(self, _enq):
        unknown = "00000000-0000-0000-0000-000000000000"
        response = self.client.get(
            f"/api/v1/tenants/runtime/{unknown}/preferred-model/",
            **self._headers(tenant_id=unknown),
        )
        # Tenant lookup fails inside per-tenant key validation before view
        # body — the validator returns 401 because the tenant has no key
        # and the legacy global doesn't match. Either 401 or 404 is
        # acceptable; we just don't want a 500 or silent success.
        self.assertIn(response.status_code, {401, 404})

    # ── GET state ──────────────────────────────────────────────────────

    def test_get_returns_starter_tier_state(self, _enq):
        response = self.client.get(self.endpoint, **self._headers())
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["model_tier"], "starter")
        self.assertEqual(body["preferred_model"], "")
        model_ids = {m["model_id"] for m in body["allowed_models"]}
        self.assertEqual(model_ids, {MINIMAX_MODEL, DEEPSEEK_MODEL, "openrouter/google/gemma-4-31b-it"})
        aliases = {m["alias"] for m in body["allowed_models"]}
        self.assertEqual(aliases, {"minimax", "deepseek", "gemma"})

    # ── POST allowed switch ────────────────────────────────────────────

    def test_post_allowed_model_persists_and_bumps_config(self, mock_enq):
        baseline_version = self.tenant.pending_config_version
        response = self.client.post(
            self.endpoint,
            data={"model_id": DEEPSEEK_MODEL},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["updated"])
        self.assertEqual(body["preferred_model"], DEEPSEEK_MODEL)

        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.preferred_model, DEEPSEEK_MODEL)
        self.assertGreater(self.tenant.pending_config_version, baseline_version)
        mock_enq.assert_called_once()

    def test_post_clear_model(self, mock_enq):
        # Pre-set a model, then clear it
        self.tenant.preferred_model = DEEPSEEK_MODEL
        self.tenant.save(update_fields=["preferred_model"])

        response = self.client.post(
            self.endpoint,
            data={"model_id": ""},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 200)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.preferred_model, "")
        mock_enq.assert_called_once()

    # ── POST forbidden ─────────────────────────────────────────────────

    def test_post_byo_model_without_credential_rejected(self, mock_enq):
        # Starter tier with byo_models_enabled=True but no BYOCredential
        # → Sonnet/Opus must not be in allowed_models and POST must 400.
        self.assertTrue(self.tenant.byo_models_enabled)

        response = self.client.post(
            self.endpoint,
            data={"model_id": ANTHROPIC_OPUS_MODEL},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["error"], "model_not_allowed")
        # Allowed list is returned alongside the error so the assistant can
        # tell the user what's actually available.
        self.assertIn("allowed_models", body)
        model_ids = {m["model_id"] for m in body["allowed_models"]}
        self.assertNotIn(ANTHROPIC_OPUS_MODEL, model_ids)
        self.assertNotIn(ANTHROPIC_SONNET_MODEL, model_ids)
        # Side effects must not fire on rejection
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.preferred_model, "")
        mock_enq.assert_not_called()

    def test_post_unknown_model_rejected(self, mock_enq):
        response = self.client.post(
            self.endpoint,
            data={"model_id": "anthropic/totally-made-up-model"},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "model_not_allowed")
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.preferred_model, "")
        mock_enq.assert_not_called()
