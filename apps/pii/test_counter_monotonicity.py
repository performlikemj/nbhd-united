"""Placeholder-number monotonicity tests (PII counter high-water mark).

Placeholders are minted as ``[TYPE_N]``. ``N`` used to be re-derived purely from
``max(pii_entity_map suffix per type) + 1``. Deleting a binding (bulk-delete,
junk sweep) lowered that max, so a freed number was RECYCLED onto a different
value — in prod ``[ACCOUNT_4]`` was a temperature range one morning and a
shipping-tracking number by afternoon, and stale ``[ACCOUNT_4]`` tokens sitting
in agent-side workspace files then rehydrated to the WRONG new value.

The fix: ``Tenant.pii_type_counters`` is a per-type monotonic high-water mark
that never drops on deletion, and every mint numbers from
``max(map-derived, stored counter) + 1``. These tests pin that a freed number is
never reachable again, that the counter persists across mints, that the data
migration seeds correctly, that deletion paths leave the counter intact, and
that a legacy tenant with no counters still mints from the map maxima.

``_detect_pii`` is stubbed (no ONNX model needed), matching apps/pii/test_mint_gating.
All fixtures use throwaway values ("Bob", "Alice", example.com).
"""

from __future__ import annotations

from importlib import import_module
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from apps.pii.junk_sweep import sweep_tenant
from apps.pii.redactor import DetectedEntity, redact_user_message
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant

_BULK_URL = "/api/v1/tenants/settings/entity-registry/bulk/"

# The seeding logic lives in a migration whose module name starts with a digit,
# so it can't be a normal ``import`` — import_module takes the dotted string.
_seed_migration = import_module("apps.tenants.migrations.0109_seed_pii_type_counters")


def _make_tenant(*, chat_id: int, entity_map=None, counters=None, denylist=None) -> Tenant:
    """A tenant with a controlled map / counters / denylist, freshly reloaded."""
    tenant = create_tenant(display_name="Test User", telegram_chat_id=chat_id)
    Tenant.objects.filter(pk=tenant.pk).update(
        pii_entity_map=entity_map or {},
        pii_type_counters=counters or {},
        pii_denylist=denylist or {},
    )
    tenant.refresh_from_db()
    return tenant


def _person(start: int, end: int) -> list[DetectedEntity]:
    """A single stubbed PERSON detection over ``text[start:end]``."""
    return [DetectedEntity("PERSON", start, end, 0.99)]


class FreedNumberNeverRecycledTests(TestCase):
    """Task (a): mint → delete via map manipulation → next NEW mint jumps past
    the freed number instead of reusing it."""

    def test_deleted_number_is_not_reissued_to_a_new_name(self):
        tenant = _make_tenant(chat_id=41001, entity_map={}, counters={})

        # Mint "Bob" -> [PERSON_1]; high-water recorded.
        with patch("apps.pii.redactor._detect_pii", return_value=_person(0, 3)):
            out = redact_user_message("Bob said hi", tenant)
        self.assertIn("[PERSON_1]", out)
        tenant.refresh_from_db()
        self.assertEqual(tenant.pii_type_counters, {"PERSON": 1})

        # Delete the binding the way the real paths do — drop it from the map,
        # leave the counter untouched (targeted .update on pii_entity_map only).
        Tenant.objects.filter(pk=tenant.pk).update(pii_entity_map={})
        tenant.refresh_from_db()
        self.assertEqual(tenant.pii_entity_map, {})
        self.assertEqual(tenant.pii_type_counters, {"PERSON": 1})  # never lowered

        # Mint a DIFFERENT name — must NOT recycle the freed [PERSON_1].
        with patch("apps.pii.redactor._detect_pii", return_value=_person(0, 5)):
            out = redact_user_message("Alice said hi", tenant)
        self.assertIn("[PERSON_2]", out)
        self.assertNotIn("[PERSON_1]", out)
        tenant.refresh_from_db()
        self.assertEqual(tenant.pii_entity_map, {"[PERSON_2]": {"name": "Alice"}})
        self.assertEqual(tenant.pii_type_counters, {"PERSON": 2})


class CounterPersistsAcrossMintsTests(TestCase):
    """Task (b): the high-water survives a DB reload and rises with each mint."""

    def test_counter_advances_and_survives_reload(self):
        tenant = _make_tenant(chat_id=41010, entity_map={}, counters={})

        with patch("apps.pii.redactor._detect_pii", return_value=_person(0, 3)):
            redact_user_message("Bob said hi", tenant)
        with patch("apps.pii.redactor._detect_pii", return_value=_person(0, 5)):
            redact_user_message("Alice said hi", tenant)

        # Reload a completely fresh instance from the DB.
        reloaded = Tenant.objects.get(pk=tenant.pk)
        self.assertEqual(reloaded.pii_type_counters, {"PERSON": 2})
        self.assertEqual(
            set(reloaded.pii_entity_map),
            {"[PERSON_1]", "[PERSON_2]"},
        )

    def test_mint_carries_unrelated_stored_types_forward(self):
        # A stored counter for a type not touched by this mint must survive the
        # write (locked_counters is seeded from stored_counters).
        tenant = _make_tenant(chat_id=41011, entity_map={}, counters={"EMAIL_ADDRESS": 9})
        with patch("apps.pii.redactor._detect_pii", return_value=_person(0, 3)):
            redact_user_message("Bob said hi", tenant)
        tenant.refresh_from_db()
        self.assertEqual(tenant.pii_type_counters, {"EMAIL_ADDRESS": 9, "PERSON": 1})


class DataMigrationSeedingTests(TestCase):
    """Task (c): the migration's seeding function, called directly."""

    def test_max_suffixes_per_type_parses_and_ignores_malformed(self):
        counters = _seed_migration._max_suffixes_per_type(
            {
                "[PERSON_3]": {"name": "A"},
                "[PERSON_7]": {"name": "B"},  # higher wins
                "[EMAIL_ADDRESS_2]": {"name": "a@b.co"},
                "legacy-bare-key": "ignored",  # not a placeholder
                "[malformed]": "ignored",
            }
        )
        self.assertEqual(counters, {"PERSON": 7, "EMAIL_ADDRESS": 2})

    def test_seed_counters_fills_from_map_maxima(self):
        tenant = _make_tenant(
            chat_id=41020,
            entity_map={"[PERSON_5]": {"name": "X"}, "[EMAIL_ADDRESS_2]": {"name": "a@b.co"}},
            counters={},  # pre-migration shape
        )
        updated = _seed_migration.seed_counters(Tenant)
        self.assertGreaterEqual(updated, 1)
        tenant.refresh_from_db()
        self.assertEqual(tenant.pii_type_counters, {"PERSON": 5, "EMAIL_ADDRESS": 2})

    def test_seed_counters_skips_already_populated(self):
        # A tenant whose counters were already advanced (a mint between deploy and
        # migration) must not be clobbered back down to the map max.
        tenant = _make_tenant(
            chat_id=41021,
            entity_map={"[PERSON_5]": {"name": "X"}},
            counters={"PERSON": 12},
        )
        _seed_migration.seed_counters(Tenant)
        tenant.refresh_from_db()
        self.assertEqual(tenant.pii_type_counters, {"PERSON": 12})

    def test_seed_counters_leaves_empty_map_tenant_untouched(self):
        tenant = _make_tenant(chat_id=41022, entity_map={}, counters={})
        _seed_migration.seed_counters(Tenant)
        tenant.refresh_from_db()
        self.assertEqual(tenant.pii_type_counters, {})


class DeletionLeavesCountersIntactTests(TestCase):
    """Task (d): bulk-delete and junk_sweep drop bindings but never the counter."""

    def test_bulk_delete_view_preserves_counters(self):
        tenant = _make_tenant(
            chat_id=41030,
            entity_map={"[PERSON_1]": {"name": "Bob"}, "[PERSON_2]": {"name": "Alice"}},
            counters={"PERSON": 2},
        )
        client = APIClient()
        client.force_authenticate(user=tenant.user)
        resp = client.post(
            _BULK_URL,
            {"placeholders": ["[PERSON_1]", "[PERSON_2]"], "deny": False},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        tenant.refresh_from_db()
        self.assertEqual(tenant.pii_entity_map, {})  # both deleted
        self.assertEqual(tenant.pii_type_counters, {"PERSON": 2})  # counter intact

    def test_bulk_delete_then_mint_does_not_recycle(self):
        tenant = _make_tenant(
            chat_id=41031,
            entity_map={"[PERSON_1]": {"name": "Bob"}},
            counters={"PERSON": 1},
        )
        client = APIClient()
        client.force_authenticate(user=tenant.user)
        client.post(_BULK_URL, {"placeholders": ["[PERSON_1]"]}, format="json")

        tenant.refresh_from_db()
        with patch("apps.pii.redactor._detect_pii", return_value=_person(0, 5)):
            out = redact_user_message("Alice said hi", tenant)
        self.assertIn("[PERSON_2]", out)
        self.assertNotIn("[PERSON_1]", out)

    def test_junk_sweep_preserves_counters(self):
        # "django" as CREDIT_CARD is deterministic junk (no Luhn) → deleted; the
        # PERSON keeper and BOTH counters must survive the sweep's .update().
        tenant = _make_tenant(
            chat_id=41032,
            entity_map={
                "[CREDIT_CARD_6]": {"name": "django"},
                "[PERSON_1]": {"name": "Sarah Chen"},
            },
            counters={"CREDIT_CARD": 6, "PERSON": 1},
        )
        summary = sweep_tenant(tenant)
        self.assertEqual(summary["deleted"], 1)
        tenant.refresh_from_db()
        self.assertNotIn("[CREDIT_CARD_6]", tenant.pii_entity_map)  # junk dropped
        self.assertIn("[PERSON_1]", tenant.pii_entity_map)  # keeper stays
        self.assertEqual(tenant.pii_type_counters, {"CREDIT_CARD": 6, "PERSON": 1})


class LegacyEmptyCountersTests(TestCase):
    """Task (e): a pre-migration tenant (counters {}) still mints from map max."""

    def test_empty_counters_fall_back_to_map_maxima(self):
        tenant = _make_tenant(
            chat_id=41040,
            entity_map={"[PERSON_3]": {"name": "Old Contact"}},
            counters={},  # never seeded
        )
        with patch("apps.pii.redactor._detect_pii", return_value=_person(0, 3)):
            out = redact_user_message("Bob is new", tenant)
        # Map max was 3 → next mint is [PERSON_4], and the high-water is recorded.
        self.assertIn("[PERSON_4]", out)
        tenant.refresh_from_db()
        self.assertEqual(tenant.pii_type_counters, {"PERSON": 4})
        self.assertIn("[PERSON_4]", tenant.pii_entity_map)
