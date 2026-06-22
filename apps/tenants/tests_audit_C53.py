"""Audit cluster C53 regression tests — entitled_active() ghost-tenant exclusion.

Covers FA-1136: entitled_active() used negative-exclude logic that missed
"ghost" tenants (is_trial=False, status=ACTIVE, no stripe_subscription_id,
not budget-exempt). Those ghosts should be excluded from entitled_active()
because has_entitlement returns False for them.

Ensures entitled_active() is the exact positive inverse of
_unentitled_active_tenants() in apps/cron/views.py.
"""

from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.tenants.models import Tenant, User


def _make_user(email: str) -> User:
    user = User.objects.create(username=email, email=email, display_name="Test")
    user.set_password("pw")
    user.save()
    return user


def _make_tenant(user: User, **kwargs) -> Tenant:
    """Create a tenant with a container_id so it qualifies for entitled_active container filter."""
    defaults = dict(
        status=Tenant.Status.ACTIVE,
        container_id="oc-fake-container",
        is_trial=False,
        stripe_subscription_id="",
        is_budget_exempt=False,
    )
    defaults.update(kwargs)
    return Tenant.objects.create(user=user, **defaults)


class EntitledActiveGhostExclusionTests(TestCase):
    """entitled_active() must exclude ghost tenants that have no real entitlement."""

    def test_ghost_tenant_excluded(self):
        """A non-trial active tenant with no subscription is a ghost — must be excluded."""
        user = _make_user("ghost@test.com")
        _make_tenant(
            user,
            is_trial=False,
            stripe_subscription_id="",
            is_budget_exempt=False,
        )
        qs = Tenant.entitled_active()
        self.assertFalse(
            qs.filter(user=user).exists(),
            "Ghost tenant (is_trial=False, no subscription, not exempt) must not appear in entitled_active()",
        )

    def test_paid_tenant_included(self):
        """A tenant with a Stripe subscription is entitled and must be included."""
        user = _make_user("paid@test.com")
        _make_tenant(
            user,
            is_trial=False,
            stripe_subscription_id="sub_abc123",
            is_budget_exempt=False,
        )
        qs = Tenant.entitled_active()
        self.assertTrue(
            qs.filter(user=user).exists(),
            "Paid tenant (stripe_subscription_id set) must appear in entitled_active()",
        )

    def test_unexpired_trial_included(self):
        """A tenant on a valid unexpired trial is entitled and must be included."""
        user = _make_user("trial@test.com")
        _make_tenant(
            user,
            is_trial=True,
            trial_ends_at=timezone.now() + timedelta(days=3),
            stripe_subscription_id="",
            is_budget_exempt=False,
        )
        qs = Tenant.entitled_active()
        self.assertTrue(
            qs.filter(user=user).exists(),
            "Unexpired-trial tenant must appear in entitled_active()",
        )

    def test_expired_trial_excluded(self):
        """A tenant whose trial has ended and has no subscription must be excluded."""
        user = _make_user("expired@test.com")
        _make_tenant(
            user,
            is_trial=True,
            trial_ends_at=timezone.now() - timedelta(days=1),
            stripe_subscription_id="",
            is_budget_exempt=False,
        )
        qs = Tenant.entitled_active()
        self.assertFalse(
            qs.filter(user=user).exists(),
            "Expired-trial tenant with no subscription must not appear in entitled_active()",
        )

    def test_budget_exempt_included(self):
        """Budget-exempt tenants (canary/internal) are entitled regardless of subscription."""
        user = _make_user("canary@test.com")
        _make_tenant(
            user,
            is_trial=False,
            stripe_subscription_id="",
            is_budget_exempt=True,
        )
        qs = Tenant.entitled_active()
        self.assertTrue(
            qs.filter(user=user).exists(),
            "Budget-exempt tenant must appear in entitled_active() even without a subscription",
        )

    def test_suspended_tenant_excluded(self):
        """Suspended tenants are always excluded regardless of entitlement fields."""
        user = _make_user("suspended@test.com")
        _make_tenant(
            user,
            status=Tenant.Status.SUSPENDED,
            stripe_subscription_id="sub_suspended",
            is_budget_exempt=False,
        )
        qs = Tenant.entitled_active()
        self.assertFalse(
            qs.filter(user=user).exists(),
            "Suspended tenant must not appear in entitled_active()",
        )

    def test_no_container_excluded(self):
        """Active entitled tenant without a container_id is excluded (not fully provisioned)."""
        user = _make_user("nocontainer@test.com")
        _make_tenant(
            user,
            container_id="",
            stripe_subscription_id="sub_nocontainer",
            is_budget_exempt=False,
        )
        qs = Tenant.entitled_active()
        self.assertFalse(
            qs.filter(user=user).exists(),
            "Active entitled tenant with no container_id must not appear in entitled_active()",
        )

    def test_has_entitlement_and_entitled_active_agree_for_ghost(self):
        """has_entitlement returns False for a ghost; entitled_active() must also exclude it."""
        user = _make_user("ghost2@test.com")
        tenant = _make_tenant(
            user,
            is_trial=False,
            stripe_subscription_id="",
            is_budget_exempt=False,
        )
        self.assertFalse(
            tenant.has_entitlement,
            "Ghost tenant's has_entitlement must be False",
        )
        self.assertFalse(
            Tenant.entitled_active().filter(pk=tenant.pk).exists(),
            "Ghost tenant must be excluded from entitled_active() — has_entitlement and entitled_active() must agree",
        )
