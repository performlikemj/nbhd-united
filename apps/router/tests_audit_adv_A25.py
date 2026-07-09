"""Adversarial-audit cluster A25 regression tests.

FA-0914 (superseded) — ``build_conversation_digest`` used to rehydrate PII
placeholders before returning, so the USER.md envelope reached the container in
real-value space. The encryption-at-rest directive (§5/§7, Phase 0 PR-4)
REVERSES that: the digest renders into the USER.md managed region, which the
container loads into the model prompt on every turn — a MODEL-facing seam.
Real names must NOT reach the model there, so the digest now stays
placeholder-space (both user AND reply lines). The owner-facing on-device digest
(``ChatContextView``) rehydrates the rendered snapshot separately; the ``?since=``
feed rehydrates reply lines on read. These tests pin the model-facing invariant:
placeholders stay placeholders in ``build_conversation_digest``.
"""

from __future__ import annotations

import secrets
from datetime import date

from django.test import TestCase

from apps.tenants.models import Tenant, User


def _make_tenant(entity_map: dict | None = None) -> Tenant:
    user = User.objects.create_user(
        username=f"a25_{secrets.token_hex(4)}",
        email=f"{secrets.token_hex(4)}@example.com",
        preferred_channel="telegram",
    )
    t = Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE,
        container_fqdn="oc-a25.example.com",
    )
    if entity_map is not None:
        t.pii_entity_map = entity_map
        t.save(update_fields=["pii_entity_map"])
    return t


class DigestStaysPlaceholderSpaceTest(TestCase):
    """The USER.md conversation digest is model-facing: placeholders stay."""

    def test_placeholder_user_text_stays_placeholder_in_digest(self):
        """Model-facing digest keeps [PERSON_1] in user lines — no real name leaks."""
        from apps.router.conversation_capture import build_conversation_digest
        from apps.router.models import ConversationTurn

        # pii_entity_map keys are the bracketed placeholders (see redactor.py).
        entity_map = {"[PERSON_1]": "Alice Smith"}
        tenant = _make_tenant(entity_map=entity_map)

        ConversationTurn.objects.create(
            tenant=tenant,
            channel="telegram",
            channel_user_id="99999",
            local_date=date.today(),
            user_text="[PERSON_1] emailed me about the meeting",
            reply_text="Got it — I'll remind you about that.",
        )

        digest = build_conversation_digest(tenant)

        self.assertIn("[PERSON_1]", digest, "Model-facing digest must keep the placeholder")
        self.assertNotIn("Alice Smith", digest, "Real name must NOT reach the model prompt via the digest")

    def test_placeholder_reply_text_stays_placeholder_in_digest(self):
        """The live-leak fix on the reply side: a placeholder-space reply is NOT
        rehydrated into the model-facing digest."""
        from apps.router.conversation_capture import build_conversation_digest
        from apps.router.models import ConversationTurn

        entity_map = {"[PERSON_1]": "Alice Smith"}
        tenant = _make_tenant(entity_map=entity_map)

        ConversationTurn.objects.create(
            tenant=tenant,
            channel="telegram",
            channel_user_id="99998",
            local_date=date.today(),
            user_text="did you email them?",
            reply_text="Yes — I told [PERSON_1] you'd call.",
        )

        digest = build_conversation_digest(tenant)

        self.assertIn("[PERSON_1]", digest)
        self.assertNotIn("Alice Smith", digest)

    def test_digest_without_entity_map_passes_through(self):
        """Tenants without an entity map still get a valid digest (no placeholders)."""
        from apps.router.conversation_capture import build_conversation_digest
        from apps.router.models import ConversationTurn

        tenant = _make_tenant(entity_map=None)

        ConversationTurn.objects.create(
            tenant=tenant,
            channel="telegram",
            channel_user_id="88888",
            local_date=date.today(),
            user_text="Hello from the test",
            reply_text="Hello back.",
        )

        digest = build_conversation_digest(tenant)

        self.assertIn("Hello from the test", digest)
        self.assertIn("Hello back.", digest)
