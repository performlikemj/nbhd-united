import uuid

from django.test import SimpleTestCase

from .capture import TRANSCRIPTS_TURN_NS, derive_turn_id


class TurnIdTest(SimpleTestCase):
    def test_turn_id_is_stable_and_seam_scoped(self):
        tenant_id = uuid.UUID("3d53c648-247b-40ae-87e2-4bac9afceea1")

        first = derive_turn_id(tenant_id, "telegram_poller", "update-7")
        second = derive_turn_id(tenant_id, "telegram_poller", "update-7")
        other_seam = derive_turn_id(tenant_id, "line", "update-7")

        self.assertEqual(first, second)
        self.assertNotEqual(first, other_seam)
        self.assertEqual(
            first,
            uuid.uuid5(
                TRANSCRIPTS_TURN_NS,
                f"{tenant_id}:telegram_poller:update-7",
            ),
        )
