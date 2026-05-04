"""Tests for the cron context envelope.

Covers ``_build_context_envelope`` and its component fetchers
(``_envelope_goals``, ``_envelope_open_tasks``, ``_envelope_recent_lessons``),
plus the wiring through ``_prepare_cron_prompt`` and ``_build_cron_message``.

``create_tenant`` seeds starter Documents (goals, tasks, ideas, memory) with
placeholder content. The envelope helpers must skip those placeholders so a
fresh tenant doesn't see tutorial bullets injected as "open tasks". The tests
clear seeded docs in ``setUp`` to isolate behavior, with dedicated tests for
the seed-detection path.
"""

from __future__ import annotations

from django.test import TestCase

from apps.journal.models import Document
from apps.journal.services import STARTER_DOCUMENT_TEMPLATES
from apps.lessons.models import Lesson
from apps.orchestrator.config_generator import (
    _CRON_CONTEXT_PREAMBLE,
    _build_context_envelope,
    _build_cron_message,
    _envelope_goals,
    _envelope_open_tasks,
    _envelope_recent_lessons,
    _prepare_cron_prompt,
)
from apps.tenants.services import create_tenant


def _clear_seed_docs(tenant) -> None:
    """Strip the placeholder Documents that ``create_tenant`` seeds."""
    Document.objects.filter(tenant=tenant).delete()


def _starter_md(slug: str) -> str:
    for tmpl in STARTER_DOCUMENT_TEMPLATES:
        if tmpl["slug"] == slug:
            return tmpl["markdown"]
    raise KeyError(slug)


class EnvelopeGoalsTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="EnvGoals", telegram_chat_id=910001)
        _clear_seed_docs(self.tenant)

    def test_returns_empty_when_no_goals_doc(self):
        self.assertEqual(_envelope_goals(self.tenant), "")

    def test_returns_empty_when_goals_doc_blank(self):
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.GOAL,
            slug="goals",
            title="Goals",
            markdown="   \n  \n",
        )
        self.assertEqual(_envelope_goals(self.tenant), "")

    def test_returns_full_markdown_under_cap(self):
        md = "## Active\n- Ship the envelope\n- Run the canary"
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.GOAL,
            slug="goals",
            title="Goals",
            markdown=md,
        )
        self.assertEqual(_envelope_goals(self.tenant), md)

    def test_truncates_when_over_cap(self):
        md = "x" * 2000
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.GOAL,
            slug="goals",
            title="Goals",
            markdown=md,
        )
        out = _envelope_goals(self.tenant, max_chars=1500)
        self.assertTrue(out.startswith("x" * 1500))
        self.assertIn("truncated", out)
        self.assertLess(len(out), 2000)

    def test_only_picks_goals_doc_for_this_tenant(self):
        other = create_tenant(display_name="Other", telegram_chat_id=910002)
        _clear_seed_docs(other)
        Document.objects.create(
            tenant=other,
            kind=Document.Kind.GOAL,
            slug="goals",
            title="Other goals",
            markdown="other tenant content",
        )
        self.assertEqual(_envelope_goals(self.tenant), "")

    def test_skips_unmodified_starter_seed(self):
        # A fresh tenant whose goals doc is still the seed template should
        # contribute no goals to the envelope.
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.GOAL,
            slug="goals",
            title="Goals",
            markdown=_starter_md("goals"),
        )
        self.assertEqual(_envelope_goals(self.tenant), "")

    def test_treats_user_addition_as_real_content(self):
        # User kept the seed scaffolding and added a real goal underneath.
        custom = _starter_md("goals") + "\n- Train for half-marathon\n"
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.GOAL,
            slug="goals",
            title="Goals",
            markdown=custom,
        )
        out = _envelope_goals(self.tenant)
        self.assertIn("Train for half-marathon", out)


class EnvelopeOpenTasksTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="EnvTasks", telegram_chat_id=910010)
        _clear_seed_docs(self.tenant)

    def test_returns_empty_when_no_tasks_doc(self):
        self.assertEqual(_envelope_open_tasks(self.tenant), "")

    def test_returns_only_open_items(self):
        md = (
            "## Tasks\n"
            "- [ ] First open task\n"
            "- [x] Already done\n"
            "- [ ] Second open task\n"
            "Some prose that isn't a task line\n"
            "  - [ ] Indented open task\n"
        )
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.TASKS,
            slug="tasks",
            title="Tasks",
            markdown=md,
        )
        out = _envelope_open_tasks(self.tenant)
        self.assertIn("- [ ] First open task", out)
        self.assertIn("- [ ] Second open task", out)
        self.assertIn("- [ ] Indented open task", out)
        self.assertNotIn("Already done", out)
        self.assertNotIn("Some prose", out)

    def test_returns_empty_when_no_open_items(self):
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.TASKS,
            slug="tasks",
            title="Tasks",
            markdown="- [x] Done one\n- [x] Done two\n",
        )
        self.assertEqual(_envelope_open_tasks(self.tenant), "")

    def test_caps_at_max_items_with_overflow_hint(self):
        lines = "\n".join(f"- [ ] Task {i}" for i in range(40))
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.TASKS,
            slug="tasks",
            title="Tasks",
            markdown=lines,
        )
        out = _envelope_open_tasks(self.tenant, max_items=10)
        self.assertEqual(out.count("- [ ] Task"), 10)
        self.assertIn("+30 more open tasks", out)

    def test_skips_starter_placeholder_tasks(self):
        # Fresh tenant whose tasks doc is still the seed template should
        # contribute zero open tasks (placeholders aren't real tasks).
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.TASKS,
            slug="tasks",
            title="Tasks",
            markdown=_starter_md("tasks"),
        )
        self.assertEqual(_envelope_open_tasks(self.tenant), "")

    def test_keeps_real_tasks_alongside_starter(self):
        # User added a real task next to the starter ones — only the real
        # one shows up in the envelope.
        md = _starter_md("tasks") + "\n- [ ] Real task the user added\n"
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.TASKS,
            slug="tasks",
            title="Tasks",
            markdown=md,
        )
        out = _envelope_open_tasks(self.tenant)
        self.assertIn("Real task the user added", out)
        self.assertNotIn("Add one tiny task", out)
        self.assertNotIn("Keep going", out)


class EnvelopeRecentLessonsTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="EnvLessons", telegram_chat_id=910020)

    def test_returns_empty_when_none(self):
        self.assertEqual(_envelope_recent_lessons(self.tenant), "")

    def test_returns_approved_only(self):
        Lesson.objects.create(
            tenant=self.tenant,
            text="Pending lesson — should be skipped",
            source_type="conversation",
            status="pending",
        )
        Lesson.objects.create(
            tenant=self.tenant,
            text="Dismissed lesson — should be skipped",
            source_type="conversation",
            status="dismissed",
        )
        Lesson.objects.create(
            tenant=self.tenant,
            text="Approved insight worth surfacing",
            source_type="conversation",
            status="approved",
        )
        out = _envelope_recent_lessons(self.tenant)
        self.assertIn("Approved insight worth surfacing", out)
        self.assertNotIn("Pending", out)
        self.assertNotIn("Dismissed", out)

    def test_limits_to_most_recent(self):
        for i in range(5):
            Lesson.objects.create(
                tenant=self.tenant,
                text=f"Lesson {i}",
                source_type="conversation",
                status="approved",
            )
        out = _envelope_recent_lessons(self.tenant, limit=3)
        # Most recent three (by created_at desc) → Lessons 4, 3, 2
        self.assertEqual(out.count("- Lesson"), 3)
        self.assertIn("Lesson 4", out)
        self.assertIn("Lesson 3", out)
        self.assertIn("Lesson 2", out)
        self.assertNotIn("Lesson 0", out)

    def test_truncates_long_text_to_one_line(self):
        Lesson.objects.create(
            tenant=self.tenant,
            text="line one of a multi-line lesson\nline two should be dropped",
            source_type="conversation",
            status="approved",
        )
        out = _envelope_recent_lessons(self.tenant)
        self.assertIn("line one of a multi-line lesson", out)
        self.assertNotIn("line two", out)


class BuildContextEnvelopeTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="EnvBuild", telegram_chat_id=910030)
        _clear_seed_docs(self.tenant)

    def test_empty_when_no_state(self):
        self.assertEqual(_build_context_envelope(self.tenant), "")

    def test_empty_when_only_starter_seeds(self):
        # Fresh-tenant scenario: starter goals + starter tasks docs exist but
        # neither has been customized → envelope should be empty.
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.GOAL,
            slug="goals",
            title="Goals",
            markdown=_starter_md("goals"),
        )
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.TASKS,
            slug="tasks",
            title="Tasks",
            markdown=_starter_md("tasks"),
        )
        self.assertEqual(_build_context_envelope(self.tenant), "")

    def test_includes_all_three_when_all_present(self):
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.GOAL,
            slug="goals",
            title="Goals",
            markdown="- Ship envelope to canary",
        )
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.TASKS,
            slug="tasks",
            title="Tasks",
            markdown="- [ ] Run unit tests\n- [x] Read hook site",
        )
        Lesson.objects.create(
            tenant=self.tenant,
            text="Pre-fetched state beats agent tool reliance",
            source_type="conversation",
            status="approved",
        )
        env = _build_context_envelope(self.tenant)
        self.assertIn("Pre-loaded user state", env)
        self.assertIn("### Active goals", env)
        self.assertIn("Ship envelope to canary", env)
        self.assertIn("### Open tasks", env)
        self.assertIn("- [ ] Run unit tests", env)
        self.assertNotIn("Read hook site", env)  # closed task excluded
        self.assertIn("### Recent lessons", env)
        self.assertIn("Pre-fetched state beats agent tool reliance", env)
        self.assertTrue(env.endswith("---\n\n"))

    def test_skips_missing_sections_but_keeps_present_ones(self):
        # Only tasks present
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.TASKS,
            slug="tasks",
            title="Tasks",
            markdown="- [ ] Solo task",
        )
        env = _build_context_envelope(self.tenant)
        self.assertIn("### Open tasks", env)
        self.assertIn("Solo task", env)
        self.assertNotIn("### Active goals", env)
        self.assertNotIn("### Recent lessons", env)


class PrepareCronPromptTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="EnvPrepare", telegram_chat_id=910040)
        _clear_seed_docs(self.tenant)
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.GOAL,
            slug="goals",
            title="Goals",
            markdown="- Ship the canary",
        )

    def test_envelope_present_by_default(self):
        out = _prepare_cron_prompt("BODY", self.tenant)
        self.assertIn("Current date and time:", out)
        self.assertIn("Pre-loaded user state", out)
        self.assertIn("Ship the canary", out)
        self.assertIn(_CRON_CONTEXT_PREAMBLE, out)
        self.assertTrue(out.endswith("BODY"))

    def test_envelope_absent_when_with_envelope_false(self):
        out = _prepare_cron_prompt("BODY", self.tenant, with_envelope=False)
        self.assertIn("Current date and time:", out)
        self.assertNotIn("Pre-loaded user state", out)
        self.assertIn(_CRON_CONTEXT_PREAMBLE, out)
        self.assertTrue(out.endswith("BODY"))

    def test_structural_order_date_then_envelope_then_preamble_then_body(self):
        out = _prepare_cron_prompt("THE_BODY", self.tenant)
        idx_date = out.index("Current date and time:")
        idx_env = out.index("Pre-loaded user state")
        idx_preamble = out.index("MANDATORY")
        idx_body = out.index("THE_BODY")
        self.assertLess(idx_date, idx_env)
        self.assertLess(idx_env, idx_preamble)
        self.assertLess(idx_preamble, idx_body)

    def test_message_still_starts_with_date_line_so_default_prefix_match_holds(self):
        # update_system_cron_prompts uses "Current date and time:" as a known
        # default-prompt prefix to decide whether to overwrite an existing
        # cron message. The envelope must not break that prefix match.
        out = _prepare_cron_prompt("BODY", self.tenant)
        self.assertTrue(out.startswith("Current date and time:"))


class BuildCronMessageTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="EnvBuildMsg", telegram_chat_id=910050)
        _clear_seed_docs(self.tenant)
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.GOAL,
            slug="goals",
            title="Goals",
            markdown="- Goal A",
        )

    def test_foreground_appends_phase2_block(self):
        out = _build_cron_message("BODY", "TestJob", foreground=True, tenant=self.tenant)
        self.assertIn("Pre-loaded user state", out)
        self.assertIn("FINAL STEP — conditional sync to the main session", out)

    def test_background_omits_phase2_block(self):
        out = _build_cron_message("BODY", "TestJob", foreground=False, tenant=self.tenant)
        self.assertIn("Pre-loaded user state", out)
        self.assertNotIn("FINAL STEP — conditional sync to the main session", out)

    def test_with_envelope_false_skips_envelope(self):
        out = _build_cron_message("BODY", "TestJob", foreground=False, tenant=self.tenant, with_envelope=False)
        self.assertNotIn("Pre-loaded user state", out)
        self.assertIn(_CRON_CONTEXT_PREAMBLE, out)

    def test_default_includes_envelope(self):
        out = _build_cron_message("BODY", "TestJob", foreground=True, tenant=self.tenant)
        self.assertIn("Pre-loaded user state", out)
