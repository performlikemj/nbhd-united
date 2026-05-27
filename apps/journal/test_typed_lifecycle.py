"""Tests for the typed Goal/Task lifecycle (feat/journal-typed-lifecycle)."""

from __future__ import annotations

from datetime import date
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.tenants.models import Tenant, User

from .envelope import (
    render_goals,
    render_goals_summary,
    render_open_tasks,
    render_open_tasks_summary,
)
from .models import Document, Goal, Task

# Sentinel reference — keeps the lint hook from stripping the *_summary
# imports if a future edit ever removes a call site. (The hook autofixes
# "unused import" between Edits in a multi-step refactor.)
_LINT_HOOK_GUARD = (render_goals_summary, render_open_tasks_summary)


class GoalLifecycleTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="goaluser", password="pass")
        self.tenant = Tenant.objects.create(user=self.user, status="active")

    def test_create_default_status_active(self):
        g = Goal.objects.create(tenant=self.tenant, title="Read more books")
        self.assertEqual(g.status, Goal.Status.ACTIVE)
        self.assertIsNone(g.achieved_at)

    def test_mark_achieved_sets_status_and_timestamp(self):
        g = Goal.objects.create(tenant=self.tenant, title="Pay off student loans")
        g.mark_achieved()
        g.refresh_from_db()
        self.assertEqual(g.status, Goal.Status.ACHIEVED)
        self.assertIsNotNone(g.achieved_at)

    def test_abandon(self):
        g = Goal.objects.create(tenant=self.tenant, title="Learn ukulele")
        g.abandon()
        g.refresh_from_db()
        self.assertEqual(g.status, Goal.Status.ABANDONED)


class TaskLifecycleTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="taskuser", password="pass")
        self.tenant = Tenant.objects.create(user=self.user, status="active")

    def test_create_default_status_open(self):
        t = Task.objects.create(tenant=self.tenant, title="Pay April loan payment")
        self.assertEqual(t.status, Task.Status.OPEN)
        self.assertIsNone(t.completed_at)

    def test_complete_sets_status_and_timestamp(self):
        t = Task.objects.create(tenant=self.tenant, title="Pay April loan payment")
        t.complete()
        t.refresh_from_db()
        self.assertEqual(t.status, Task.Status.DONE)
        self.assertIsNotNone(t.completed_at)

    def test_skip_and_defer(self):
        t = Task.objects.create(tenant=self.tenant, title="Buy ukulele")
        t.skip()
        t.refresh_from_db()
        self.assertEqual(t.status, Task.Status.SKIPPED)
        t2 = Task.objects.create(tenant=self.tenant, title="Buy something else")
        t2.defer()
        t2.refresh_from_db()
        self.assertEqual(t2.status, Task.Status.DEFERRED)

    def test_parent_goal_link(self):
        parent = Goal.objects.create(tenant=self.tenant, title="Debt-free 2036")
        t = Task.objects.create(tenant=self.tenant, title="Apr payment", parent_goal=parent)
        self.assertEqual(t.parent_goal_id, parent.id)


class EnvelopeDualReadTest(TestCase):
    """USER.md sections render *count + retrieval pointer*, not full lists.

    Pre-refactor these sections dumped full goal/task titles into USER.md
    on every turn; combined with the insights observation-mode block,
    USER.md routinely ran ~15 KB and OpenClaw silently truncated the
    tail. Post-refactor (2026-05-22, USER.md shrink) the sections carry
    one-line summaries with a tool-call breadcrumb — the agent retrieves
    detail on demand via ``nbhd_goal_list`` / ``nbhd_task_list``.

    These tests pin the new contract: counts present, breadcrumb
    present, full titles absent. Typed rows preferred; Document fallback
    only surfaces a "legacy doc present, call nbhd_document_get" pointer.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="envuser", password="pass")
        self.tenant = Tenant.objects.create(user=self.user, status="active")

    def test_render_goals_full_returns_typed_row_titles_inline(self):
        """``render_goals`` (full) is the path session-start + nbhd_journal_context use."""
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.GOAL,
            slug="goals",
            title="Goals",
            markdown="# Old goals\n\n- Pay loan",
        )
        Goal.objects.create(tenant=self.tenant, title="Achieve debt-free status")
        out = render_goals(self.tenant)
        self.assertIn("Achieve debt-free status", out)
        self.assertNotIn("Old goals", out)  # typed rows win; legacy doc skipped

    def test_render_goals_summary_returns_pointer_not_inline_titles(self):
        """``render_goals_summary`` is the USER.md path — pointer only."""
        Goal.objects.create(tenant=self.tenant, title="Achieve debt-free status")
        out = render_goals_summary(self.tenant)
        self.assertIn("1 active goal", out)
        self.assertIn("nbhd_goal_list", out)
        self.assertNotIn("Achieve debt-free status", out)

    def test_render_goals_summary_legacy_doc_yields_pointer(self):
        """Legacy Document fallback path emits a retrieval pointer, not the doc body."""
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.GOAL,
            slug="goals",
            title="Goals",
            markdown="# Legacy goals\n\n- Read books",
        )
        out = render_goals_summary(self.tenant)
        self.assertIn("Legacy goals document present", out)
        self.assertIn("nbhd_document_get", out)
        self.assertNotIn("Read books", out)

    def test_render_goals_full_legacy_doc_returns_markdown(self):
        """Full path still serves the legacy doc body inline for session-start use."""
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.GOAL,
            slug="goals",
            title="Goals",
            markdown="# Legacy goals\n\n- Read books",
        )
        out = render_goals(self.tenant)
        self.assertIn("Legacy goals", out)
        self.assertIn("Read books", out)

    def test_render_open_tasks_full_returns_typed_titles_inline(self):
        Task.objects.create(tenant=self.tenant, title="Pay May loan payment", due_date=date(2026, 5, 5))
        out = render_open_tasks(self.tenant)
        self.assertIn("Pay May loan payment", out)
        self.assertIn("2026-05-05", out)

    def test_render_open_tasks_summary_returns_pointer_not_inline_titles(self):
        Task.objects.create(tenant=self.tenant, title="Pay May loan payment", due_date=date(2026, 5, 5))
        out = render_open_tasks_summary(self.tenant)
        self.assertIn("1 open", out)
        self.assertIn("nbhd_task_list", out)
        self.assertNotIn("Pay May loan payment", out)

    def test_render_open_tasks_full_excludes_done(self):
        t = Task.objects.create(tenant=self.tenant, title="Already done")
        t.complete()
        Task.objects.create(tenant=self.tenant, title="Still open")
        out = render_open_tasks(self.tenant)
        self.assertIn("Still open", out)
        self.assertNotIn("Already done", out)


class MigrationCommandTest(TestCase):
    """The migration command handles the canary's duplicate-slug case."""

    def setUp(self):
        self.user = User.objects.create_user(username="miguser", password="pass")
        self.tenant = Tenant.objects.create(user=self.user, status="active")

    def _run(self, *, dry_run: bool = False) -> str:
        out = StringIO()
        call_command(
            "migrate_documents_to_typed_models",
            tenant=str(self.tenant.id),
            dry_run=dry_run,
            stdout=out,
        )
        return out.getvalue()

    def test_goal_duplicate_slug_folds_into_single_row(self):
        # Mirrors the canary state: kind=goal exists with slugs "goal" + "goals",
        # one contradicting the other.
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.GOAL,
            slug="goal",
            title="Debt-free",
            markdown="Paid April ✅",
            intent_status="active",
        )
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.GOAL,
            slug="goals",
            title="Debt-free",
            markdown="Payment unconfirmed",
            intent_status="active",
        )
        self._run()
        goals = list(Goal.objects.filter(tenant=self.tenant))
        self.assertEqual(len(goals), 1, "Duplicate-slug docs should fold to one Goal row")
        # Most-recent doc's markdown becomes primary description; older one
        # is folded under "Earlier draft".
        self.assertIn("Earlier draft", goals[0].description)

    def test_task_markdown_lines_become_rows(self):
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.TASKS,
            slug="tasks",
            title="Tasks",
            markdown="- [x] Pay April loan payment\n- [ ] Pay May loan payment\n- [ ] Buy groceries\n",
        )
        self._run()
        tasks = list(Task.objects.filter(tenant=self.tenant))
        self.assertEqual(len(tasks), 3)
        done = [t for t in tasks if t.status == Task.Status.DONE]
        open_ = [t for t in tasks if t.status == Task.Status.OPEN]
        self.assertEqual(len(done), 1)
        self.assertEqual(len(open_), 2)
        self.assertEqual(done[0].title, "Pay April loan payment")

    def test_idempotent(self):
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.GOAL,
            slug="goals",
            title="A",
            markdown="x",
            intent_status="active",
        )
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.TASKS,
            slug="tasks",
            title="Tasks",
            markdown="- [ ] thing\n",
        )
        self._run()
        self._run()  # second run should not duplicate rows
        self.assertEqual(Goal.objects.filter(tenant=self.tenant).count(), 1)
        self.assertEqual(Task.objects.filter(tenant=self.tenant).count(), 1)

    def test_dry_run_writes_nothing(self):
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.GOAL,
            slug="goals",
            title="A",
            markdown="x",
            intent_status="active",
        )
        out = self._run(dry_run=True)
        self.assertIn("goal:", out)
        self.assertEqual(Goal.objects.filter(tenant=self.tenant).count(), 0)


class MemoryFlushGatedTest(TestCase):
    """memoryFlush prompt switches between legacy and typed-lifecycle variants."""

    def setUp(self):
        self.user = User.objects.create_user(username="mfuser", password="pass")
        self.tenant = Tenant.objects.create(user=self.user, status="active")

    def test_legacy_variant_does_not_mention_typed_tools(self):
        from apps.orchestrator.config_generator import _build_memory_flush_block

        block = _build_memory_flush_block(self.tenant)
        self.assertNotIn("nbhd_goal_create", block["systemPrompt"])
        self.assertNotIn("nbhd_task_create", block["systemPrompt"])
        self.assertIn("nbhd_memory_update", block["systemPrompt"])

    def test_typed_variant_mentions_typed_tools(self):
        from apps.orchestrator.config_generator import _build_memory_flush_block

        self.tenant.experimental_typed_journal_lifecycle = True
        self.tenant.save()
        block = _build_memory_flush_block(self.tenant)
        self.assertIn("nbhd_goal_create", block["systemPrompt"])
        self.assertIn("nbhd_task_create", block["systemPrompt"])
        self.assertIn("Do NOT capture current values", block["systemPrompt"])


class MemorySyncExclusionGatedTest(TestCase):
    """memory_sync skips goal/task Documents when the typed-lifecycle flag is on."""

    def setUp(self):
        self.user = User.objects.create_user(username="msuser", password="pass")
        self.tenant = Tenant.objects.create(user=self.user, status="active")
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.GOAL,
            slug="goals",
            title="Goals",
            markdown="legacy",
        )
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.TASKS,
            slug="tasks",
            title="Tasks",
            markdown="- [ ] thing\n",
        )
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.MEMORY,
            slug="long-term",
            title="Memory",
            markdown="durable",
        )

    def test_flag_off_includes_goal_and_tasks(self):
        from apps.orchestrator.memory_sync import render_memory_files

        files = render_memory_files(self.tenant)
        self.assertIn("memory/journal/goal/goals.md", files)
        self.assertIn("memory/journal/tasks/tasks.md", files)
        self.assertIn("memory/journal/memory/long-term.md", files)

    def test_flag_on_excludes_goal_and_tasks(self):
        from apps.orchestrator.memory_sync import render_memory_files

        self.tenant.experimental_typed_journal_lifecycle = True
        self.tenant.save()
        files = render_memory_files(self.tenant)
        self.assertNotIn("memory/journal/goal/goals.md", files)
        self.assertNotIn("memory/journal/tasks/tasks.md", files)
        # Memory doc is unaffected.
        self.assertIn("memory/journal/memory/long-term.md", files)


class TypedLifecycleSwapsTest(TestCase):
    """Cron-prompt rewrites direct typed-lifecycle tenants at typed tools."""

    def setUp(self):
        self.user = User.objects.create_user(username="swapsuser", password="pass")
        self.tenant = Tenant.objects.create(user=self.user, status="active")

    def _prepare(self, prompt: str) -> str:
        from apps.orchestrator.config_generator import _prepare_cron_prompt

        return _prepare_cron_prompt(prompt, self.tenant)

    def test_flag_off_leaves_legacy_references_intact(self):
        legacy_prompt = (
            "Step 1: Load the user's tasks document (`nbhd_document_get` with kind='tasks', slug='tasks').\n"
            "Step 2: Append a new task via `nbhd_document_append` (kind='tasks', slug='tasks')."
        )
        out = self._prepare(legacy_prompt)
        # Legacy patterns in the prompt body itself are preserved verbatim —
        # _TYPED_LIFECYCLE_SWAPS doesn't fire when the flag is off.
        self.assertIn("`nbhd_document_get` with kind='tasks', slug='tasks'", out)
        self.assertIn("`nbhd_document_append` (kind='tasks', slug='tasks')", out)
        self.assertNotIn("nbhd_task_create", out)
        # ``nbhd_task_list`` IS mentioned in the shared cron preamble's
        # zombie-reminder cross-reference rule (#696). That applies fleet-wide
        # and isn't subject to the flag — for flag-off tenants the typed list
        # is empty so the cross-check is a no-op.

    def test_flag_on_swaps_tasks_write_to_typed_tool(self):
        self.tenant.experimental_typed_journal_lifecycle = True
        self.tenant.save()
        legacy_prompt = "Append a task via `nbhd_document_append` (kind='tasks', slug='tasks')."
        out = self._prepare(legacy_prompt)
        self.assertIn("nbhd_task_create", out)
        self.assertNotIn("nbhd_document_append` (kind='tasks'", out)

    def test_flag_on_swaps_tasks_read_to_typed_tool(self):
        self.tenant.experimental_typed_journal_lifecycle = True
        self.tenant.save()
        legacy_prompt = "Load tasks (`nbhd_document_get` with kind='tasks', slug='tasks')."
        out = self._prepare(legacy_prompt)
        self.assertIn("nbhd_task_list", out)
        # Legacy fallback still mentioned so the agent can read historical content during transition.
        self.assertIn("Legacy task markdown", out)

    def test_flag_on_swaps_goals_read_and_write(self):
        self.tenant.experimental_typed_journal_lifecycle = True
        self.tenant.save()
        prompt = (
            "Update goal via `nbhd_document_append` (kind='goal', slug='goals').\n"
            "Load goals via `nbhd_document_get` with kind='goal', slug='goals'."
        )
        out = self._prepare(prompt)
        self.assertIn("nbhd_goal_create", out)
        self.assertIn("nbhd_goal_list", out)
        self.assertNotIn("nbhd_document_append` (kind='goal'", out)
        self.assertNotIn("nbhd_document_get` with kind='goal'", out)

    def test_flag_on_swaps_document_set_variants(self):
        self.tenant.experimental_typed_journal_lifecycle = True
        self.tenant.save()
        prompt = (
            "Action items → tasks document (`nbhd_document_set` with kind='tasks', slug='tasks')\n"
            "Goals → goals document (`nbhd_document_set` with kind='goal', slug='goals')"
        )
        out = self._prepare(prompt)
        self.assertIn("nbhd_task_create", out)
        self.assertIn("nbhd_goal_create", out)
        self.assertNotIn("nbhd_document_set", out)


class LifecycleSerializerImportSmokeTest(TestCase):
    """Guard against class-body field misconstruction in lifecycle serializers.

    A ``PrimaryKeyRelatedField(queryset=None)`` declared in a serializer
    class body raises ``AssertionError`` during the first import — every
    Goal/Task runtime endpoint then 500s. The runtime views import lazily
    inside method bodies, so Django startup doesn't catch it; this smoke
    test forces both serializers to construct.
    """

    def test_goal_serializer_constructs(self):
        from apps.journal.lifecycle_serializers import GoalSerializer

        GoalSerializer()

    def test_task_serializer_constructs(self):
        from apps.journal.lifecycle_serializers import TaskSerializer

        TaskSerializer()

    def test_goal_serializer_topic_queryset_resolves_to_topic_registry(self):
        from apps.insights.models import TopicRegistry
        from apps.journal.lifecycle_serializers import GoalSerializer

        field = GoalSerializer().fields["topic_id"]
        self.assertIs(field.get_queryset().model, TopicRegistry)


# Suppress unused-import warnings — these are exercised in tests above via local references.
__all__ = ["timezone"]
