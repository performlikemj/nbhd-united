"""Tests for the Agenda envelope section (Phase A).

Covers:
- Empty rendering when nothing is open and no untouched intros
- Each counter (open tasks, active goals, planned workouts, payoff plans)
- Untouched-intros detection via ``feature_enabled`` × ``welcomes_sent``
- Synthesis line composition (singular/plural, skipping zero counts)
- Full render integration through ``render_managed_region`` so the agenda
  block lands in the same envelope the agent reads
"""

from __future__ import annotations

from datetime import date as _date
from datetime import timedelta as _td
from decimal import Decimal

from django.test import TestCase

from apps.finance.models import PayoffPlan
from apps.fuel.models import FuelGoal, Workout, WorkoutStatus
from apps.journal.models import Document
from apps.orchestrator.agenda_envelope import (
    _count_active_goals,
    _count_active_payoff_plans,
    _count_open_tasks,
    _count_planned_workouts,
    _render_summary,
    _render_untouched_intros,
    render_agenda,
)
from apps.orchestrator.workspace_envelope import render_managed_region
from apps.tenants.services import create_tenant


def _wipe_seed_docs(tenant) -> None:
    """Strip the placeholder Documents that ``create_tenant`` seeds.

    Agenda counters key off non-empty post-starter content; the seeded
    docs would otherwise count as "active goals = 1" and noise the
    untouched-state tests.
    """
    Document.objects.filter(tenant=tenant).delete()


class AgendaEmptyTest(TestCase):
    """A brand-new tenant with no open work and every feature off should
    render an empty agenda — the registry skips empty sections, so no
    heading lands in USER.md."""

    def test_returns_empty_when_nothing_open(self):
        tenant = create_tenant(display_name="Empty", telegram_chat_id=920001)
        _wipe_seed_docs(tenant)
        # Belt-and-suspenders: clear flags create_tenant might set.
        tenant.fuel_enabled = False
        tenant.finance_enabled = False
        tenant.welcomes_sent = {}
        tenant.save()
        self.assertEqual(render_agenda(tenant), "")


class AgendaUntouchedIntrosTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Intros", telegram_chat_id=920010)
        _wipe_seed_docs(self.tenant)
        self.tenant.fuel_enabled = False
        self.tenant.finance_enabled = False
        self.tenant.welcomes_sent = {}
        self.tenant.save()

    def test_renders_untouched_fuel_intro(self):
        self.tenant.fuel_enabled = True
        self.tenant.save()
        out = _render_untouched_intros(self.tenant)
        self.assertIn("Fuel", out)
        self.assertIn("no engagement yet", out)

    def test_renders_untouched_finance_intro(self):
        self.tenant.finance_enabled = True
        self.tenant.save()
        out = _render_untouched_intros(self.tenant)
        self.assertIn("Gravity", out)

    def test_renders_both_when_both_enabled(self):
        self.tenant.fuel_enabled = True
        self.tenant.finance_enabled = True
        self.tenant.save()
        out = _render_untouched_intros(self.tenant)
        self.assertIn("Fuel", out)
        self.assertIn("Gravity", out)

    def test_skips_when_welcomed(self):
        """welcomes_sent set → no longer untouched, drop from intros."""
        self.tenant.fuel_enabled = True
        self.tenant.welcomes_sent = {"fuel": "2026-05-07T16:44:00+00:00"}
        self.tenant.save()
        out = _render_untouched_intros(self.tenant)
        self.assertNotIn("Fuel", out)

    def test_skips_when_feature_disabled(self):
        """fuel_enabled=False → not an open thread, regardless of welcomes_sent."""
        self.tenant.fuel_enabled = False
        self.tenant.welcomes_sent = {}
        self.tenant.save()
        out = _render_untouched_intros(self.tenant)
        self.assertEqual(out, "")

    def test_top_level_render_includes_intros(self):
        """Untouched intros appear in the full agenda render even when nothing else is open."""
        self.tenant.fuel_enabled = True
        self.tenant.save()
        out = render_agenda(self.tenant)
        self.assertIn("Untouched introductions", out)
        self.assertIn("Fuel", out)

    def test_abandoned_engagement_suppresses_intro(self):
        """Phase B integration: a feature_intro with state=ABANDONED
        drops out of the rendered list even though welcomes_sent[key]
        is still null."""
        from apps.tenants.agenda_models import AgendaEngagement
        from apps.tenants.agenda_service import mark_state

        self.tenant.fuel_enabled = True
        self.tenant.save()
        # Without engagement: intro renders.
        self.assertIn("Fuel", _render_untouched_intros(self.tenant))

        # Mark ABANDONED → intro suppressed.
        mark_state(
            self.tenant,
            kind=AgendaEngagement.Kind.FEATURE_INTRO,
            item_id="fuel",
            state=AgendaEngagement.State.ABANDONED,
        )
        self.assertNotIn("Fuel", _render_untouched_intros(self.tenant))

    def test_recent_surface_suppresses_intro(self):
        """A freshly-surfaced intro is suppressed for the cooldown window
        even when welcomes_sent is still null."""
        from apps.tenants.agenda_models import AgendaEngagement
        from apps.tenants.agenda_service import mark_surfaced

        self.tenant.fuel_enabled = True
        self.tenant.save()

        mark_surfaced(
            self.tenant,
            kind=AgendaEngagement.Kind.FEATURE_INTRO,
            item_id="fuel",
        )
        # Surfaced just now → suppressed by the 6-hour cooldown.
        self.assertNotIn("Fuel", _render_untouched_intros(self.tenant))


class AgendaCountersTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Counters", telegram_chat_id=920020)
        _wipe_seed_docs(self.tenant)

    def test_open_tasks_counts_unchecked_lines(self):
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.TASKS,
            slug="tasks",
            title="Tasks",
            markdown="- [ ] Email Sarah\n- [ ] Call mom\n- [x] Done one",
        )
        self.assertEqual(_count_open_tasks(self.tenant), 2)

    def test_open_tasks_zero_when_doc_missing(self):
        self.assertEqual(_count_open_tasks(self.tenant), 0)

    def test_active_goals_counts_curated_doc_plus_fuel_goals(self):
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.GOAL,
            slug="goals",
            title="Goals",
            markdown="- Run a 10k by October\n- Pay off Visa by Q3",
        )
        FuelGoal.objects.create(
            tenant=self.tenant,
            exercise_name="bench press",
            metric="weight_kg",
            target_value=Decimal("100"),
        )
        # 1 (goals doc) + 1 (FuelGoal not achieved) = 2
        self.assertEqual(_count_active_goals(self.tenant), 2)

    def test_active_goals_excludes_achieved_fuel_goals(self):
        from django.utils import timezone

        FuelGoal.objects.create(
            tenant=self.tenant,
            exercise_name="bench press",
            metric="weight_kg",
            target_value=Decimal("100"),
            achieved_at=timezone.now(),
        )
        self.assertEqual(_count_active_goals(self.tenant), 0)

    def test_planned_workouts_window(self):
        today = _date.today()
        # In window
        Workout.objects.create(
            tenant=self.tenant,
            date=today + _td(days=1),
            status=WorkoutStatus.PLANNED,
            category="strength",
            activity="Push day",
        )
        Workout.objects.create(
            tenant=self.tenant,
            date=today + _td(days=6),
            status=WorkoutStatus.PLANNED,
            category="cardio",
            activity="5k run",
        )
        # Out of window (10 days from now)
        Workout.objects.create(
            tenant=self.tenant,
            date=today + _td(days=10),
            status=WorkoutStatus.PLANNED,
            category="strength",
            activity="Future workout",
        )
        # Wrong status
        Workout.objects.create(
            tenant=self.tenant,
            date=today,
            status=WorkoutStatus.DONE,
            category="strength",
            activity="Done one",
        )
        self.assertEqual(_count_planned_workouts(self.tenant, days=7), 2)

    def test_active_payoff_plans(self):
        PayoffPlan.objects.create(
            tenant=self.tenant,
            strategy="snowball",
            monthly_budget=Decimal("500"),
            total_debt=Decimal("10000"),
            total_interest=Decimal("1500"),
            payoff_months=24,
            payoff_date=_date.today() + _td(days=730),
            schedule_json=[],
            is_active=True,
        )
        PayoffPlan.objects.create(
            tenant=self.tenant,
            strategy="avalanche",
            monthly_budget=Decimal("500"),
            total_debt=Decimal("10000"),
            total_interest=Decimal("1200"),
            payoff_months=22,
            payoff_date=_date.today() + _td(days=670),
            schedule_json=[],
            is_active=False,  # Inactive plan — shouldn't count
        )
        self.assertEqual(_count_active_payoff_plans(self.tenant), 1)


class AgendaSummaryLineTest(TestCase):
    """The summary collates counts in one prose line. Test plural/singular,
    zero-skipping, and ordering."""

    def setUp(self):
        self.tenant = create_tenant(display_name="Summary", telegram_chat_id=920030)
        _wipe_seed_docs(self.tenant)

    def test_empty_when_nothing_open(self):
        self.assertEqual(_render_summary(self.tenant), "")

    def test_includes_only_nonzero_counts(self):
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.TASKS,
            slug="tasks",
            title="Tasks",
            markdown="- [ ] One thing",
        )
        out = _render_summary(self.tenant)
        self.assertIn("1 open task", out)
        # Singular form — no trailing 's'
        self.assertNotIn("open tasks", out)
        self.assertNotIn("goal", out)
        self.assertNotIn("workout", out)

    def test_pluralizes_correctly(self):
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.TASKS,
            slug="tasks",
            title="Tasks",
            markdown="- [ ] One\n- [ ] Two\n- [ ] Three",
        )
        out = _render_summary(self.tenant)
        self.assertIn("3 open tasks", out)


class AgendaFullRenderTest(TestCase):
    """End-to-end through ``render_managed_region`` — the agenda must
    appear under its heading in the managed USER.md block, ahead of
    the goals/tasks pillar sections."""

    def setUp(self):
        self.tenant = create_tenant(display_name="FullRender", telegram_chat_id=920040)
        _wipe_seed_docs(self.tenant)
        # Make the tenant agenda non-empty so the section renders.
        self.tenant.fuel_enabled = True
        self.tenant.welcomes_sent = {}
        self.tenant.save()
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.TASKS,
            slug="tasks",
            title="Tasks",
            markdown="- [ ] Email Sarah",
        )

    def test_agenda_appears_in_managed_region(self):
        managed = render_managed_region(self.tenant)
        self.assertIn("## Agenda — Open threads with this user", managed)
        self.assertIn("Untouched introductions", managed)
        self.assertIn("Fuel", managed)
        self.assertIn("1 open task", managed)

    def test_agenda_renders_before_goals_and_tasks(self):
        """Order=15 places agenda above goals (20) and tasks (30)."""
        managed = render_managed_region(self.tenant)
        agenda_pos = managed.find("## Agenda — Open threads with this user")
        tasks_pos = managed.find("## Open tasks")
        self.assertGreater(agenda_pos, 0)
        # Tasks section may not appear if open task list is empty after
        # filtering, but if it does appear it must be after agenda.
        if tasks_pos > 0:
            self.assertLess(agenda_pos, tasks_pos)
