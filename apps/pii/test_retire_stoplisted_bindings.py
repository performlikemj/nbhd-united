"""Coverage for the fleet never-a-name predicate and its retire backfill.

The predicate tests double as the anti-drift pin: ``is_never_a_name`` is the one
rule shared by the detection filter (what stops minting) and this command (what
gets retired), so a name-shaped word slipping into either stoplist fails here.
"""

from __future__ import annotations

import secrets
from io import StringIO

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from apps.pii.management.commands.retire_stoplisted_bindings import _retire_stoplisted
from apps.pii.redactor import is_never_a_name
from apps.tenants.models import Tenant, User


class IsNeverANameTests(SimpleTestCase):
    def test_matches_fully_stoplisted_spans(self):
        for text in [
            "calendar",
            "Calendar",
            "CALENDAR",
            "quick wins",
            "Quick Wins",
            "morning briefing",
            "heartbeat check-in",  # hyphen tokenizes to ['heartbeat','check','in']
            "google calendar",
            "calendar status",
            "\U0001f3c6 wins",  # emoji prefix is a token separator
            "quick wins\n-",  # markdown fragment, all alphabetic tokens stoplisted
            "japanese",
            "American",
            "hmm",
            "gyoza",
            "nbhd",
        ]:
            self.assertTrue(is_never_a_name(text), f"{text!r} should be never-a-name")

    def test_does_not_match_anything_carrying_a_real_name(self):
        for text in [
            "Marcus Delgado",
            "Quick Delgado",  # one non-stoplisted token is enough
            "Japanese Yamamoto",
            "quick wins\n- reply",  # 'reply' is not stoplisted
            "Sarah",
            "",
            "   ",
        ]:
            self.assertFalse(is_never_a_name(text), f"{text!r} must not be never-a-name")

    def test_name_collision_words_are_never_retired(self):
        # 'max'/'mark'/'sat' live in the POSITIONAL stoplist — suppressed only
        # when they open a sentence, never eligible for retirement. 'theo'/'la'/
        # 'moon'/'claude' were deliberately kept off both stoplists.
        for text in ["max", "Mark", "sat", "theo", "la", "moon", "claude"]:
            self.assertFalse(is_never_a_name(text), f"{text!r} must stay eligible as a name")

    def test_name_shaped_abbreviations_are_exempt_from_retirement(self):
        # These ARE stoplisted for detection (month/weekday abbreviations, an ops
        # noun) but are real names too — retiring their bindings would silently
        # strip protection a user created by hand.
        from apps.pii.redactor import _is_common_word_span

        for text in ["mar", "Jan", "jun", "Sun", "can"]:
            self.assertTrue(
                _is_common_word_span(text.lower(), True),
                f"{text!r} should still be dropped at detection",
            )
            self.assertFalse(is_never_a_name(text), f"{text!r} must never be retired")

    def test_cjk_names_are_exempt(self):
        # No a-z tokens at all → False, so a Japanese/Chinese name can never be
        # swept by a Latin vocabulary rule.
        for text in ["田中太郎", "マイケル", "佐々木"]:
            self.assertFalse(is_never_a_name(text))


class RetireStoplistedMapTests(SimpleTestCase):
    NOW = "2026-08-08T01:02:03+00:00"

    def test_retires_only_person_and_location_matches(self):
        entity_map = {
            "[PERSON_1]": "Calendar",
            "[PERSON_2]": {"name": "quick wins", "updated_at": "2026-01-01T00:00:00+00:00"},
            "[LOCATION_3]": {"name": "Japanese"},
            "[PERSON_4]": {"name": "Marcus Delgado"},
            "[PERSON_5]": {"name": "Quick Delgado"},
            # Same junk word under a structured kind — out of scope by design.
            "[EMAIL_ADDRESS_6]": {"name": "calendar"},
        }

        updated, by_key = _retire_stoplisted(entity_map, now_iso=self.NOW)

        self.assertEqual(by_key, {"calendar": 1, "japanese": 1, "quick wins": 1})
        for placeholder in ("[PERSON_1]", "[PERSON_2]", "[LOCATION_3]"):
            self.assertTrue(updated[placeholder]["retired"], placeholder)
            self.assertEqual(updated[placeholder]["retired_at"], self.NOW)
        self.assertEqual(updated["[PERSON_2]"]["updated_at"], "2026-01-01T00:00:00+00:00")
        self.assertEqual(updated["[PERSON_4]"], {"name": "Marcus Delgado"})
        self.assertEqual(updated["[PERSON_5]"], {"name": "Quick Delgado"})
        self.assertEqual(updated["[EMAIL_ADDRESS_6]"], {"name": "calendar"})
        # Input map untouched (retire-in-place returns a copy).
        self.assertEqual(entity_map["[PERSON_1]"], "Calendar")

    def test_already_retired_bindings_are_skipped(self):
        entity_map = {
            "[PERSON_1]": {"name": "calendar", "retired": True, "retired_at": "earlier"},
        }

        updated, by_key = _retire_stoplisted(entity_map, now_iso=self.NOW)

        self.assertEqual(by_key, {})
        self.assertEqual(updated["[PERSON_1]"]["retired_at"], "earlier")

    def test_user_curated_bindings_are_left_alone(self):
        # The console screens a manual add with is_junk_span only, so a user can
        # hold a stoplist-word binding on purpose. relationship / notes /
        # reviewed_at are fields only a human writes — never sweep them.
        entity_map = {
            "[PERSON_1]": {"name": "Quick", "relationship": "neighbour"},
            "[PERSON_2]": {"name": "Calendar", "notes": "the band"},
            "[PERSON_3]": {"name": "Gmail", "reviewed_at": "2026-08-01T00:00:00+00:00"},
            "[PERSON_4]": {"name": "Calendar"},
        }

        updated, by_key = _retire_stoplisted(entity_map, now_iso=self.NOW)

        # Only the uncurated duplicate retires; the curated same-name binding
        # survives even though its canonical key matched.
        self.assertEqual(by_key, {"calendar": 1})
        self.assertTrue(updated["[PERSON_4]"]["retired"])
        for placeholder in ("[PERSON_1]", "[PERSON_2]", "[PERSON_3]"):
            self.assertNotIn("retired", updated[placeholder], placeholder)

    def test_report_label_flattens_markdown_fragment_keys(self):
        entity_map = {"[PERSON_1]": {"name": "quick wins\n-"}}

        _, by_key = _retire_stoplisted(entity_map, now_iso=self.NOW)

        self.assertEqual(by_key, {"quick wins -": 1})


def _make_tenant(*, entity_map: dict) -> Tenant:
    user = User.objects.create_user(
        username=f"u_{secrets.token_hex(4)}",
        email=f"{secrets.token_hex(4)}@example.com",
        password="hunter2-test",
    )
    return Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE,
        container_fqdn="container.example.com",
        pii_entity_map=entity_map,
    )


class RetireStoplistedBindingsCommandTests(TestCase):
    def test_dry_run_reports_counts_and_words_and_writes_nothing(self):
        tenant = _make_tenant(
            entity_map={
                "[PERSON_1]": "Calendar",
                "[PERSON_2]": {"name": " calendar "},
                "[PERSON_3]": {"name": "Quick Wins"},
                "[PERSON_4]": {"name": "Marcus Delgado"},
            }
        )
        before = tenant.pii_entity_map
        stdout = StringIO()

        call_command("retire_stoplisted_bindings", str(tenant.id), stdout=stdout)

        tenant.refresh_from_db()
        self.assertEqual(tenant.pii_entity_map, before)
        output = stdout.getvalue()
        self.assertIn(f"tenant={tenant.id} would_retire=3", output)
        self.assertIn("by_word=calendar=2,quick wins=1", output)
        self.assertIn("[DRY-RUN] tenants_scanned=1 tenants_with_matches=1 bindings_would_retire=3", output)
        self.assertNotIn("Delgado", output)

    def test_commit_persists_retirements_and_is_idempotent(self):
        first = _make_tenant(
            entity_map={
                "[PERSON_1]": "Calendar",
                "[LOCATION_2]": {"name": "Gmail"},
                "[PERSON_3]": {"name": "Marcus Delgado"},
            }
        )
        second = _make_tenant(entity_map={"[PERSON_9]": {"name": "Hmm"}})
        stdout = StringIO()

        call_command("retire_stoplisted_bindings", "--all", "--commit", stdout=stdout)

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertTrue(first.pii_entity_map["[PERSON_1]"]["retired"])
        self.assertTrue(first.pii_entity_map["[LOCATION_2]"]["retired"])
        self.assertEqual(first.pii_entity_map["[PERSON_3]"], {"name": "Marcus Delgado"})
        self.assertTrue(second.pii_entity_map["[PERSON_9]"]["retired"])
        self.assertIn("bindings_retired=3", stdout.getvalue())

        rerun = StringIO()
        call_command("retire_stoplisted_bindings", "--all", "--commit", stdout=rerun)
        self.assertIn("tenants_with_matches=0", rerun.getvalue())
        self.assertIn("bindings_retired=0", rerun.getvalue())

    def test_requires_exactly_one_target(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command("retire_stoplisted_bindings", stdout=StringIO())
        with self.assertRaises(CommandError):
            call_command("retire_stoplisted_bindings", "--all", str(_make_tenant(entity_map={}).id))
