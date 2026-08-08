"""Pattern-handler tests: payload validation, build_oc_data shape, outbound validation.

Per pattern, exercises:
  - payload schema rejects bad input
  - build_oc_data emits an OC job dict the gateway will accept (toolsAllow
    has no mutation tools; sessionTarget pairs with payload.kind)
  - validate_outbound_message accepts conformant content + rejects drift
"""

from __future__ import annotations

from django.test import SimpleTestCase, TestCase

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
        # Destructive + irreversible, cascades to subtasks, and its confirm
        # handshake can only be satisfied by a human answering in conversation.
        # No cron pattern may ever hold it — see task_hygiene, which proposes
        # deletions in prose instead.
        "nbhd_task_delete",
        "cron",
    }
)

# The subset of the above that the task_hygiene pattern must ALSO never hold.
# It is deliberately narrower: hygiene's whole job is closing and deferring
# tasks that already exist, so complete/skip/defer are granted to it and only
# to it. What stays forbidden is everything that can INVENT, REWRITE or
# DESTROY — the properties that make a proactive unattended turn dangerous.
HYGIENE_FORBIDDEN_TOOLS = FORBIDDEN_MUTATION_TOOLS - {
    "nbhd_task_complete",
    "nbhd_task_skip",
    "nbhd_task_defer",
}

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

    def test_get_outbound_contract_shape(self):
        payload = self.handler.validate_payload({"text": "Take out trash"})
        contract = self.handler.get_outbound_contract(payload, name="trash")
        self.assertEqual(contract["check"], {"kind": "contains", "text": "Take out trash"})
        self.assertEqual(contract["on_fail"], {"action": "rewrite", "content": "Take out trash"})


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

    def test_get_outbound_contract_shape(self):
        payload = self.handler.validate_payload({"text": "appointment Tuesday 3pm"})
        contract = self.handler.get_outbound_contract(payload, name="appt")
        self.assertEqual(contract["check"], {"kind": "contains", "text": "appointment Tuesday 3pm"})
        self.assertEqual(
            contract["on_fail"],
            {"action": "revise_then_rewrite", "content": "appointment Tuesday 3pm", "max_revisions": 1},
        )


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

    def test_get_outbound_contract_shape(self):
        payload = self.handler.validate_payload(
            {"query_tool": "nbhd_task_list", "render_block": "task_summary"},
        )
        contract = self.handler.get_outbound_contract(payload, name="weekly-tasks")
        self.assertEqual(contract["check"], {"kind": "marker", "marker": "[block: task_summary]"})
        self.assertEqual(contract["on_fail"], {"action": "revise_then_allow", "max_revisions": 1})


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

    def test_get_outbound_contract_shape(self):
        payload = self.handler.validate_payload({})
        contract = self.handler.get_outbound_contract(payload, name="Morning Briefing")
        self.assertEqual(contract["check"], {"kind": "marker", "marker": "[block: daily_briefing]"})
        self.assertEqual(contract["on_fail"], {"action": "revise_then_allow", "max_revisions": 1})


class TaskHygieneTests(SimpleTestCase):
    """The weekly proactive cleanup turn.

    This pattern is the one exception to "no cron may mutate" — so its guard
    rails get tested harder than the read-only patterns', not more loosely.
    """

    def setUp(self):
        self.handler = get_handler("task_hygiene")

    def test_payload_defaults(self):
        payload = self.handler.validate_payload({})
        self.assertEqual(payload.stale_after_days, 14)

    def test_payload_rejects_out_of_range_staleness(self):
        with self.assertRaises(Exception):
            self.handler.validate_payload({"stale_after_days": 0})
        with self.assertRaises(Exception):
            self.handler.validate_payload({"stale_after_days": 400})

    def test_payload_rejects_extra_fields(self):
        # Cron prompts bypass inbound PII redaction, so the payload must never
        # become a channel for free text (the workout_congrats lesson).
        with self.assertRaises(Exception):
            self.handler.validate_payload({"stale_after_days": 14, "notes": "private free text"})

    def test_tools_allow_is_pinned_exactly(self):
        """Drift guard. The allowlist IS the security boundary for this
        pattern — prose in the prompt can be reinterpreted by a new model,
        this list cannot. Any change here is a deliberate decision that must
        be re-reviewed, so the test pins the exact membership, not a subset."""
        payload = self.handler.validate_payload({})
        self.assertEqual(
            self.handler.get_tools_allow(payload),
            [
                "nbhd_task_list",
                "nbhd_task_get",
                "nbhd_goal_list",
                "nbhd_current_status",
                "nbhd_task_complete",
                "nbhd_task_skip",
                "nbhd_task_defer",
                "nbhd_send_to_user",
            ],
        )

    def test_tools_allow_cannot_create_rewrite_or_destroy(self):
        payload = self.handler.validate_payload({})
        allow = self.handler.get_tools_allow(payload)
        for t in allow:
            self.assertNotIn(t, HYGIENE_FORBIDDEN_TOOLS)
        # Named explicitly so the intent survives a refactor of the set above.
        for forbidden in ("nbhd_task_create", "nbhd_task_update", "nbhd_task_delete"):
            self.assertNotIn(forbidden, allow)

    def test_other_patterns_still_cannot_mutate(self):
        """task_hygiene must not become a precedent. The read-only patterns
        keep their absolute no-mutation guard — this asserts granting hygiene
        its budget did not quietly widen theirs."""
        for pattern in ("daily_briefing", "domain_summary", "pure_reminder"):
            with self.subTest(pattern=pattern):
                handler = get_handler(pattern)
                payload_dict = (
                    {"query_tool": "nbhd_task_list", "render_block": "task_summary"}
                    if pattern == "domain_summary"
                    else ({"text": "x"} if pattern == "pure_reminder" else {})
                )
                payload = handler.validate_payload(payload_dict)
                for t in handler.get_tools_allow(payload):
                    self.assertNotIn(t, FORBIDDEN_MUTATION_TOOLS)

    def test_build_oc_data_shape(self):
        payload = self.handler.validate_payload({})
        data = self.handler.build_oc_data(
            payload,
            tenant=None,
            name="Task Hygiene",
            schedule=_RECURRING_SCHEDULE,
        )
        self.assertEqual(data["name"], "Task Hygiene")
        self.assertEqual(data["sessionTarget"], "isolated")
        self.assertEqual(data["wakeMode"], "next-heartbeat")
        self.assertEqual(data["payload"]["kind"], "agentTurn")
        # Delivery routes through Django (iOS-reachable), never OC channels.
        self.assertEqual(data["delivery"], {"mode": "none"})

    def test_prompt_proposes_deletion_rather_than_performing_it(self):
        """The behavioural twin of the toolsAllow guard: the prompt must tell
        the model deletion is out of reach here AND why, so it proposes instead
        of trying and failing."""
        payload = self.handler.validate_payload({})
        message = self.handler.build_oc_data(
            payload,
            tenant=None,
            name="Task Hygiene",
            schedule=_RECURRING_SCHEDULE,
        )["payload"]["message"]
        self.assertIn("nbhd_task_delete", message)
        self.assertIn("not available in this turn", message)
        # The reason must be the structural one, not just "please don't".
        self.assertIn("confirmation cannot be obtained", message)

    def test_prompt_carries_the_staleness_threshold_from_the_payload(self):
        payload = self.handler.validate_payload({"stale_after_days": 30})
        message = self.handler.build_oc_data(
            payload,
            tenant=None,
            name="Task Hygiene",
            schedule=_RECURRING_SCHEDULE,
        )["payload"]["message"]
        self.assertIn("30 days", message)

    def test_prompt_requires_silence_when_nothing_changed(self):
        """A new proactive sender that messages every week regardless is a
        weekly spam channel. The no-news-no-message rule is load-bearing."""
        payload = self.handler.validate_payload({})
        message = self.handler.build_oc_data(
            payload,
            tenant=None,
            name="Task Hygiene",
            schedule=_RECURRING_SCHEDULE,
        )["payload"]["message"]
        self.assertIn("send NOTHING AT ALL", message)
        self.assertIn("EXACTLY ONCE", message)

    def test_validate_outbound_requires_marker(self):
        payload = self.handler.validate_payload({})
        ok, _ = self.handler.validate_outbound_message(
            "[block: task_hygiene]\nClosed 2, deferred 1.",
            payload,
        )
        self.assertTrue(ok)
        ok2, reason = self.handler.validate_outbound_message(
            "Closed 2 tasks and deferred 1.",
            payload,
        )
        self.assertFalse(ok2)
        self.assertIn("[block: task_hygiene]", reason)

    def test_get_outbound_contract_shape(self):
        payload = self.handler.validate_payload({})
        contract = self.handler.get_outbound_contract(payload, name="Task Hygiene")
        self.assertEqual(contract["check"], {"kind": "marker", "marker": "[block: task_hygiene]"})
        self.assertEqual(contract["on_fail"], {"action": "revise_then_allow", "max_revisions": 1})


_AT_SCHEDULE = {"kind": "at", "at": "2099-01-01T00:00:00+00:00"}


class WorkoutCongratsTests(SimpleTestCase):
    def setUp(self):
        self.handler = get_handler("workout_congrats")

    def test_payload_validates_minimum(self):
        payload = self.handler.validate_payload({"activity": "Push Day"})
        self.assertEqual(payload.activity, "Push Day")
        self.assertIsNone(payload.duration_minutes)
        self.assertIsNone(payload.rpe)

    def test_payload_rejects_empty_activity(self):
        with self.assertRaises(Exception):
            self.handler.validate_payload({"activity": ""})

    def test_payload_rejects_extra_fields(self):
        # Free-text notes must never leak into the payload (cron prompts bypass
        # inbound PII redaction) — extra="forbid" enforces it structurally.
        with self.assertRaises(Exception):
            self.handler.validate_payload({"activity": "ok", "notes": "private free text"})

    def test_payload_rejects_out_of_range_rpe(self):
        with self.assertRaises(Exception):
            self.handler.validate_payload({"activity": "ok", "rpe": 42})

    def test_build_oc_data_shape(self):
        payload = self.handler.validate_payload(
            {
                "activity": "Push — Chest & Shoulders",
                "category": "strength",
                "duration_minutes": 52,
                "rpe": 8,
                "pr_summary": "New PR: Bench — est. 1RM 116.7 kg (from 100 kg × 5)",
            }
        )
        data = self.handler.build_oc_data(
            payload,
            tenant=None,
            name="_congrats-abc",
            schedule=_AT_SCHEDULE,
        )
        self.assertEqual(data["name"], "_congrats-abc")
        self.assertEqual(data["schedule"], _AT_SCHEDULE)
        self.assertEqual(data["sessionTarget"], "isolated")
        self.assertEqual(data["payload"]["kind"], "agentTurn")
        self.assertEqual(data["payload"]["toolsAllow"], ["nbhd_send_to_user"])
        # iOS-reachable delivery shape — send routes through Django, not OC channels.
        self.assertEqual(data["delivery"], {"mode": "none"})
        message = data["payload"]["message"]
        self.assertIn("Push — Chest & Shoulders", message)
        self.assertIn("RPE 8", message)
        self.assertIn("call it estimated", message)
        self.assertIn("actual source set", message)
        self.assertIn("New PR: Bench — est. 1RM 116.7 kg (from 100 kg × 5)", message)
        self.assertIn("nbhd_send_to_user", message)

    def test_tools_allow_has_no_mutations(self):
        payload = self.handler.validate_payload({"activity": "x"})
        for t in self.handler.get_tools_allow(payload):
            self.assertNotIn(t, FORBIDDEN_MUTATION_TOOLS)

    def test_validate_outbound_accepts_warm_note(self):
        payload = self.handler.validate_payload({"activity": "Push Day"})
        ok, reason = self.handler.validate_outbound_message(
            "Strong push session — that's your third this week, nice work!",
            payload,
        )
        self.assertTrue(ok, reason)

    def test_validate_outbound_rejects_empty(self):
        payload = self.handler.validate_payload({"activity": "Push Day"})
        ok, reason = self.handler.validate_outbound_message("   ", payload)
        self.assertFalse(ok)
        self.assertIn("non-empty", reason or "")

    def test_validate_outbound_rejects_over_long(self):
        payload = self.handler.validate_payload({"activity": "Push Day"})
        ok, _ = self.handler.validate_outbound_message("x" * 5000, payload)
        self.assertFalse(ok)

    def test_fallback_message_names_activity(self):
        payload = self.handler.validate_payload({"activity": "Push Day"})
        self.assertEqual(
            self.handler.get_fallback_message(payload, name="_congrats-abc"), "Nice work finishing Push Day."
        )

    def test_get_outbound_contract_shape(self):
        payload = self.handler.validate_payload({"activity": "Push Day"})
        contract = self.handler.get_outbound_contract(payload, name="_congrats-abc")
        self.assertEqual(contract["check"], {"kind": "bounded", "max": 800})
        self.assertEqual(
            contract["on_fail"],
            {"action": "rewrite", "content": "Nice work finishing Push Day."},
        )


# ── Parity case-table ──────────────────────────────────────────────────────
# The drift control between this Python spec and the nbhd-cron-enforcement
# plugin's JS evaluator (runtime/openclaw/plugins/nbhd-cron-enforcement/test.js,
# PARITY_CASES). Every case here has an IDENTICAL twin there, expressed against
# the generic {kind, ...} check dict instead of a pattern + payload. If you
# change either side's pass/fail semantics, update BOTH tables — that manual
# duplication is the whole point: it's the only way to catch Python/JS drift
# in a two-language plugin. See apps/cron/patterns/base.py:get_outbound_contract.
#
# Tuple shape: (pattern, payload_dict, content, expected_ok)
PARITY_CASES: tuple[tuple[str, dict, str, bool], ...] = (
    # ── pure_reminder: contains ─────────────────────────────────────────
    ("pure_reminder", {"text": "Take out trash"}, "Take out trash", True),
    ("pure_reminder", {"text": "Take out trash"}, 'Reminder: "Take out trash" today!', True),
    ("pure_reminder", {"text": "Take out trash"}, "Don't forget your chores", False),
    # ── quote_user_intent: contains ─────────────────────────────────────
    (
        "quote_user_intent",
        {"text": "appointment Tuesday 3pm"},
        'Heads up — "appointment Tuesday 3pm" is coming up!',
        True,
    ),
    ("quote_user_intent", {"text": "appointment Tuesday 3pm"}, "Something is happening this week", False),
    # ── domain_summary: marker ───────────────────────────────────────────
    (
        "domain_summary",
        {"query_tool": "nbhd_task_list", "render_block": "task_summary"},
        "[block: task_summary]\n- 3 open tasks",
        True,
    ),
    (
        "domain_summary",
        {"query_tool": "nbhd_task_list", "render_block": "task_summary"},
        "You have 3 open tasks",
        False,
    ),
    # ── daily_briefing: marker ───────────────────────────────────────────
    ("daily_briefing", {}, "[block: daily_briefing]\nGood morning!", True),
    ("daily_briefing", {}, "Good morning! Your day looks busy.", False),
    # ── task_hygiene: marker ─────────────────────────────────────────────
    ("task_hygiene", {}, "[block: task_hygiene]\nClosed 2, deferred 1.", True),
    ("task_hygiene", {}, "I tidied up your task list this week.", False),
    # ── workout_congrats: bounded ────────────────────────────────────────
    ("workout_congrats", {"activity": "Push Day"}, "Great push session — third this week!", True),
    ("workout_congrats", {"activity": "Push Day"}, "   ", False),
    ("workout_congrats", {"activity": "Push Day"}, "x" * 900, False),
    # Code-point vs UTF-16-code-unit drift regression (Fable review, round 2):
    # 400 ASCII + 250 astral emoji (U+1F4AA, outside the BMP) = 650 Unicode
    # code points (what Python's `len(str)` counts) but 900 UTF-16 code units
    # (what a naive JS `.length` counts, since each astral char is a surrogate
    # pair). Python passes this (650 <= 800); the JS evaluator must too — it
    # counts code points via `[...trimmed].length`, not `.length`.
    ("workout_congrats", {"activity": "Push Day"}, "x" * 400 + "\U0001f4aa" * 250, True),
)


class OutboundContractParityTests(SimpleTestCase):
    """Python side of the parity case-table — asserted against
    ``validate_outbound_message`` (the canonical spec). The JS twin
    (nbhd-cron-enforcement/test.js) asserts the same verdicts via
    ``evaluateCheck`` against the corresponding ``check`` dict."""

    def test_all_cases_match_expected_verdict(self):
        for pattern, payload_dict, content, expected_ok in PARITY_CASES:
            with self.subTest(pattern=pattern, content=content):
                handler = get_handler(pattern)
                payload = handler.validate_payload(payload_dict)
                ok, _reason = handler.validate_outbound_message(content, payload)
                self.assertEqual(ok, expected_ok)


# Minimal schema-valid payload per pattern that pins a fire-time model. All five
# typed-cron patterns now pin the allowlist-safe DeepSeek Flash model: the four
# that hardcoded "haiku-4.5" (fixed in #1167) plus daily_briefing, which pinned
# "sonnet-4.6" until MJ's 2026-07-12 decision to trade briefing quality for
# allowlist safety on non-BYO tenants (sonnet is a BYO-only model, so it failed
# preflight for everyone without an active Anthropic credential). Used to assert
# each pattern's resolved model lands inside the firing tenant's allowlist.
_MODEL_PINNING_PATTERN_PAYLOADS: dict[str, dict] = {
    "pure_reminder": {"text": "Take out trash"},
    "quote_user_intent": {"text": "appointment Tuesday 3pm"},
    "domain_summary": {"query_tool": "nbhd_task_list", "render_block": "task_summary"},
    "workout_congrats": {"activity": "Push Day"},
    "daily_briefing": {"sections": ["overdue_tasks", "due_today"], "warmth_level": "warm"},
    "task_hygiene": {"stale_after_days": 14},
}


class TypedCronPayloadModelTests(TestCase):
    """Regression: a typed-cron payload model outside the tenant's
    ``agents.defaults.models`` allowlist makes OpenClaw's cron preflight reject
    the fire-turn (``payload.model '...' rejected by agents.defaults.models
    allowlist``) — the turn dies before ``nbhd_send_to_user`` and no delivery
    happens. Every pattern's fire-time model MUST be a member of the firing
    tenant's resolved allowlist.

    Both the model and the allowlist are derived from the real config_generator
    path (``build_oc_data`` / ``resolve_tenant_models``), never a fixture that
    could re-state the bug. ``resolve_tenant_models``'s ``model_entries`` is
    exactly what ``generate_openclaw_config`` writes as ``agents.defaults.models``.
    """

    def _payload_model(self, pattern: str, tenant) -> str:
        handler = get_handler(pattern)
        payload = handler.validate_payload(_MODEL_PINNING_PATTERN_PAYLOADS[pattern])
        data = handler.build_oc_data(payload, tenant=tenant, name="probe", schedule=_RECURRING_SCHEDULE)
        return data["payload"]["model"]

    def test_starter_non_byo_model_in_allowlist(self):
        from apps.orchestrator.config_generator import resolve_tenant_models
        from apps.tenants.models import Tenant, User

        user = User.objects.create_user(username="cron-starter", password="x" * 32)
        tenant = Tenant.objects.create(user=user, model_tier="starter")

        # The allowlist exactly as generate_openclaw_config writes it into
        # agents.defaults.models — what OC preflight checks payload.model against.
        _config, allowlist, _fallbacks = resolve_tenant_models(tenant)

        for pattern in _MODEL_PINNING_PATTERN_PAYLOADS:
            with self.subTest(pattern=pattern):
                model = self._payload_model(pattern, tenant)
                self.assertIn(
                    model,
                    allowlist,
                    f"{pattern} pinned {model!r}, outside the tenant allowlist "
                    f"{sorted(allowlist)} — OC preflight would reject the fire-turn.",
                )
                # The exact bug this guards: a hardcoded model no non-BYO
                # allowlist contains.
                self.assertNotEqual(model, "haiku-4.5")

    def test_byo_model_in_allowlist_and_stays_off_metered_subscription(self):
        from apps.billing.constants import ANTHROPIC_SONNET_MODEL
        from apps.byo_models.models import BYOCredential
        from apps.orchestrator.config_generator import _byo_model_extras, resolve_tenant_models
        from apps.tenants.models import Tenant, User

        user = User.objects.create_user(username="cron-byo", password="x" * 32)
        tenant = Tenant.objects.create(user=user, model_tier="starter")
        tenant.byo_models_enabled = True
        tenant.save(update_fields=["byo_models_enabled"])
        BYOCredential.objects.create(
            tenant=tenant,
            provider=BYOCredential.Provider.ANTHROPIC,
            mode=BYOCredential.Mode.CLI_SUBSCRIPTION,
            key_vault_secret_name="x",
            status=BYOCredential.Status.VERIFIED,
        )

        _config, allowlist, _fallbacks = resolve_tenant_models(tenant)
        byo_extras = _byo_model_extras(tenant)
        # Sanity: BYO extras really are live (Claude is in the allowlist), so the
        # assertions below aren't vacuously true against a non-BYO shape.
        self.assertIn(ANTHROPIC_SONNET_MODEL, allowlist)
        self.assertTrue(byo_extras)

        for pattern in _MODEL_PINNING_PATTERN_PAYLOADS:
            with self.subTest(pattern=pattern):
                model = self._payload_model(pattern, tenant)
                self.assertIn(model, allowlist)
                # Platform-fired crons never burn the tenant's metered Claude
                # subscription (matches TIER_TASK_DEFAULTS' NON-BYO invariant).
                self.assertNotIn(model, byo_extras)
