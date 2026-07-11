"""Tests for Tenant.is_synthetic (eval-system synthetic tenants, Wave A2).

Synthetic tenants are EXCLUDED from business aggregates (campaign audiences,
donation pledges, infra-cost denominator, ...) but behave EXACTLY like real
tenants in operational queries (they still provision, cron, hibernate, wake).
See docs/evals-directive.md.
"""

from __future__ import annotations

import secrets

from django.test import TestCase

from apps.tenants.models import Tenant, User


def _tenant(email: str, *, synthetic: bool = False, **kw) -> Tenant:
    user = User.objects.create_user(username=f"{email}-{secrets.token_hex(3)}", email=email)
    return Tenant.objects.create(user=user, is_synthetic=synthetic, **kw)


class IsSyntheticFlagTest(TestCase):
    def test_default_false(self):
        self.assertFalse(_tenant("real@e.com").is_synthetic)


class BusinessExclusionTest(TestCase):
    def test_campaign_audience_excludes_synthetic(self):
        # Representative business exclusion: the promo/comeback send list.
        from apps.tenants.management.commands.send_promo_campaign import AUDIENCE_COMEBACK, Command

        _tenant("real@e.com", onboarding_complete=True, status=Tenant.Status.ACTIVE)
        _tenant("synthetic@e.com", synthetic=True, onboarding_complete=True, status=Tenant.Status.ACTIVE)

        emails = set(Command()._build_audience_qs(AUDIENCE_COMEBACK, owner_email="").values_list("email", flat=True))

        self.assertIn("real@e.com", emails)
        self.assertNotIn("synthetic@e.com", emails)  # never emailed a marketing blast

    def test_donation_snapshot_excludes_synthetic(self):
        # Exercise the REAL snapshot function (not an inline re-declaration of its
        # candidate query) so this guards the PRODUCTION gate: if the
        # .exclude(is_synthetic) at donation_service candidate selection is ever
        # deleted, the synthetic payer gets a real pledge row and this fails.
        # Mocking the Stripe price so BOTH payers verify makes is_synthetic the
        # ONLY discriminator between them.
        from datetime import date
        from unittest.mock import patch

        from apps.billing.donation_service import snapshot_donations_for_month
        from apps.billing.models import DonationLedger

        real = _tenant("real-payer@e.com", status=Tenant.Status.ACTIVE, stripe_subscription_id="sub_real")
        synth = _tenant(
            "synth-payer@e.com", synthetic=True, status=Tenant.Status.ACTIVE, stripe_subscription_id="sub_synth"
        )
        month = date(2026, 6, 1)

        with patch(
            "apps.billing.usage_services._get_subscription_price_with_source",
            return_value=(12.0, "stripe"),
        ):
            snapshot_donations_for_month(month)

        # The money invariant: the synthetic payer gets NO pledge row.
        self.assertFalse(DonationLedger.objects.filter(tenant=synth, month=month).exists())
        # ...and the real payer DID get a PENDING row — so the synthetic-absence
        # is a real gate, not a vacuous "nobody got booked".
        self.assertTrue(
            DonationLedger.objects.filter(tenant=real, month=month, status=DonationLedger.Status.PENDING).exists()
        )


class OperationalUnaffectedTest(TestCase):
    def test_synthetic_still_in_entitled_active(self):
        # entitled_active() is THE operational inclusion query (config apply, cron
        # seeding, broadcasts). A synthetic tenant must be treated exactly like a
        # real one here — synthetic tenants provision/cron/hibernate normally.
        synth = _tenant(
            "synthetic@e.com",
            synthetic=True,
            status=Tenant.Status.ACTIVE,
            container_id="oc-synthetic",
            is_budget_exempt=True,
        )
        self.assertIn(synth.id, set(Tenant.entitled_active().values_list("id", flat=True)))
