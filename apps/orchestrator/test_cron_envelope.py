"""Tests for the workspace envelope and the slimmed-down cron prompt builder.

The envelope used to be baked into every cron message via
``_build_context_envelope`` in ``config_generator``. Phase 2.5 moved it into
``apps/orchestrator/workspace_envelope.py``, which renders the data into
``workspace/USER.md`` — auto-loaded by OpenClaw on every agent turn — and
delegates write-back to the file share with a leading-edge debounce.

Coverage here:

* State fetchers: ``envelope_goals``, ``envelope_open_tasks``,
  ``envelope_recent_lessons`` (former private helpers, promoted to public).
* ``render_profile_section`` (new — Profile block from ``tenant.user`` fields).
* ``render_managed_region`` (replaces ``_build_context_envelope``).
* ``merge_into_user_md`` — three-case algorithm + idempotency + round-trip.
* ``push_user_md`` — debounce, force, mocked file-share calls.
* ``_prepare_cron_prompt`` / ``_build_cron_message`` — envelope is no longer
  in the cron message body; verify date line + preamble + prompt only.
"""

from __future__ import annotations

from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings

from apps.journal.models import Document
from apps.journal.services import STARTER_DOCUMENT_TEMPLATES
from apps.lessons.models import Lesson
from apps.orchestrator.config_generator import (
    _CRON_CONTEXT_PREAMBLE,
    _build_cron_message,
    _prepare_cron_prompt,
)
from apps.orchestrator.workspace_envelope import (
    BEGIN_MARKER,
    END_MARKER,
    envelope_goals,
    envelope_open_tasks,
    envelope_recent_lessons,
    merge_into_user_md,
    push_user_md,
    render_managed_region,
    render_profile_section,
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


# ─── State fetchers ────────────────────────────────────────────────────────


class EnvelopeGoalsTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="EnvGoals", telegram_chat_id=910001)
        _clear_seed_docs(self.tenant)

    def test_returns_empty_when_no_goals_doc(self):
        self.assertEqual(envelope_goals(self.tenant), "")

    def test_returns_empty_when_goals_doc_blank(self):
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.GOAL,
            slug="goals",
            title="Goals",
            markdown="   \n  \n",
        )
        self.assertEqual(envelope_goals(self.tenant), "")

    def test_returns_full_markdown_under_cap(self):
        md = "## Active\n- Ship the envelope\n- Run the canary"
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.GOAL,
            slug="goals",
            title="Goals",
            markdown=md,
        )
        self.assertEqual(envelope_goals(self.tenant), md)

    def test_truncates_when_over_cap(self):
        md = "x" * 2000
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.GOAL,
            slug="goals",
            title="Goals",
            markdown=md,
        )
        out = envelope_goals(self.tenant, max_chars=1500)
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
        self.assertEqual(envelope_goals(self.tenant), "")

    def test_skips_unmodified_starter_seed(self):
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.GOAL,
            slug="goals",
            title="Goals",
            markdown=_starter_md("goals"),
        )
        self.assertEqual(envelope_goals(self.tenant), "")

    def test_treats_user_addition_as_real_content(self):
        custom = _starter_md("goals") + "\n- Train for half-marathon\n"
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.GOAL,
            slug="goals",
            title="Goals",
            markdown=custom,
        )
        out = envelope_goals(self.tenant)
        self.assertIn("Train for half-marathon", out)


class EnvelopeOpenTasksTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="EnvTasks", telegram_chat_id=910010)
        _clear_seed_docs(self.tenant)

    def test_returns_empty_when_no_tasks_doc(self):
        self.assertEqual(envelope_open_tasks(self.tenant), "")

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
        out = envelope_open_tasks(self.tenant)
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
        self.assertEqual(envelope_open_tasks(self.tenant), "")

    def test_caps_at_max_items_with_overflow_hint(self):
        lines = "\n".join(f"- [ ] Task {i}" for i in range(40))
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.TASKS,
            slug="tasks",
            title="Tasks",
            markdown=lines,
        )
        out = envelope_open_tasks(self.tenant, max_items=10)
        self.assertEqual(out.count("- [ ] Task"), 10)
        self.assertIn("+30 more open tasks", out)

    def test_skips_starter_placeholder_tasks(self):
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.TASKS,
            slug="tasks",
            title="Tasks",
            markdown=_starter_md("tasks"),
        )
        self.assertEqual(envelope_open_tasks(self.tenant), "")

    def test_keeps_real_tasks_alongside_starter(self):
        md = _starter_md("tasks") + "\n- [ ] Real task the user added\n"
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.TASKS,
            slug="tasks",
            title="Tasks",
            markdown=md,
        )
        out = envelope_open_tasks(self.tenant)
        self.assertIn("Real task the user added", out)


class EnvelopeRecentLessonsTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="EnvLessons", telegram_chat_id=910020)

    def test_returns_empty_when_none(self):
        self.assertEqual(envelope_recent_lessons(self.tenant), "")

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
        out = envelope_recent_lessons(self.tenant)
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
        out = envelope_recent_lessons(self.tenant, limit=3)
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
        out = envelope_recent_lessons(self.tenant)
        self.assertIn("line one of a multi-line lesson", out)
        self.assertNotIn("line two", out)


# ─── Profile section ───────────────────────────────────────────────────────


class RenderProfileSectionTest(TestCase):
    def test_omits_block_when_no_meaningful_fields(self):
        # Default tenant: display_name="Friend", timezone="UTC", language="en",
        # no city. None of those should produce a Profile block.
        tenant = create_tenant(display_name="Friend", telegram_chat_id=910100)
        self.assertEqual(render_profile_section(tenant), "")

    def test_includes_set_fields_only(self):
        tenant = create_tenant(display_name="Mike", telegram_chat_id=910101)
        tenant.user.timezone = "Asia/Tokyo"
        tenant.user.preferred_channel = "line"
        tenant.user.location_city = "Osaka"
        tenant.user.save()

        out = render_profile_section(tenant)
        self.assertIn("## Profile", out)
        self.assertIn("Display name: Mike", out)
        self.assertIn("Timezone: Asia/Tokyo", out)
        self.assertIn("Preferred channel: line", out)
        self.assertIn("Location: Osaka", out)
        # Default language not included
        self.assertNotIn("Language:", out)


# ─── Managed region ────────────────────────────────────────────────────────


class RenderManagedRegionTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="EnvBuild", telegram_chat_id=910030)
        _clear_seed_docs(self.tenant)

    def test_always_includes_markers_even_when_state_empty(self):
        out = render_managed_region(self.tenant)
        self.assertIn(BEGIN_MARKER, out)
        self.assertIn(END_MARKER, out)
        self.assertIn("Last refreshed:", out)
        # Friendly placeholder when no real state
        self.assertIn("No active goals, open tasks, or recent lessons", out)

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
        out = render_managed_region(self.tenant)
        self.assertIn(BEGIN_MARKER, out)
        self.assertIn("# Pre-loaded user state", out)
        self.assertIn("## Active goals", out)
        self.assertIn("Ship envelope to canary", out)
        self.assertIn("## Open tasks", out)
        self.assertIn("- [ ] Run unit tests", out)
        self.assertNotIn("Read hook site", out)  # closed task excluded
        self.assertIn("## Recent lessons", out)
        self.assertIn("Pre-fetched state beats agent tool reliance", out)
        self.assertIn(END_MARKER, out)

    def test_skips_missing_sections_but_keeps_present_ones(self):
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.TASKS,
            slug="tasks",
            title="Tasks",
            markdown="- [ ] Solo task",
        )
        out = render_managed_region(self.tenant)
        self.assertIn("## Open tasks", out)
        self.assertIn("Solo task", out)
        self.assertNotIn("## Active goals", out)
        self.assertNotIn("## Recent lessons", out)


# ─── Merge algorithm ───────────────────────────────────────────────────────


class MergeIntoUserMdTest(TestCase):
    def setUp(self):
        # A simple managed block is enough for merge tests — we don't need
        # real envelope rendering here.
        self.managed = (
            f"{BEGIN_MARKER}\n"
            "\n"
            "# Pre-loaded user state\n"
            "\n"
            "_Last refreshed: 2026-05-07T00:00:00+00:00_\n"
            "\n"
            "_(No active goals, open tasks, or recent lessons yet.)_\n"
            "\n"
            f"{END_MARKER}\n"
        )

    def test_case1_empty_returns_managed_alone(self):
        self.assertEqual(merge_into_user_md(None, self.managed), self.managed)
        self.assertEqual(merge_into_user_md("", self.managed), self.managed)
        self.assertEqual(merge_into_user_md("   \n\n  ", self.managed), self.managed)

    def test_case1_openclaw_boilerplate_treated_as_empty(self):
        from apps.orchestrator.workspace_envelope import _OPENCLAW_DEFAULT_USER_MD

        merged = merge_into_user_md(_OPENCLAW_DEFAULT_USER_MD, self.managed)
        self.assertEqual(merged, self.managed)

    def test_case2_replaces_existing_managed_region(self):
        existing = self.managed + "\n## Agent's notes\n\n- Mike prefers concise replies.\n"
        new_managed = self.managed.replace("No active goals", "Has stuff now")
        merged = merge_into_user_md(existing, new_managed)
        # New managed content present
        self.assertIn("Has stuff now", merged)
        # Stale managed content gone
        self.assertNotIn("No active goals", merged)
        # Agent's notes preserved
        self.assertIn("Agent's notes", merged)
        self.assertIn("Mike prefers concise replies", merged)

    def test_case3_first_migration_prepends_with_markers(self):
        agent_only = "## Agent's notes\n\n- Mike likes to plan workouts on weekends.\n"
        merged = merge_into_user_md(agent_only, self.managed)
        # Managed block at the top
        self.assertTrue(merged.startswith(BEGIN_MARKER))
        # Managed END marker present
        self.assertIn(END_MARKER, merged)
        # Agent's content preserved (verbatim) below the managed block
        self.assertIn("Agent's notes", merged)
        self.assertIn("Mike likes to plan workouts", merged)
        # Sentinel order: managed END comes before the agent content
        self.assertLess(merged.index(END_MARKER), merged.index("Agent's notes"))

    def test_idempotent(self):
        # Applying the same merge twice should produce the same output.
        agent_only = "## Agent's notes\n\nFoo.\n"
        once = merge_into_user_md(agent_only, self.managed)
        twice = merge_into_user_md(once, self.managed)
        self.assertEqual(once, twice)

    def test_round_trip_after_managed_refresh(self):
        # Initial: agent has content, no markers
        existing = "## Agent's notes\n\nfirst observation\n"

        # First migration: prepends managed
        v1 = merge_into_user_md(existing, self.managed)
        # Second refresh: replaces only the managed region
        new_managed = self.managed.replace(
            "_Last refreshed: 2026-05-07T00:00:00+00:00_",
            "_Last refreshed: 2026-05-07T01:00:00+00:00_",
        )
        v2 = merge_into_user_md(v1, new_managed)

        # Agent content survives the second refresh
        self.assertIn("first observation", v2)
        # Managed region updated
        self.assertIn("01:00:00", v2)
        self.assertNotIn("00:00:00", v2)


# ─── push_user_md ──────────────────────────────────────────────────────────


class PushUserMdTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="EnvPush", telegram_chat_id=910200)
        cache.clear()

    def tearDown(self):
        cache.clear()

    @mock.patch("apps.orchestrator.workspace_envelope.upload_workspace_file")
    @mock.patch("apps.orchestrator.workspace_envelope.download_workspace_file")
    def test_writes_managed_region_when_file_missing(self, mock_download, mock_upload):
        mock_download.return_value = None
        result = push_user_md(self.tenant, force=True)
        self.assertTrue(result)
        mock_upload.assert_called_once()
        args, _kwargs = mock_upload.call_args
        self.assertEqual(args[0], str(self.tenant.id))
        self.assertEqual(args[1], "workspace/USER.md")
        body = args[2]
        self.assertIn(BEGIN_MARKER, body)
        self.assertIn(END_MARKER, body)

    @mock.patch("apps.orchestrator.workspace_envelope.upload_workspace_file")
    @mock.patch("apps.orchestrator.workspace_envelope.download_workspace_file")
    def test_debounces_within_window(self, mock_download, mock_upload):
        mock_download.return_value = None
        # First call writes
        self.assertTrue(push_user_md(self.tenant, debounce_seconds=60))
        # Second call within window is dropped
        self.assertFalse(push_user_md(self.tenant, debounce_seconds=60))
        self.assertEqual(mock_upload.call_count, 1)

    @mock.patch("apps.orchestrator.workspace_envelope.upload_workspace_file")
    @mock.patch("apps.orchestrator.workspace_envelope.download_workspace_file")
    def test_force_bypasses_debounce(self, mock_download, mock_upload):
        mock_download.return_value = None
        push_user_md(self.tenant, debounce_seconds=60)
        push_user_md(self.tenant, debounce_seconds=60, force=True)
        self.assertEqual(mock_upload.call_count, 2)

    @mock.patch("apps.orchestrator.workspace_envelope.upload_workspace_file")
    @mock.patch("apps.orchestrator.workspace_envelope.download_workspace_file")
    def test_preserves_agent_content_on_existing_user_md(self, mock_download, mock_upload):
        agent_authored = "## Agent's notes\n\nfondly remembers fishing trip\n"
        mock_download.return_value = agent_authored

        push_user_md(self.tenant, force=True)
        args, _ = mock_upload.call_args
        body = args[2]
        self.assertIn("fondly remembers fishing trip", body)
        self.assertIn(BEGIN_MARKER, body)


# ─── Cron prompt builder — envelope is GONE ────────────────────────────────


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

    def test_does_not_inject_envelope_into_message(self):
        # Phase 2.5: envelope content lives in workspace/USER.md, not in the
        # cron message body.
        out = _prepare_cron_prompt("BODY", self.tenant)
        self.assertIn("Current date and time:", out)
        self.assertIn(_CRON_CONTEXT_PREAMBLE, out)
        self.assertNotIn("Pre-loaded user state", out)
        self.assertNotIn("Ship the canary", out)
        self.assertTrue(out.endswith("BODY"))

    def test_message_still_starts_with_date_line_so_default_prefix_match_holds(self):
        out = _prepare_cron_prompt("BODY", self.tenant)
        self.assertTrue(out.startswith("Current date and time:"))

    def test_structural_order_date_then_preamble_then_body(self):
        out = _prepare_cron_prompt("THE_BODY", self.tenant)
        idx_date = out.index("Current date and time:")
        idx_preamble = out.index("MANDATORY")
        idx_body = out.index("THE_BODY")
        self.assertLess(idx_date, idx_preamble)
        self.assertLess(idx_preamble, idx_body)


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
        self.assertIn("FINAL STEP — conditional sync to the main session", out)

    def test_background_omits_phase2_block(self):
        out = _build_cron_message("BODY", "TestJob", foreground=False, tenant=self.tenant)
        self.assertNotIn("FINAL STEP — conditional sync to the main session", out)

    def test_message_does_not_include_envelope(self):
        out = _build_cron_message("BODY", "TestJob", foreground=True, tenant=self.tenant)
        self.assertNotIn("Pre-loaded user state", out)
        self.assertNotIn("Goal A", out)


# ─── audit_user_md classifier ──────────────────────────────────────────────


class AuditClassifierTest(TestCase):
    """Quick coverage of the audit command's classification helper.

    The helper lives in the management command module and is intentionally
    pure — testing it directly is much cheaper than spinning up the full
    command output.
    """

    def test_classifications(self):
        from apps.orchestrator.management.commands.audit_user_md import _classify
        from apps.orchestrator.workspace_envelope import _OPENCLAW_DEFAULT_USER_MD

        self.assertEqual(_classify(None), "missing")
        self.assertEqual(_classify(""), "empty")
        self.assertEqual(_classify("   \n\n"), "empty")
        self.assertEqual(_classify(_OPENCLAW_DEFAULT_USER_MD), "boilerplate")
        self.assertEqual(_classify(f"{BEGIN_MARKER}\nstuff\n{END_MARKER}\n"), "managed")
        self.assertEqual(_classify("## Agent notes\n- foo"), "agent")


@override_settings(NBHD_DISABLE_BACKGROUND_THREADS=True)
class PushUserMdInBackgroundTest(TestCase):
    """When NBHD_DISABLE_BACKGROUND_THREADS is set, the helper runs synchronously."""

    def setUp(self):
        cache.clear()
        self.tenant = create_tenant(display_name="EnvBg", telegram_chat_id=910300)

    def tearDown(self):
        cache.clear()

    @mock.patch("apps.orchestrator.workspace_envelope.upload_workspace_file")
    @mock.patch("apps.orchestrator.workspace_envelope.download_workspace_file")
    def test_synchronous_when_disabled(self, mock_download, mock_upload):
        from apps.orchestrator.workspace_envelope import push_user_md_in_background

        mock_download.return_value = None
        push_user_md_in_background(self.tenant)
        mock_upload.assert_called_once()
