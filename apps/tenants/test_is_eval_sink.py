"""The ``is_eval_sink`` backfill predicate (migration 0127) and the flag's boundary.

``0127_tenant_is_eval_sink`` adds the field and classifies the EXISTING eval
tenants in one shot. The predicate it uses — the user's email ending in
``@evals.invalid`` — is the whole safety story: it must catch the eval harness's
own tenants and NOTHING else. A backfill that over-matched would silently switch
a paying tenant into the sink, where the assistant records its outbound messages
as eval evidence instead of sending them to the human. The user simply stops
hearing from their assistant, and every health check stays green.

That is not hypothetical. The previous design gated sink behavior on
``is_synthetic`` — which the App Store Review demo account also sets. Apple would
have talked to an assistant whose replies went nowhere. The demo fixture below (a
synthetic tenant with a non-eval email) pins that regression shut.

The migration module is loaded from disk BY PATH (its filename starts with a
digit, so it is not importable by module name) and its forward/reverse functions
are called against the real app registry. That exercises the SHIPPING predicate
rather than a re-declaration of it — a test that re-typed the ``iendswith``
filter would pass even if the migration's own filter were wrong.
"""

from __future__ import annotations

import importlib.util
import secrets
from pathlib import Path

from django.apps import apps as django_apps
from django.conf import settings
from django.test import TestCase

from apps.tenants.models import Tenant, User

_MIGRATION_PATH = Path(settings.BASE_DIR) / "apps" / "tenants" / "migrations" / "0127_tenant_is_eval_sink.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location("tenants_0127_tenant_is_eval_sink", _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration = _load_migration()


def _tenant(email: str, *, synthetic: bool = False) -> Tenant:
    user = User.objects.create_user(username=f"{email}-{secrets.token_hex(3)}", email=email)
    return Tenant.objects.create(user=user, is_synthetic=synthetic)


class IsEvalSinkFlagTest(TestCase):
    def test_defaults_to_false(self):
        self.assertFalse(_tenant("real@e.com").is_eval_sink)

    def test_synthetic_does_not_imply_eval_sink(self):
        """The un-overloading, stated as an assertion: the two flags are independent."""
        self.assertFalse(_tenant("demo@e.com", synthetic=True).is_eval_sink)


class IsEvalSinkBackfillTest(TestCase):
    """Runs the REAL migration functions from ``0127_tenant_is_eval_sink``."""

    def test_backfill_flips_only_the_eval_harness_tenants(self):
        harness = _tenant("behavior@evals.invalid", synthetic=True)
        harness_upper = _tenant("JOURNEY@EVALS.INVALID", synthetic=True)  # predicate is case-INsensitive
        demo = _tenant("demo@example.com", synthetic=True)  # the App Store Review demo account
        real = _tenant("subscriber@example.com")

        migration.backfill_existing_eval_sinks(django_apps, None)

        for t in (harness, harness_upper, demo, real):
            t.refresh_from_db()

        self.assertTrue(harness.is_eval_sink)
        self.assertTrue(harness_upper.is_eval_sink, "iendswith: an upper-case eval address is still an eval tenant")
        # THE REGRESSION THIS FIELD EXISTS TO PREVENT. If is_synthetic ever implies
        # the sink again, the App Review demo assistant goes silent on Apple.
        self.assertFalse(demo.is_eval_sink, "a synthetic demo account must NOT be backfilled into the sink")
        self.assertFalse(real.is_eval_sink)

    def test_reverse_unsets_the_backfilled_tenants(self):
        harness = _tenant("behavior@evals.invalid", synthetic=True)

        migration.backfill_existing_eval_sinks(django_apps, None)
        harness.refresh_from_db()
        self.assertTrue(harness.is_eval_sink)  # confirm it was set before clearing

        migration.clear_backfilled_eval_sinks(django_apps, None)
        harness.refresh_from_db()
        self.assertFalse(harness.is_eval_sink)
