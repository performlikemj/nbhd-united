"""Adversarial-audit cluster A25 regression tests.

FA-0914 — build_conversation_digest rehydrates PII placeholders before
returning so the USER.md envelope is in real-value space.

Background: the audit fix (commit c1124d28) redacted user_text_excerpt at the
poller before enqueue, which meant ConversationTurn.user_text is stored with
PII placeholders (e.g. "[PERSON_1] emailed me at [EMAIL_ADDRESS_1]").
build_conversation_digest rendered those placeholders verbatim into the USER.md
envelope, while the reply side (clean_reply_for_capture) was already
rehydrated. The fix adds a rehydrate_text call on the assembled digest string
before returning, mirroring what ChatContextView already does at request time.
"""

from __future__ import annotations

import secrets
from datetime import date
from unittest.mock import patch

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


class DigestRehydratesPlaceholdersTest(TestCase):
    """Telegram ConversationTurn with placeholder user_text renders real name."""

    def test_placeholder_user_text_rehydrated_in_digest(self):
        """FA-0914: digest renders real name, not [PERSON_1], in user lines."""
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

        self.assertIn("Alice Smith", digest, "Real name should appear in digest after rehydration")
        self.assertNotIn("[PERSON_1]", digest, "Placeholder should not appear in digest — must be rehydrated")

    def test_digest_without_entity_map_passes_through(self):
        """FA-0914: tenants without an entity map still get a valid digest."""
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

    def test_digest_rehydrate_error_falls_back_to_placeholder(self):
        """FA-0914: a rehydrate_text exception must not raise — digest is returned as-is."""
        from apps.router.conversation_capture import build_conversation_digest
        from apps.router.models import ConversationTurn

        entity_map = {"PERSON_1": "Bob"}
        tenant = _make_tenant(entity_map=entity_map)

        ConversationTurn.objects.create(
            tenant=tenant,
            channel="telegram",
            channel_user_id="77777",
            local_date=date.today(),
            user_text="[PERSON_1] asked me something",
            reply_text="Sure thing.",
        )

        with patch("apps.pii.redactor.rehydrate_text", side_effect=RuntimeError("boom")):
            # Must not raise; fall-open returns the un-rehydrated digest.
            digest = build_conversation_digest(tenant)

        self.assertIsInstance(digest, str)
        self.assertGreater(len(digest), 0, "Digest should not be empty on rehydrate error")
