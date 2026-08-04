"""Mint-gating regression tests (PII self-cleaning).

The prod audit found 979/1103 bindings were junk, and none of it came from human
chat: it was MACHINE text. Agent-authored workspace markdown minted structural
fragments (table separators, headings, timestamps NER labels PERSON/ACCOUNT), and
raw tool payloads minted newsletter senders / invisible-char runs / unvalidated
financial labels. So minting is now gated by WHERE the text came from:

  * agent markdown (memory sync, co-pilot)  -> mint='never'      (replace-known-only)
  * tool responses (email bodies, tool JSON) -> mint='validated' (only validated types)
  * human chat ingress                       -> mint='all'       (unchanged)

Under every policy, entities the tenant map ALREADY knows are still replaced, so
privacy for known people is preserved. These tests stub ``_detect_pii`` (no ONNX
model needed) and patch ``_structured_validator`` so the validated path is
deterministic and does not depend on ``apps.pii.hygiene`` having landed yet.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from apps.pii.redactor import (
    DetectedEntity,
    RedactionSession,
    redact_tool_response,
    redact_user_message,
)
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


def _fake_validate(entity_type: str, text: str) -> bool:
    """Stand-in for ``hygiene.validate_structured``: structurally-checkable types
    pass, neural contextual labels never do. Mirrors the real contract closely
    enough that the mint gate behaves as production will (email-shaped and
    checksummed spans mint; PERSON/LOCATION do not)."""
    return entity_type in ("EMAIL_ADDRESS", "CREDIT_CARD", "IBAN_CODE", "PHONE_NUMBER")


def _make_tenant(*, chat_id: int, entity_map=None, denylist=None) -> Tenant:
    tenant = create_tenant(display_name="Test User", telegram_chat_id=chat_id)
    Tenant.objects.filter(pk=tenant.pk).update(
        pii_entity_map=entity_map or {},
        pii_denylist=denylist or {},
    )
    tenant.refresh_from_db()
    return tenant


class MemorySyncMintNeverTests(TestCase):
    """RedactionSession(mint='never') — the memory-sync + co-pilot policy."""

    def test_junk_never_minted_but_known_name_still_replaced(self):
        # Agent markdown carrying a KNOWN contact plus structure NER would junk.
        tenant = _make_tenant(chat_id=31001, entity_map={"[PERSON_1]": {"name": "Sautai"}})

        session = RedactionSession(tenant=tenant, mint="never")
        md = "| Task | Owner |\n|------|-------|\n### 08:05 — Sautai review\n- **06:02** standup"

        # Even if the detector WOULD fire (junk ACCOUNT on the table row), the
        # never-policy must not consult it or mint anything.
        junk = [DetectedEntity("ACCOUNT", 0, 6, 0.99)]
        with patch("apps.pii.redactor._detect_pii", return_value=junk) as mock_detect:
            out = session.redact(md)

        # Known contact stays masked.
        self.assertIn("[PERSON_1]", out)
        self.assertNotIn("Sautai", out)
        # Nothing minted; the detector was never even run.
        self.assertEqual(session.entity_map, {})
        mock_detect.assert_not_called()
        # Machine structure survives verbatim.
        self.assertIn("|------|-------|", out)
        self.assertIn("### 08:05", out)

    def test_never_policy_makes_no_db_write(self):
        # No mints -> memory_sync's `if session.entity_map:` guard skips the DB
        # write entirely, so the tenant map is untouched.
        tenant = _make_tenant(chat_id=31002, entity_map={"[PERSON_1]": {"name": "Sautai"}})
        session = RedactionSession(tenant=tenant, mint="never")
        session.redact("Quick Wins\n- ship it\n|----|----|\nSautai owns the standup")
        self.assertEqual(session.entity_map, {})
        tenant.refresh_from_db()
        self.assertEqual(tenant.pii_entity_map, {"[PERSON_1]": {"name": "Sautai"}})

    def test_default_session_still_mints_new_entities(self):
        # Regression guard: the DEFAULT policy ('all') preserves the historical
        # mint-everything behavior so non-opted-in callers are unaffected.
        tenant = _make_tenant(chat_id=31003, entity_map={})
        session = RedactionSession(tenant=tenant)  # default mint='all'
        detected = [DetectedEntity("PERSON", 0, 3, 0.99)]  # "Bob"
        with patch("apps.pii.redactor._detect_pii", return_value=detected):
            out = session.redact("Bob said hi")
        self.assertIn("[PERSON_1]", out)
        self.assertEqual(session.entity_map, {"[PERSON_1]": "Bob"})


class ToolResponseMintValidatedTests(TestCase):
    """redact_tool_response — the mint='validated' machine-text policy."""

    def test_unvalidated_person_hit_is_not_minted(self):
        # A neural PERSON span in an email body (newsletter sender etc.) must NOT
        # coin a new binding — it is left verbatim.
        tenant = _make_tenant(chat_id=31010, entity_map={})
        data = {"snippet": "Bob said hi"}
        detected = [DetectedEntity("PERSON", 0, 3, 0.99)]
        with (
            patch("apps.pii.redactor._detect_pii", return_value=detected),
            patch("apps.pii.redactor._structured_validator", return_value=_fake_validate),
        ):
            out = redact_tool_response(data, tenant)

        tenant.refresh_from_db()
        self.assertEqual(tenant.pii_entity_map, {})  # nothing minted
        self.assertEqual(out["snippet"], "Bob said hi")  # left raw

    def test_email_shaped_hit_is_minted(self):
        # Real correspondent emails still mint so inbound addresses stay protected.
        tenant = _make_tenant(chat_id=31011, entity_map={})
        data = {"snippet": "Email bob@example.com now"}
        # "bob@example.com" spans [6, 21).
        detected = [DetectedEntity("EMAIL_ADDRESS", 6, 21, 0.99)]
        with (
            patch("apps.pii.redactor._detect_pii", return_value=detected),
            patch("apps.pii.redactor._structured_validator", return_value=_fake_validate),
        ):
            out = redact_tool_response(data, tenant)

        tenant.refresh_from_db()
        self.assertIn("[EMAIL_ADDRESS_1]", tenant.pii_entity_map)
        self.assertEqual(tenant.pii_entity_map["[EMAIL_ADDRESS_1]"]["name"], "bob@example.com")
        self.assertIn("[EMAIL_ADDRESS_1]", out["snippet"])
        self.assertNotIn("bob@example.com", out["snippet"])

    def test_known_name_replaced_without_new_mint(self):
        # A contact the tenant already knows is replaced (Step 1 literal pass);
        # no new binding is coined.
        tenant = _make_tenant(chat_id=31012, entity_map={"[PERSON_1]": {"name": "Alice"}})
        data = {"snippet": "Alice emailed the report"}
        with (
            patch("apps.pii.redactor._detect_pii", return_value=[]),
            patch("apps.pii.redactor._structured_validator", return_value=_fake_validate),
        ):
            out = redact_tool_response(data, tenant)

        tenant.refresh_from_db()
        self.assertEqual(set(tenant.pii_entity_map), {"[PERSON_1]"})  # no new mint
        self.assertIn("[PERSON_1|unresolved]", out["snippet"])
        self.assertNotIn("Alice", out["snippet"])

    def test_without_hygiene_email_floor_mints(self):
        # Until apps.pii.hygiene lands the validator loader returns None and the
        # gate uses a conservative email-only floor, so email-shaped spans keep
        # minting and inbound-correspondent protection never regresses.
        tenant = _make_tenant(chat_id=31013, entity_map={})
        data = {"snippet": "bob@example.com"}
        detected = [DetectedEntity("EMAIL_ADDRESS", 0, 15, 0.99)]
        with (
            patch("apps.pii.redactor._detect_pii", return_value=detected),
            patch("apps.pii.redactor._structured_validator", return_value=None),
        ):
            out = redact_tool_response(data, tenant)

        tenant.refresh_from_db()
        self.assertIn("[EMAIL_ADDRESS_1]", tenant.pii_entity_map)
        self.assertEqual(out["snippet"], "[EMAIL_ADDRESS_1]")

    def test_without_hygiene_person_stays_blocked(self):
        # The junk-prone classes (neural PERSON/LOCATION) still fail closed when
        # hygiene is absent — the floor vouches ONLY for email-shaped spans.
        tenant = _make_tenant(chat_id=31014, entity_map={})
        data = {"snippet": "Bob said hi"}
        detected = [DetectedEntity("PERSON", 0, 3, 0.99)]
        with (
            patch("apps.pii.redactor._detect_pii", return_value=detected),
            patch("apps.pii.redactor._structured_validator", return_value=None),
        ):
            out = redact_tool_response(data, tenant)

        tenant.refresh_from_db()
        self.assertEqual(tenant.pii_entity_map, {})  # no mint
        self.assertEqual(out["snippet"], "Bob said hi")  # left raw


class ChatIngressMintAllTests(TestCase):
    """redact_user_message (chat ingress) keeps today's unrestricted minting."""

    def test_new_person_hit_still_mints(self):
        tenant = _make_tenant(chat_id=31020, entity_map={})
        detected = [DetectedEntity("PERSON", 0, 3, 0.99)]  # "Bob"
        with patch("apps.pii.redactor._detect_pii", return_value=detected):
            out = redact_user_message("Bob said hi", tenant)

        tenant.refresh_from_db()
        self.assertIn("[PERSON_1]", tenant.pii_entity_map)
        self.assertEqual(tenant.pii_entity_map["[PERSON_1]"]["name"], "Bob")
        self.assertIn("[PERSON_1]", out)
        self.assertNotIn("Bob", out)

    def test_chat_does_not_consult_validator(self):
        # The 'all' policy short-circuits before the validator, so chat ingress is
        # unaffected by hygiene's presence or absence.
        tenant = _make_tenant(chat_id=31021, entity_map={})
        detected = [DetectedEntity("PERSON", 0, 3, 0.99)]
        with (
            patch("apps.pii.redactor._detect_pii", return_value=detected),
            patch("apps.pii.redactor._structured_validator") as mock_validator,
        ):
            redact_user_message("Bob said hi", tenant)
        mock_validator.assert_not_called()
