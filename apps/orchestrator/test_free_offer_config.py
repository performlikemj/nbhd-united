"""Tests for free-offer-aware model resolution in config_generator."""

import re
from pathlib import Path

from django.test import TestCase

from apps.billing.constants import DEEPSEEK_FLASH_MODEL, DEEPSEEK_MODEL, GEMMA_MODEL, NEMOTRON_FREE_MODEL
from apps.billing.models import FreeModelOffer
from apps.orchestrator.config_generator import TIER_MODELS, effective_primary_model, resolve_tenant_models
from apps.tenants.models import Tenant, User


def _tenant(preferred=""):
    user = User.objects.create_user(username="cfg-user", password="x" * 32)
    return Tenant.objects.create(user=user, model_tier="starter", preferred_model=preferred)


def _activate():
    offer = FreeModelOffer.load()
    offer.is_active = True
    offer.save(update_fields=["is_active"])


class ResolveTenantModelsTest(TestCase):
    def test_inactive_offer_uses_pro_tier_primary_and_preserves_explicit_choices(self):
        tenant = _tenant()
        models_config, entries, fallbacks = resolve_tenant_models(tenant)
        self.assertEqual(models_config["primary"], DEEPSEEK_MODEL)
        self.assertNotIn(NEMOTRON_FREE_MODEL, entries)
        self.assertNotIn(NEMOTRON_FREE_MODEL, fallbacks)
        self.assertEqual(effective_primary_model(tenant), DEEPSEEK_MODEL)

        tenant.preferred_model = DEEPSEEK_FLASH_MODEL
        tenant.save(update_fields=["preferred_model"])
        models_config, _entries, _fallbacks = resolve_tenant_models(tenant)
        self.assertEqual(models_config["primary"], DEEPSEEK_FLASH_MODEL)
        self.assertEqual(effective_primary_model(tenant), DEEPSEEK_FLASH_MODEL)

        tenant.preferred_model = GEMMA_MODEL
        tenant.save(update_fields=["preferred_model"])
        models_config, _entries, _fallbacks = resolve_tenant_models(tenant)
        self.assertEqual(models_config["primary"], GEMMA_MODEL)
        self.assertEqual(effective_primary_model(tenant), GEMMA_MODEL)

    def test_frontend_default_matches_backend_tier_primary(self):
        models_ts = Path(__file__).resolve().parents[2] / "frontend" / "lib" / "models.ts"
        match = re.search(r'export const DEFAULT_MODEL = "([^"]+)";', models_ts.read_text())
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), TIER_MODELS["starter"]["primary"])

    def test_active_offer_is_primary_with_deepseek_fallback_first(self):
        _activate()
        tenant = _tenant()
        models_config, entries, fallbacks = resolve_tenant_models(tenant)
        self.assertEqual(models_config["primary"], NEMOTRON_FREE_MODEL)
        self.assertIn(NEMOTRON_FREE_MODEL, entries)
        # Offer model is primary, so it's not in its own fallback list, and the
        # configured paid fallback leads the chain.
        self.assertNotIn(NEMOTRON_FREE_MODEL, fallbacks)
        self.assertEqual(fallbacks[0], DEEPSEEK_MODEL)
        self.assertEqual(effective_primary_model(tenant), NEMOTRON_FREE_MODEL)

    def test_explicit_preferred_model_wins_over_offer(self):
        _activate()
        tenant = _tenant(preferred=DEEPSEEK_FLASH_MODEL)
        models_config, _entries, fallbacks = resolve_tenant_models(tenant)
        self.assertEqual(models_config["primary"], DEEPSEEK_FLASH_MODEL)
        # NEMOTRON still selectable in the allowlist, so it appears as a fallback,
        # but DeepSeek is not force-led since the primary isn't the offer model.
        self.assertIn(NEMOTRON_FREE_MODEL, fallbacks)
        self.assertEqual(effective_primary_model(tenant), DEEPSEEK_FLASH_MODEL)
