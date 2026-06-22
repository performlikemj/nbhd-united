"""Adversarial-audit regression tests (cluster A09).

Covers the unsynchronized read-modify-write race on ``Tenant.pii_entity_map``
/ ``Tenant.pii_denylist``. Every writer now re-reads the row under a
``select_for_update`` lock and re-derives placeholder counters from that
locked snapshot, so two concurrent updates cannot:

  (1) mint the same ``[TYPE_N]`` placeholder for two different entities, and
  (2) silently clobber a key (delete/denylist stamp) written since the read.

The tests simulate "a concurrent write landed between the in-memory read and
the locked re-read" by seeding the in-memory tenant object with a STALE map
while the DB row already holds the committed concurrent state.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from apps.pii.redactor import DetectedEntity, redact_user_message
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


def _make_tenant(*, chat_id: int, entity_map=None, denylist=None) -> Tenant:
    tenant = create_tenant(display_name="Test User", telegram_chat_id=chat_id)
    Tenant.objects.filter(pk=tenant.pk).update(
        pii_entity_map=entity_map or {},
        pii_denylist=denylist or {},
    )
    tenant.refresh_from_db()
    return tenant


class RedactorLockedMintTests(TestCase):
    """The mint path derives its counter from the row-locked DB snapshot."""

    def test_concurrent_person_does_not_clobber_existing_placeholder(self):
        # DB row already has [PERSON_1] = "Alice" (a concurrent redaction
        # committed it). The in-memory tenant object still carries the STALE
        # empty map this request started with.
        tenant = _make_tenant(
            chat_id=21001,
            entity_map={"[PERSON_1]": {"name": "Alice"}},
        )
        tenant.pii_entity_map = {}  # stale snapshot, as the request saw it

        # NER detects a brand-new PERSON "Bob".
        text = "Bob said hi"
        detected = [DetectedEntity(entity_type="PERSON", start=0, end=3, score=0.99)]

        with patch("apps.pii.redactor._detect_pii", return_value=detected):
            out = redact_user_message(text, tenant)

        tenant.refresh_from_db()
        db_map = tenant.pii_entity_map

        # [PERSON_1]=Alice must survive untouched (no clobber).
        self.assertEqual(db_map["[PERSON_1]"]["name"], "Alice")
        # Bob must be minted as [PERSON_2], NOT [PERSON_1].
        self.assertIn("[PERSON_2]", db_map)
        self.assertEqual(db_map["[PERSON_2]"]["name"], "Bob")
        # The redacted text must carry the SAME placeholder we persisted, so
        # outbound rehydration maps Bob's placeholder back to Bob — not Alice.
        self.assertIn("[PERSON_2]", out)
        self.assertNotIn("Bob", out)

    def test_existing_entity_reused_from_locked_snapshot(self):
        # DB already knows "Alice" as [PERSON_1] (committed concurrently); the
        # in-memory map is stale/empty. A new message mentioning Alice must
        # reuse [PERSON_1], not mint a duplicate.
        tenant = _make_tenant(
            chat_id=21002,
            entity_map={"[PERSON_1]": {"name": "Alice"}},
        )
        tenant.pii_entity_map = {}

        text = "Alice again"
        detected = [DetectedEntity(entity_type="PERSON", start=0, end=5, score=0.99)]

        with patch("apps.pii.redactor._detect_pii", return_value=detected):
            out = redact_user_message(text, tenant)

        tenant.refresh_from_db()
        db_map = tenant.pii_entity_map
        # No duplicate [PERSON_2] minted for the same entity.
        self.assertNotIn("[PERSON_2]", db_map)
        self.assertEqual(db_map["[PERSON_1]"]["name"], "Alice")
        self.assertIn("[PERSON_1]", out)

    def test_two_new_entities_same_message_get_distinct_placeholders(self):
        tenant = _make_tenant(chat_id=21003, entity_map={})

        text = "Carol met Dave"
        detected = [
            DetectedEntity(entity_type="PERSON", start=0, end=5, score=0.99),  # Carol
            DetectedEntity(entity_type="PERSON", start=10, end=14, score=0.99),  # Dave
        ]

        with patch("apps.pii.redactor._detect_pii", return_value=detected):
            out = redact_user_message(text, tenant)

        tenant.refresh_from_db()
        db_map = tenant.pii_entity_map
        names = {v["name"] for v in db_map.values()}
        self.assertEqual(names, {"Carol", "Dave"})
        self.assertEqual(len(db_map), 2)
        self.assertIn("[PERSON_1]", out)
        self.assertIn("[PERSON_2]", out)


class RedactorPersistTests(TestCase):
    """The persisted map and the returned text stay in sync."""

    def test_no_results_returns_text_unchanged_and_no_write(self):
        tenant = _make_tenant(chat_id=21010, entity_map={"[PERSON_1]": {"name": "Alice"}})
        with patch("apps.pii.redactor._detect_pii", return_value=[]):
            out = redact_user_message("nothing to redact", tenant)
        self.assertEqual(out, "nothing to redact")
        tenant.refresh_from_db()
        self.assertEqual(tenant.pii_entity_map, {"[PERSON_1]": {"name": "Alice"}})


class ArbiterLockedReadTests(TestCase):
    """The arbiter re-reads the row under a lock before stamping."""

    def test_apply_decisions_uses_db_snapshot_not_stale_object(self):
        from apps.pii.arbiter import _apply_decisions_for_tenant

        # DB has the live map; the passed-in tenant object is STALE (missing a
        # placeholder added concurrently). The arbiter must operate on the DB
        # snapshot and not drop the concurrently-added [PERSON_2].
        tenant = _make_tenant(
            chat_id=21020,
            entity_map={
                "[PERSON_1]": {"name": "Sarah Chen"},
                "[PERSON_2]": {"name": "Bob Lee"},
            },
        )
        # Make the in-memory object stale: it only knows [PERSON_1].
        tenant.pii_entity_map = {"[PERSON_1]": {"name": "Sarah Chen"}}

        batch = [{"key": "sarah chen", "placeholder": "[PERSON_1]"}]
        decisions = {"sarah chen": False}  # denied -> denylist + stamp

        denied, confirmed = _apply_decisions_for_tenant(tenant, batch, decisions, "2026-06-22T00:00:00+00:00")

        self.assertEqual((denied, confirmed), (1, 0))
        tenant.refresh_from_db()
        db_map = tenant.pii_entity_map
        # The concurrently-added [PERSON_2] must NOT have been clobbered.
        self.assertIn("[PERSON_2]", db_map)
        self.assertEqual(db_map["[PERSON_2]"]["name"], "Bob Lee")
        # Sarah got stamped as judged.
        self.assertEqual(db_map["[PERSON_1]"].get("arbiter_judged_at"), "2026-06-22T00:00:00+00:00")
        # Denylist picked up the canonical key.
        self.assertIn("sarah chen", tenant.pii_denylist)

    def test_apply_decisions_missing_tenant_is_noop(self):
        from apps.pii.arbiter import _apply_decisions_for_tenant

        tenant = _make_tenant(chat_id=21021, entity_map={"[PERSON_1]": {"name": "Sarah"}})
        Tenant.objects.filter(pk=tenant.pk).delete()

        denied, confirmed = _apply_decisions_for_tenant(
            tenant,
            [{"key": "sarah", "placeholder": "[PERSON_1]"}],
            {"sarah": False},
            "2026-06-22T00:00:00+00:00",
        )
        self.assertEqual((denied, confirmed), (0, 0))
