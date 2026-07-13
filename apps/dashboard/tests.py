"""Dashboard view tests — Horizons Weekly Pulse helpers + HorizonsView Phase 2 extension."""

from __future__ import annotations

from datetime import date, timedelta

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.dashboard.views import _clean_markdown_preview, _derive_week_bounds
from apps.insights.models import AssistantInsight, TopicRegistry
from apps.insights.pillars import Pillar
from apps.journal.models import Document, Goal
from apps.tenants.services import create_tenant


class CleanMarkdownPreviewTests(SimpleTestCase):
    def test_strips_headings_bold_and_lists(self):
        md = (
            "# Weekly Review — 2026-W14\n"
            "*Week of April 6–12, 2026*\n\n"
            "## 🏆 Wins\n"
            "- Shipped **billing** fix\n"
            "- Landed *reflection* gate\n"
        )
        out = _clean_markdown_preview(md)
        self.assertNotIn("#", out)
        self.assertNotIn("**", out)
        self.assertNotIn("- ", out)
        self.assertIn("Shipped billing fix", out)
        self.assertIn("Landed reflection gate", out)

    def test_drops_links_keeps_visible_text(self):
        md = "See [the doc](https://example.com/thing) for details."
        self.assertEqual(
            _clean_markdown_preview(md),
            "See the doc for details.",
        )

    def test_strips_inline_code_and_blockquotes(self):
        md = "> quoted line\n`inline` snippet here"
        out = _clean_markdown_preview(md)
        self.assertEqual(out, "quoted line inline snippet here")

    def test_empty_input_returns_empty(self):
        self.assertEqual(_clean_markdown_preview(""), "")
        self.assertEqual(_clean_markdown_preview(None), "")  # type: ignore[arg-type]

    def test_truncates_with_ellipsis(self):
        md = "a " * 200
        out = _clean_markdown_preview(md, max_chars=50)
        self.assertLessEqual(len(out), 50)
        self.assertTrue(out.endswith("\u2026"))

    def test_preserves_underscore_in_identifiers(self):
        # A bare `some_var` should not be treated as italic
        md = "Look at some_var and another_name in the logs."
        out = _clean_markdown_preview(md)
        self.assertIn("some_var", out)
        self.assertIn("another_name", out)


class DeriveWeekBoundsTests(SimpleTestCase):
    def test_parses_iso_monday_slug(self):
        start, end = _derive_week_bounds("2026-04-06", date(2026, 4, 13))
        self.assertEqual(start, date(2026, 4, 6))
        self.assertEqual(end, date(2026, 4, 12))

    def test_falls_back_to_week_of_fallback(self):
        # Wednesday 2026-04-15 → Monday is 2026-04-13
        start, end = _derive_week_bounds("not-a-date", date(2026, 4, 15))
        self.assertEqual(start, date(2026, 4, 13))
        self.assertEqual(end, date(2026, 4, 19))

    def test_invalid_month_falls_back(self):
        start, _ = _derive_week_bounds("2026-13-01", date(2026, 4, 20))
        self.assertEqual(start, date(2026, 4, 20))  # Monday


@override_settings(NBHD_DISABLE_BACKGROUND_THREADS=True)
class HorizonsViewAssistantInsightsTests(TestCase):
    """Phase 2 extension: assistant_insights surfaced in /api/v1/dashboard/horizons/.

    Threads disabled so each AssistantInsight / Document save runs its
    envelope-refresh receiver synchronously inside on_commit. Daemon
    threads opening their own Postgres connection leak past test-DB
    teardown — caught when Day 2's higher-volume tests started failing
    CI on the post-merge run.
    """

    def setUp(self):
        self.tenant = create_tenant(display_name="Horizons-P2", telegram_chat_id=900700)
        self.other_tenant = create_tenant(display_name="Horizons-Other", telegram_chat_id=900701)
        self.client = APIClient()
        token = RefreshToken.for_user(self.tenant.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")
        # "debt" is pre-seeded by insights migration 0002.
        self.topic, _ = TopicRegistry.objects.get_or_create(
            pillar=Pillar.GRAVITY.value,
            slug="debt",
            defaults={
                "display_name": "Debt",
                "status": TopicRegistry.Status.CANONICAL,
                "source": TopicRegistry.Source.SEED,
            },
        )

    def _make(self, *, tenant=None, statement="An observation.", status=AssistantInsight.Status.OPEN):
        return AssistantInsight.objects.create(
            tenant=tenant or self.tenant,
            pillar=Pillar.GRAVITY.value,
            topic=self.topic,
            statement=statement,
            status=status,
        )

    def test_horizons_includes_assistant_insights_field(self):
        resp = self.client.get("/api/v1/dashboard/horizons/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("assistant_insights", resp.json())
        self.assertEqual(resp.json()["assistant_insights"], [])

    def test_open_and_confirmed_surface_refuted_hidden(self):
        self._make(statement="Open one")
        self._make(statement="Confirmed one", status=AssistantInsight.Status.CONFIRMED)
        self._make(statement="Refuted one", status=AssistantInsight.Status.REFUTED)
        resp = self.client.get("/api/v1/dashboard/horizons/")
        statements = {i["statement"] for i in resp.json()["assistant_insights"]}
        self.assertIn("Open one", statements)
        self.assertIn("Confirmed one", statements)
        self.assertNotIn("Refuted one", statements)

    def test_ordered_newest_first(self):
        first = self._make(statement="Older")
        second = self._make(statement="Newer")
        # Force-stamp first one to be older
        AssistantInsight.objects.filter(id=first.id).update(created_at=second.created_at.replace(year=2025))
        resp = self.client.get("/api/v1/dashboard/horizons/")
        statements = [i["statement"] for i in resp.json()["assistant_insights"]]
        self.assertEqual(statements[0], "Newer")
        self.assertEqual(statements[1], "Older")

    def test_capped_at_twenty(self):
        for i in range(25):
            self._make(statement=f"Insight {i}")
        resp = self.client.get("/api/v1/dashboard/horizons/")
        self.assertLessEqual(len(resp.json()["assistant_insights"]), 20)

    def test_isolates_other_tenant_insights(self):
        self._make(tenant=self.other_tenant, statement="Other tenant private")
        resp = self.client.get("/api/v1/dashboard/horizons/")
        statements = {i["statement"] for i in resp.json()["assistant_insights"]}
        self.assertNotIn("Other tenant private", statements)

    def test_payload_shape(self):
        ins = self._make(statement="Shape check", status=AssistantInsight.Status.CONFIRMED)
        resp = self.client.get("/api/v1/dashboard/horizons/")
        row = next(i for i in resp.json()["assistant_insights"] if i["id"] == str(ins.id))
        self.assertEqual(row["pillar"], Pillar.GRAVITY.value)
        self.assertEqual(row["topic_slug"], "debt")
        self.assertEqual(row["topic_display_name"], "Debt")
        self.assertEqual(row["status"], "confirmed")
        self.assertIn("confidence", row)
        self.assertIn("created_at", row)


@override_settings(NBHD_DISABLE_BACKGROUND_THREADS=True)
class HorizonsViewGoalsDualReadTests(TestCase):
    """Dual-read: HorizonsView 'goals' section returns typed Goal rows
    unioned with legacy Document(kind=GOAL) rows, deduped via
    Goal.migrated_from_document.

    Threads disabled to prevent daemon-thread connection leaks from
    envelope-refresh receivers on Document/Goal saves.
    """

    def setUp(self):
        self.tenant = create_tenant(display_name="Horizons-Goals", telegram_chat_id=900710)
        self.client = APIClient()
        token = RefreshToken.for_user(self.tenant.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")

    def _titles(self):
        resp = self.client.get("/api/v1/dashboard/horizons/")
        self.assertEqual(resp.status_code, 200)
        return [g["title"] for g in resp.json()["goals"]]

    def test_typed_goals_appear_in_goals_list(self):
        Goal.objects.create(tenant=self.tenant, title="Typed only goal", status=Goal.Status.ACTIVE)
        self.assertIn("Typed only goal", self._titles())

    def test_legacy_documents_still_appear_when_no_typed_goals(self):
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.GOAL,
            slug="legacy-goal",
            title="Legacy doc goal",
            markdown="some content",
        )
        self.assertIn("Legacy doc goal", self._titles())

    def test_migrated_document_is_deduped(self):
        legacy = Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.GOAL,
            slug="orig",
            title="Old prose goal",
            markdown="x",
        )
        Goal.objects.create(
            tenant=self.tenant,
            title="Migrated typed goal",
            status=Goal.Status.ACTIVE,
            migrated_from_document=legacy,
        )
        titles = self._titles()
        self.assertIn("Migrated typed goal", titles)
        self.assertNotIn("Old prose goal", titles)

    def test_inactive_typed_goals_excluded(self):
        Goal.objects.create(tenant=self.tenant, title="Achieved goal", status=Goal.Status.ACHIEVED)
        Goal.objects.create(tenant=self.tenant, title="Abandoned goal", status=Goal.Status.ABANDONED)
        titles = self._titles()
        self.assertNotIn("Achieved goal", titles)
        self.assertNotIn("Abandoned goal", titles)

    def test_mixed_typed_and_unrelated_legacy_coexist(self):
        Goal.objects.create(tenant=self.tenant, title="Typed goal A", status=Goal.Status.ACTIVE)
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.GOAL,
            slug="unrelated-legacy",
            title="Unmigrated legacy goal",
            markdown="x",
        )
        titles = self._titles()
        self.assertIn("Typed goal A", titles)
        self.assertIn("Unmigrated legacy goal", titles)

    def test_folded_nonprimary_docs_excluded(self):
        # The migration folds goal/goal.md + goal/goals.md (same goal_dedup_key)
        # into one typed Goal but marks ONLY the primary via
        # migrated_from_document. The non-primary sibling must not leak through as
        # a second raw card. (Clear the seeded (goal, goals) doc for a precise
        # fixture — unique_together forbids a second one.)
        Document.objects.filter(tenant=self.tenant, kind=Document.Kind.GOAL).delete()
        primary = Document.objects.create(
            tenant=self.tenant, kind=Document.Kind.GOAL, slug="goals", title="Goals", markdown="primary content"
        )
        Document.objects.create(
            tenant=self.tenant, kind=Document.Kind.GOAL, slug="goal", title="Goals", markdown="earlier draft"
        )
        Goal.objects.create(
            tenant=self.tenant, title="Goals", status=Goal.Status.ACTIVE, migrated_from_document=primary
        )
        # Only the single typed Goal renders — neither legacy "Goals" doc leaks.
        self.assertEqual(self._titles().count("Goals"), 1)

    def test_folded_nonprimary_docs_excluded_for_nonactive_goal(self):
        # The fold hides leftover legacy docs even when their typed Goal is not
        # ACTIVE (achieved goals drop from the active list, but their folded
        # legacy siblings must not reappear as raw cards).
        Document.objects.filter(tenant=self.tenant, kind=Document.Kind.GOAL).delete()
        primary = Document.objects.create(
            tenant=self.tenant, kind=Document.Kind.GOAL, slug="goals", title="Run a marathon", markdown="primary"
        )
        Document.objects.create(
            tenant=self.tenant, kind=Document.Kind.GOAL, slug="goal", title="Run a marathon", markdown="draft"
        )
        Goal.objects.create(
            tenant=self.tenant, title="Run a marathon", status=Goal.Status.ACHIEVED, migrated_from_document=primary
        )
        self.assertNotIn("Run a marathon", self._titles())

    def test_duplicate_legacy_containers_collapse_without_migration(self):
        # Prod reality (fleet typed_migrated=0): a tenant accumulates several
        # aggregate "Goals" container docs at different slugs, all titled "Goals"
        # (one goal_dedup_key), with NO migration run. They must collapse to a
        # single, newest card — not render as several raw "Goals" cards.
        Document.objects.filter(tenant=self.tenant, kind=Document.Kind.GOAL).delete()
        older = Document.objects.create(
            tenant=self.tenant, kind=Document.Kind.GOAL, slug="goal", title="Goals", markdown="older real content"
        )
        newer = Document.objects.create(
            tenant=self.tenant, kind=Document.Kind.GOAL, slug="goals", title="Goals", markdown="newer stub"
        )
        # Deterministic recency (auto_now sets both to ~now at create time).
        Document.objects.filter(pk=older.pk).update(updated_at=timezone.now() - timedelta(days=2))
        Document.objects.filter(pk=newer.pk).update(updated_at=timezone.now())
        resp = self.client.get("/api/v1/dashboard/horizons/")
        goals = [g for g in resp.json()["goals"] if g["title"] == "Goals"]
        self.assertEqual(len(goals), 1)
        self.assertEqual(goals[0]["slug"], "goals")  # newest representative kept

    def test_goal_preview_is_cleaned_not_raw_markdown(self):
        # Regression: the goals card 'preview' used to be raw markdown[:200], so
        # headings/blockquotes/bold rendered literally on the iOS card. It must
        # now be prose-only (clean THEN truncate).
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.GOAL,
            slug="legacy-goal",
            title="My goal",
            markdown="# Goals\n\n## Active Goals\n\n> **Note**: ship the thing by Q3.",
        )
        resp = self.client.get("/api/v1/dashboard/horizons/")
        self.assertEqual(resp.status_code, 200)
        preview = next(g["preview"] for g in resp.json()["goals"] if g["title"] == "My goal")
        for marker in ("#", ">", "**"):
            self.assertNotIn(marker, preview)
        self.assertIn("ship the thing", preview)


@override_settings(NBHD_DISABLE_BACKGROUND_THREADS=True)
class HorizonsViewTopicSignalsTests(TestCase):
    """Phase 3 Day 2: HorizonsView surfaces a ``topic_signals`` array — one
    entry per topic the tenant has engaged with, showing the meta-state
    behind the assistant's voice register.

    Threads disabled because this class creates ``AssistantInsight``,
    ``PillarSnapshot``, and ``Document`` rows across 9 tests. Each save
    triggers ``_universal_refresh_receiver`` which spawns a daemon
    thread; with threads enabled, those threads open their own Postgres
    connections that linger past test-DB teardown and break CI cleanup
    with ``database is being accessed by other users``.
    """

    def setUp(self):
        from apps.insights.models import PillarSnapshot, UserVoicePref

        self.PillarSnapshot = PillarSnapshot
        self.UserVoicePref = UserVoicePref

        self.tenant = create_tenant(display_name="Horizons-Topics", telegram_chat_id=900720)
        self.other_tenant = create_tenant(display_name="Horizons-Topics-Other", telegram_chat_id=900721)
        self.client = APIClient()
        token = RefreshToken.for_user(self.tenant.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")

        # Both "dining" and "debt" are pre-seeded by insights migration 0002.
        self.dining, _ = TopicRegistry.objects.get_or_create(
            pillar=Pillar.GRAVITY.value,
            slug="dining",
            defaults={
                "display_name": "Dining",
                "status": TopicRegistry.Status.CANONICAL,
                "source": TopicRegistry.Source.SEED,
            },
        )
        self.debt, _ = TopicRegistry.objects.get_or_create(
            pillar=Pillar.GRAVITY.value,
            slug="debt",
            defaults={
                "display_name": "Debt",
                "status": TopicRegistry.Status.CANONICAL,
                "source": TopicRegistry.Source.SEED,
            },
        )

    def _signals(self):
        resp = self.client.get("/api/v1/dashboard/horizons/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("topic_signals", body)
        return body["topic_signals"]

    def test_empty_state_returns_empty_list(self):
        self.assertEqual(self._signals(), [])

    def test_topic_with_snapshots_appears(self):
        # PillarSnapshot has no topic FK — sample_size is derived via the
        # per-pillar extractor map (apps/insights/baselines.py). "debt" is
        # one of the gravity extractors, keyed off payload['totals']['debt'].
        from django.utils import timezone

        for weeks_ago in range(3):
            self.PillarSnapshot.objects.create(
                tenant=self.tenant,
                pillar=Pillar.GRAVITY.value,
                granularity=self.PillarSnapshot.Granularity.WEEKLY,
                ts=timezone.now() - timezone.timedelta(weeks=weeks_ago),
                payload={"totals": {"debt": 1000 + weeks_ago * 100}},
            )
        signals = self._signals()
        self.assertEqual(len(signals), 1)
        row = signals[0]
        self.assertEqual(row["topic_slug"], "debt")
        self.assertEqual(row["sample_size"], 3)
        self.assertEqual(row["confirmed"], 0)
        self.assertEqual(row["refuted"], 0)
        self.assertFalse(row["has_goal"])
        self.assertEqual(row["register_offset"], 0)
        self.assertIsNone(row["register_scope"])

    def test_topic_with_insights_appears_with_counts(self):
        for status in [
            AssistantInsight.Status.OPEN,
            AssistantInsight.Status.CONFIRMED,
            AssistantInsight.Status.CONFIRMED,
            AssistantInsight.Status.REFUTED,
        ]:
            AssistantInsight.objects.create(
                tenant=self.tenant,
                pillar=Pillar.GRAVITY.value,
                topic=self.dining,
                statement="s",
                status=status,
            )
        row = next(r for r in self._signals() if r["topic_slug"] == "dining")
        self.assertEqual(row["confirmed"], 2)
        self.assertEqual(row["refuted"], 1)

    def test_topic_with_typed_goal_has_goal_true(self):
        Goal.objects.create(
            tenant=self.tenant,
            title="Pay down dining",
            pillar=Pillar.GRAVITY.value,
            topic=self.dining,
            status=Goal.Status.ACTIVE,
        )
        row = next(r for r in self._signals() if r["topic_slug"] == "dining")
        self.assertTrue(row["has_goal"])

    def test_topic_with_legacy_document_goal_has_goal_true(self):
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.GOAL,
            slug="legacy-dining-goal",
            title="Legacy",
            markdown="x",
            pillar=Pillar.GRAVITY.value,
            topic=self.dining,
        )
        row = next(r for r in self._signals() if r["topic_slug"] == "dining")
        self.assertTrue(row["has_goal"])

    def test_topic_specific_voice_pref_surfaces_with_scope(self):
        self.UserVoicePref.objects.create(
            tenant=self.tenant,
            pillar=Pillar.GRAVITY.value,
            topic=self.dining,
            register_offset=1,
        )
        row = next(r for r in self._signals() if r["topic_slug"] == "dining")
        self.assertEqual(row["register_offset"], 1)
        self.assertEqual(row["register_scope"], "topic")

    def test_pillar_voice_pref_applies_to_topic_when_no_topic_pref(self):
        self.UserVoicePref.objects.create(
            tenant=self.tenant,
            pillar=Pillar.GRAVITY.value,
            topic=None,
            register_offset=-1,
        )
        # Need at least one source of engagement so the topic appears.
        AssistantInsight.objects.create(
            tenant=self.tenant,
            pillar=Pillar.GRAVITY.value,
            topic=self.dining,
            statement="x",
        )
        row = next(r for r in self._signals() if r["topic_slug"] == "dining")
        self.assertEqual(row["register_offset"], -1)
        self.assertEqual(row["register_scope"], "pillar")

    def test_topic_pref_wins_over_pillar_pref(self):
        self.UserVoicePref.objects.create(
            tenant=self.tenant,
            pillar=Pillar.GRAVITY.value,
            topic=None,
            register_offset=-1,
        )
        self.UserVoicePref.objects.create(
            tenant=self.tenant,
            pillar=Pillar.GRAVITY.value,
            topic=self.dining,
            register_offset=1,
        )
        row = next(r for r in self._signals() if r["topic_slug"] == "dining")
        self.assertEqual(row["register_offset"], 1)
        self.assertEqual(row["register_scope"], "topic")

    def test_tenant_isolation(self):
        AssistantInsight.objects.create(
            tenant=self.other_tenant,
            pillar=Pillar.GRAVITY.value,
            topic=self.dining,
            statement="other tenant",
        )
        self.assertEqual(self._signals(), [])

    def test_topics_sorted_by_pillar_then_display_name(self):
        # Both dining + debt have engagement; expect alphabetical by
        # display_name within the gravity pillar (Debt before Dining).
        AssistantInsight.objects.create(
            tenant=self.tenant,
            pillar=Pillar.GRAVITY.value,
            topic=self.dining,
            statement="d",
        )
        AssistantInsight.objects.create(
            tenant=self.tenant,
            pillar=Pillar.GRAVITY.value,
            topic=self.debt,
            statement="x",
        )
        slugs = [r["topic_slug"] for r in self._signals()]
        self.assertEqual(slugs, ["debt", "dining"])
