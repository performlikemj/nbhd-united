"""Tests for the PII arbiter cron task (issue #660 Phase 2 backend)."""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from apps.pii.arbiter import (
    ARBITER_BATCH_SIZE,
    _entries_to_judge,
    pii_arbiter_task,
)
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


def _make_tenant(*, chat_id: int = 11111, entity_map=None, denylist=None) -> Tenant:
    tenant = create_tenant(display_name="Test User", telegram_chat_id=chat_id)
    Tenant.objects.filter(pk=tenant.pk).update(
        pii_entity_map=entity_map or {},
        pii_denylist=denylist or {},
    )
    tenant.refresh_from_db()
    return tenant


class EntriesToJudgeTests(TestCase):
    def test_picks_person_and_location_entries(self):
        tenant = _make_tenant(
            chat_id=10001,
            entity_map={
                "[PERSON_1]": {"name": "Sarah Chen"},
                "[LOCATION_1]": {"name": "Shibuya"},
            },
        )

        items = _entries_to_judge(tenant)

        keys = {item["key"] for item in items}
        self.assertEqual(keys, {"sarah chen", "shibuya"})

    def test_skips_email_and_phone_placeholders(self):
        tenant = _make_tenant(
            chat_id=10002,
            entity_map={
                "[PERSON_1]": {"name": "Sarah"},
                "[EMAIL_ADDRESS_1]": {"name": "sarah@example.com"},
                "[PHONE_NUMBER_1]": {"name": "555-1234"},
                "[CREDIT_CARD_1]": {"name": "4111111111111111"},
            },
        )

        items = _entries_to_judge(tenant)

        keys = {item["key"] for item in items}
        self.assertEqual(keys, {"sarah"})

    def test_skips_entries_already_judged(self):
        tenant = _make_tenant(
            chat_id=10003,
            entity_map={
                "[PERSON_1]": {"name": "Sarah", "arbiter_judged_at": "2026-05-26T00:00:00+00:00"},
                "[PERSON_2]": {"name": "Manny"},
            },
        )

        items = _entries_to_judge(tenant)

        self.assertEqual([item["key"] for item in items], ["manny"])

    def test_skips_entries_on_denylist(self):
        tenant = _make_tenant(
            chat_id=10004,
            entity_map={
                "[PERSON_1]": {"name": "goal"},
                "[PERSON_2]": {"name": "Patrick"},
            },
            denylist={"goal": {"reason": "manual"}},
        )

        items = _entries_to_judge(tenant)

        self.assertEqual([item["key"] for item in items], ["patrick"])

    def test_accepts_legacy_string_entries(self):
        tenant = _make_tenant(
            chat_id=10005,
            entity_map={"[PERSON_1]": "Sarah", "[PERSON_2]": "Patrick"},
        )

        items = _entries_to_judge(tenant)

        self.assertEqual({item["key"] for item in items}, {"sarah", "patrick"})

    def test_dedupes_by_canonical_key(self):
        # Legacy bloat — same casefolded key under multiple placeholders.
        # The arbiter judges once; _apply_decisions stamps all placeholders.
        tenant = _make_tenant(
            chat_id=10006,
            entity_map={
                "[PERSON_1]": {"name": "Sautai"},
                "[PERSON_42]": {"name": "sautai"},
                "[PERSON_408]": {"name": " Sautai "},
            },
        )

        items = _entries_to_judge(tenant)

        self.assertEqual([item["key"] for item in items], ["sautai"])

    def test_skips_empty_names(self):
        tenant = _make_tenant(
            chat_id=10007,
            entity_map={"[PERSON_1]": {"name": ""}, "[PERSON_2]": {"name": "Sarah"}},
        )

        items = _entries_to_judge(tenant)

        self.assertEqual([item["key"] for item in items], ["sarah"])


class _FakeLLMResponse:
    """Mimic the requests.Response interface used by the arbiter."""

    def __init__(self, *, decisions: list[dict] | None = None, raw_content: str | None = None, status: int = 200):
        import json as _json

        if raw_content is None:
            raw_content = _json.dumps({"decisions": decisions or []})
        self._payload = {
            "choices": [{"message": {"content": raw_content}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30},
        }
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class ArbiterTaskTests(TestCase):
    def setUp(self):
        # `record_usage` writes a MonthlyBudget row; the patch isolates the
        # arbiter logic from billing side effects and lets us assert how
        # many times we'd have billed.
        self._usage_patcher = patch("apps.billing.services.record_usage", return_value=None)
        self.record_usage_mock = self._usage_patcher.start()
        # OPENROUTER_API_KEY isn't set in tests by default — the arbiter
        # short-circuits to {} without it. Patch it so _call_arbiter_llm
        # reaches the requests.post call (which we also patch).
        self._settings_patcher = patch("apps.pii.arbiter.settings")
        settings_mock = self._settings_patcher.start()
        settings_mock.OPENROUTER_API_KEY = "test-key"

    def tearDown(self):
        self._usage_patcher.stop()
        self._settings_patcher.stop()

    def test_denies_false_positives_and_stamps_confirmed(self):
        tenant = _make_tenant(
            chat_id=20001,
            entity_map={
                "[PERSON_1]": {"name": "Sarah Chen"},
                "[PERSON_2]": {"name": "goal"},
                "[PERSON_3]": {"name": "calendar"},
            },
        )

        fake = _FakeLLMResponse(
            decisions=[
                {"key": "sarah chen", "is_pii": True},
                {"key": "goal", "is_pii": False},
                {"key": "calendar", "is_pii": False},
            ]
        )

        with patch("apps.pii.arbiter.requests.post", return_value=fake) as post_mock:
            result = pii_arbiter_task()

        self.assertEqual(post_mock.call_count, 1)
        self.assertEqual(result["entries_judged"], 3)
        self.assertEqual(result["entries_denied"], 2)
        self.assertEqual(result["entries_confirmed"], 1)

        tenant.refresh_from_db()
        self.assertIn("goal", tenant.pii_denylist)
        self.assertIn("calendar", tenant.pii_denylist)
        self.assertNotIn("sarah chen", tenant.pii_denylist)
        self.assertEqual(tenant.pii_denylist["goal"]["reason"], "arbiter")

        for placeholder in ("[PERSON_1]", "[PERSON_2]", "[PERSON_3]"):
            entry = tenant.pii_entity_map[placeholder]
            self.assertIn("arbiter_judged_at", entry, f"missing stamp on {placeholder}")

    def test_idempotent_when_rerun(self):
        tenant = _make_tenant(
            chat_id=20002,
            entity_map={"[PERSON_1]": {"name": "Sarah Chen"}},
        )

        fake = _FakeLLMResponse(decisions=[{"key": "sarah chen", "is_pii": True}])
        with patch("apps.pii.arbiter.requests.post", return_value=fake) as post_mock:
            first = pii_arbiter_task()
            second = pii_arbiter_task()

        self.assertEqual(post_mock.call_count, 1)
        self.assertEqual(first["entries_judged"], 1)
        self.assertEqual(second["entries_judged"], 0)
        self.assertEqual(second["tenants_with_work"], 0)

    def test_malformed_llm_response_defers(self):
        tenant = _make_tenant(
            chat_id=20003,
            entity_map={"[PERSON_1]": {"name": "Sarah"}},
        )

        fake = _FakeLLMResponse(raw_content="not json at all")
        with patch("apps.pii.arbiter.requests.post", return_value=fake):
            result = pii_arbiter_task()

        self.assertEqual(result["entries_judged"], 0)
        tenant.refresh_from_db()
        self.assertEqual(tenant.pii_denylist, {})
        self.assertNotIn("arbiter_judged_at", tenant.pii_entity_map["[PERSON_1]"])

    def test_http_error_defers(self):
        _make_tenant(
            chat_id=20004,
            entity_map={"[PERSON_1]": {"name": "Sarah"}},
        )

        fake = _FakeLLMResponse(decisions=[], status=500)
        with patch("apps.pii.arbiter.requests.post", return_value=fake):
            result = pii_arbiter_task()

        self.assertEqual(result["entries_judged"], 0)

    def test_legacy_duplicates_share_one_denylist_entry(self):
        # 3 placeholders → same canonical key "sautai" → one LLM call →
        # all 3 entity entries stamped with arbiter_judged_at and one
        # denylist entry written.
        tenant = _make_tenant(
            chat_id=20005,
            entity_map={
                "[PERSON_1]": {"name": "Sautai"},
                "[PERSON_42]": {"name": "sautai"},
                "[PERSON_408]": {"name": "SAUTAI"},
            },
        )

        fake = _FakeLLMResponse(decisions=[{"key": "sautai", "is_pii": False}])
        with patch("apps.pii.arbiter.requests.post", return_value=fake) as post_mock:
            result = pii_arbiter_task()

        # One LLM call covered all three duplicates.
        self.assertEqual(post_mock.call_count, 1)
        self.assertEqual(result["entries_denied"], 1)

        tenant.refresh_from_db()
        self.assertEqual(list(tenant.pii_denylist.keys()), ["sautai"])
        for placeholder in ("[PERSON_1]", "[PERSON_42]", "[PERSON_408]"):
            self.assertIn("arbiter_judged_at", tenant.pii_entity_map[placeholder])

    def test_batches_split_at_size_limit(self):
        entity_map = {f"[PERSON_{i}]": {"name": f"Person{i}"} for i in range(1, ARBITER_BATCH_SIZE + 5)}
        _make_tenant(chat_id=20006, entity_map=entity_map)

        # Both calls return only "person1" as a decision so we can verify
        # multi-batch flow without depending on which item lands where.
        fake = _FakeLLMResponse(decisions=[{"key": "person1", "is_pii": True}])
        with patch("apps.pii.arbiter.requests.post", return_value=fake) as post_mock:
            pii_arbiter_task()

        self.assertEqual(post_mock.call_count, 2)
