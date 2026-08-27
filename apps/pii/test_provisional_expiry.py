from __future__ import annotations

import secrets
from datetime import UTC, datetime
from io import StringIO

from django.core.management import call_command
from django.test import TestCase, override_settings

from apps.pii.junk_sweep import _classify
from apps.pii.provisional import PiiIngress, record_provisional_sightings
from apps.pii.provisional_expiry import expire_provisional_bindings_task, sweep_tenant
from apps.tenants.models import Tenant, User


def _tenant(entity_map: dict, *, denylist: dict | None = None) -> Tenant:
    user = User.objects.create_user(
        username=f"u_{secrets.token_hex(4)}",
        email=f"{secrets.token_hex(4)}@example.com",
        password="fixture-password",
    )
    return Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE,
        container_fqdn="container.example.com",
        pii_entity_map=entity_map,
        pii_denylist=denylist or {},
    )


class ProvisionalExpiryTests(TestCase):
    NOW = datetime(2026, 8, 28, 12, tzinfo=UTC)

    def test_expires_only_stale_unpromoted_provisional(self):
        tenant = _tenant(
            {
                "[PERSON_1]": {
                    "name": "Fakenamealpha",
                    "provisional": True,
                    "last_seen_at": "2026-08-24T00:00:00+00:00",
                    "future_field": "fixture",
                },
                "[PERSON_2]": {
                    "name": "Fakenamesigma",
                    "provisional": True,
                    "last_seen_at": "2026-08-28T11:00:00+00:00",
                },
                "[PERSON_3]": {
                    "name": "Fakenameomega",
                    "provisional": False,
                    "promoted_at": "2026-08-24T00:00:00+00:00",
                    "last_seen_at": "2026-08-24T00:00:00+00:00",
                },
            }
        )
        result = sweep_tenant(tenant, now=self.NOW)
        self.assertEqual(result["expired"], 1)
        tenant.refresh_from_db()
        expired = tenant.pii_entity_map["[PERSON_1]"]
        self.assertEqual(expired["retired_reason"], "provisional-expired")
        self.assertEqual(expired["future_field"], "fixture")
        self.assertNotIn("retired", tenant.pii_entity_map["[PERSON_2]"])
        self.assertNotIn("retired", tenant.pii_entity_map["[PERSON_3]"])

    def test_reappearance_reactivates_same_placeholder_and_restarts_fuse(self):
        tenant = _tenant(
            {
                "[PERSON_1]": {
                    "name": "Fakenamealpha",
                    "provisional": True,
                    "last_seen_at": "2026-08-20T00:00:00+00:00",
                    "retired": True,
                    "retired_at": "2026-08-24T00:00:00+00:00",
                    "retired_reason": "provisional-expired",
                    "seen_events": ["0" * 32],
                    "seen_dates": ["2026-08-20"],
                }
            }
        )
        ingress = PiiIngress(channel="fixture", provider_event_id="return-1", occurred_at=self.NOW)
        result = record_provisional_sightings(tenant, "Fakenamealpha returned", ingress)[0]
        self.assertEqual(result.outcome, "counted")
        tenant.refresh_from_db()
        entry = tenant.pii_entity_map["[PERSON_1]"]
        self.assertFalse(entry.get("retired", False))
        self.assertEqual(entry["first_seen_at"], self.NOW.isoformat())
        self.assertEqual(len(entry["seen_events"]), 1)

    def test_reactivation_never_overrides_denylist(self):
        tenant = _tenant(
            {
                "[PERSON_1]": {
                    "name": "Fakenamealpha",
                    "provisional": True,
                    "retired": True,
                    "retired_reason": "provisional-expired",
                }
            },
            denylist={"fakenamealpha": {"reason": "fixture"}},
        )
        ingress = PiiIngress(channel="fixture", provider_event_id="return-1", occurred_at=self.NOW)
        result = record_provisional_sightings(tenant, "Fakenamealpha returned", ingress)[0]
        self.assertEqual(result.outcome, "blocked")

    def test_junk_sweep_skips_provisional(self):
        summary, junk = _classify(
            {"[PERSON_1]": {"name": "|---|", "provisional": True}},
            max_entries=500,
        )
        self.assertEqual(junk, {})
        self.assertEqual(summary["skipped"], 1)

    @override_settings(PII_PROVISIONAL_SWEEP_ENABLED=False)
    def test_cron_gate_is_independent(self):
        self.assertEqual(expire_provisional_bindings_task(), {"disabled": 1})

    def test_rollback_promote_all_and_retire_all(self):
        promoted = _tenant(
            {"[PERSON_1]": {"name": "Fakenamealpha", "provisional": True, "last_seen_at": self.NOW.isoformat()}}
        )
        call_command("expire_provisional_bindings", "--promote-all", stdout=StringIO())
        promoted.refresh_from_db()
        self.assertFalse(promoted.pii_entity_map["[PERSON_1]"]["provisional"])

        retired = _tenant(
            {"[PERSON_1]": {"name": "Fakenamesigma", "provisional": True, "last_seen_at": self.NOW.isoformat()}}
        )
        call_command("expire_provisional_bindings", "--retire-all", stdout=StringIO())
        retired.refresh_from_db()
        self.assertEqual(retired.pii_entity_map["[PERSON_1]"]["retired_reason"], "rollback")
