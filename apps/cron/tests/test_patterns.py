"""Pattern-handler tests: payload validation, build_oc_data shape, outbound validation.

Per pattern, exercises:
  - payload schema rejects bad input
  - build_oc_data emits an OC job dict the gateway will accept (toolsAllow
    has no mutation tools; sessionTarget pairs with payload.kind)
  - validate_outbound_message accepts conformant content + rejects drift
"""

from __future__ import annotations

from django.test import SimpleTestCase

from apps.cron.patterns import get_handler

# Mutation tools that must NEVER appear in any pattern's toolsAllow — this
# is the structural fix for the 22:07-style cron-creates-duplicate-task
# cascade. See CONTINUITY_cron-typed-patterns.md.
FORBIDDEN_MUTATION_TOOLS = frozenset(
    {
        "nbhd_task_create",
        "nbhd_task_complete",
        "nbhd_task_skip",
        "nbhd_task_defer",
        "nbhd_task_update",
        "nbhd_goal_create",
        "nbhd_goal_update",
        "nbhd_goal_achieve",
        "nbhd_goal_abandon",
        "nbhd_finance_record_payment",
        "nbhd_fuel_log_workout",
        "nbhd_document_append",
        "nbhd_document_put",
        "nbhd_daily_note_set_section",
        "nbhd_daily_note_append",
        "nbhd_memory_update",
        "cron",
    }
)

_RECURRING_SCHEDULE = {"kind": "cron", "expr": "0 8 * * 2", "tz": "Asia/Tokyo"}


class PureReminderTests(SimpleTestCase):
    def setUp(self):
        self.handler = get_handler("pure_reminder")

    def test_payload_validates_minimum(self):
        payload = self.handler.validate_payload({"text": "Take out trash"})
        self.assertEqual(payload.text, "Take out trash")

    def test_payload_rejects_empty_text(self):
        with self.assertRaises(Exception):
            self.handler.validate_payload({"text": ""})

    def test_payload_rejects_extra_fields(self):
        with self.assertRaises(Exception):
            self.handler.validate_payload({"text": "ok", "rogue": "field"})

    def test_build_oc_data_shape(self):
        payload = self.handler.validate_payload({"text": "Take out trash"})
        data = self.handler.build_oc_data(
            payload,
            tenant=None,
            name="trash",
            schedule=_RECURRING_SCHEDULE,
        )
        self.assertEqual(data["name"], "trash")
        self.assertEqual(data["schedule"], _RECURRING_SCHEDULE)
        self.assertEqual(data["sessionTarget"], "isolated")
        self.assertEqual(data["payload"]["kind"], "agentTurn")
        self.assertEqual(data["payload"]["toolsAllow"], ["nbhd_send_to_user"])
        self.assertIn("Take out trash", data["payload"]["message"])

    def test_tools_allow_has_no_mutations(self):
        payload = self.handler.validate_payload({"text": "x"})
        for t in self.handler.get_tools_allow(payload):
            self.assertNotIn(t, FORBIDDEN_MUTATION_TOOLS)

    def test_validate_outbound_accepts_verbatim(self):
        payload = self.handler.validate_payload({"text": "Take out trash"})
        ok, reason = self.handler.validate_outbound_message("Take out trash", payload)
        self.assertTrue(ok, reason)

    def test_validate_outbound_accepts_substring(self):
        payload = self.handler.validate_payload({"text": "Take out trash"})
        ok, _ = self.handler.validate_outbound_message(
            'Friendly reminder: "Take out trash" today!',
            payload,
        )
        self.assertTrue(ok)

    def test_validate_outbound_rejects_drift(self):
        payload = self.handler.validate_payload({"text": "Take out trash"})
        ok, reason = self.handler.validate_outbound_message(
            "Hope you remember to do that thing",
            payload,
        )
        self.assertFalse(ok)
        self.assertIn("verbatim", reason or "")


class QuoteUserIntentTests(SimpleTestCase):
    def setUp(self):
        self.handler = get_handler("quote_user_intent")

    def test_payload_without_refresh(self):
        payload = self.handler.validate_payload({"text": "my appointment is Tuesday 3pm"})
        self.assertEqual(payload.text, "my appointment is Tuesday 3pm")
        self.assertIsNone(payload.refresh_facts_via)

    def test_payload_with_refresh_in_allowlist(self):
        payload = self.handler.validate_payload(
            {
                "text": "appointment Tuesday 3pm",
                "refresh_facts_via": "nbhd_calendar_list_events",
            }
        )
        self.assertEqual(payload.refresh_facts_via, "nbhd_calendar_list_events")

    def test_payload_rejects_refresh_not_in_allowlist(self):
        with self.assertRaises(Exception):
            self.handler.validate_payload(
                {
                    "text": "x",
                    "refresh_facts_via": "nbhd_task_create",
                }
            )

    def test_tools_allow_includes_refresh_when_set(self):
        payload = self.handler.validate_payload(
            {
                "text": "x",
                "refresh_facts_via": "nbhd_calendar_list_events",
            }
        )
        allow = self.handler.get_tools_allow(payload)
        self.assertIn("nbhd_send_to_user", allow)
        self.assertIn("nbhd_calendar_list_events", allow)

    def test_tools_allow_has_no_mutations(self):
        payload = self.handler.validate_payload(
            {
                "text": "x",
                "refresh_facts_via": "nbhd_task_list",
            }
        )
        for t in self.handler.get_tools_allow(payload):
            self.assertNotIn(t, FORBIDDEN_MUTATION_TOOLS)

    def test_validate_outbound_requires_quoted_text(self):
        payload = self.handler.validate_payload(
            {
                "text": "appointment Tuesday 3pm",
            }
        )
        ok, _ = self.handler.validate_outbound_message(
            'Heads up: "appointment Tuesday 3pm" is coming up!',
            payload,
        )
        self.assertTrue(ok)
        ok2, reason = self.handler.validate_outbound_message(
            "Something is happening this week",
            payload,
        )
        self.assertFalse(ok2)


class DomainSummaryTests(SimpleTestCase):
    def setUp(self):
        self.handler = get_handler("domain_summary")

    def test_payload_rejects_unknown_query_tool(self):
        with self.assertRaises(Exception):
            self.handler.validate_payload(
                {
                    "query_tool": "nbhd_bogus",
                    "render_block": "task_summary",
                }
            )

    def test_payload_rejects_mismatched_render_block(self):
        with self.assertRaises(Exception):
            self.handler.validate_payload(
                {
                    "query_tool": "nbhd_task_list",
                    "render_block": "goal_summary",
                }
            )

    def test_payload_accepts_matched_pair(self):
        payload = self.handler.validate_payload(
            {
                "query_tool": "nbhd_task_list",
                "render_block": "task_summary",
                "query_args": {"status": "open"},
            }
        )
        self.assertEqual(payload.query_tool, "nbhd_task_list")
        self.assertEqual(payload.render_block, "task_summary")

    def test_tools_allow_has_no_mutations(self):
        payload = self.handler.validate_payload(
            {
                "query_tool": "nbhd_task_list",
                "render_block": "task_summary",
            }
        )
        for t in self.handler.get_tools_allow(payload):
            self.assertNotIn(t, FORBIDDEN_MUTATION_TOOLS)

    def test_validate_outbound_requires_marker(self):
        payload = self.handler.validate_payload(
            {
                "query_tool": "nbhd_task_list",
                "render_block": "task_summary",
            }
        )
        ok, _ = self.handler.validate_outbound_message(
            "[block: task_summary]\n- 3 open tasks\n- one due today",
            payload,
        )
        self.assertTrue(ok)
        ok2, _ = self.handler.validate_outbound_message(
            "You have 3 open tasks",
            payload,
        )
        self.assertFalse(ok2)


class DailyBriefingTests(SimpleTestCase):
    def setUp(self):
        self.handler = get_handler("daily_briefing")

    def test_payload_defaults(self):
        payload = self.handler.validate_payload({})
        self.assertEqual(payload.warmth_level, "warm")
        self.assertIn("overdue_tasks", payload.sections)

    def test_payload_rejects_unknown_section(self):
        with self.assertRaises(Exception):
            self.handler.validate_payload({"sections": ["overdue_tasks", "bogus"]})

    def test_payload_rejects_unknown_warmth(self):
        with self.assertRaises(Exception):
            self.handler.validate_payload({"warmth_level": "snarky"})

    def test_tools_allow_excludes_all_mutations(self):
        payload = self.handler.validate_payload({})
        allow = self.handler.get_tools_allow(payload)
        self.assertIn("nbhd_send_to_user", allow)
        self.assertIn("nbhd_task_list", allow)
        for t in allow:
            self.assertNotIn(t, FORBIDDEN_MUTATION_TOOLS)

    def test_build_oc_data_contains_strict_fact_sourcing_rules(self):
        payload = self.handler.validate_payload({})
        data = self.handler.build_oc_data(
            payload,
            tenant=None,
            name="Morning Briefing",
            schedule={"kind": "cron", "expr": "0 7 * * *", "tz": "Asia/Tokyo"},
        )
        # The prompt must explicitly forbid item fabrication — this is the
        # behavioural twin of the toolsAllow structural guard.
        message = data["payload"]["message"]
        self.assertIn("nbhd_task_list", message)
        self.assertIn("do not invent", message.lower())

    def test_validate_outbound_requires_marker(self):
        payload = self.handler.validate_payload({})
        ok, _ = self.handler.validate_outbound_message(
            "[block: daily_briefing]\nGood morning!",
            payload,
        )
        self.assertTrue(ok)
        ok2, _ = self.handler.validate_outbound_message(
            "Good morning! Your day looks busy.",
            payload,
        )
        self.assertFalse(ok2)
