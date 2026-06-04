"""Deterministic test for the grounding probe (layer 1 of the harness).

Builds a tenant + documents and asserts the probe correctly reports whether a
ground-truth fact is reachable in the structured state a proactive cron sees.
Needs Postgres (the probe replicates ``nbhd_journal_search``'s full-text query).
"""

from __future__ import annotations

from django.test import TestCase

from apps.journal.models import Document
from apps.orchestrator.grounding_probe import probe_grounding
from apps.tenants.services import create_tenant


class GroundingProbeTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="ProbeT", telegram_chat_id=760900)

    def _doc(self, kind: str, slug: str, markdown: str, title: str = "t") -> Document:
        return Document.objects.create(tenant=self.tenant, kind=kind, slug=slug, title=title, markdown=markdown)

    def test_red_when_recent_substance_absent(self):
        # A stale project doc mentions the topic but NOT the recent update —
        # the exact shape of the Security Champions gap.
        self._doc("project", "acme", "Acme project: kicked off the build back in April.")
        report = probe_grounding(self.tenant, "Acme", ["shipped to prod"])
        self.assertFalse(report.grounded)
        self.assertFalse(report.term_reachable["shipped to prod"])
        self.assertTrue(any(d["slug"] == "acme" for d in report.reachable_docs))

    def test_green_when_recent_substance_reachable(self):
        self._doc("daily", "2026-06-02", "Acme project — shipped to prod today, big milestone.")
        report = probe_grounding(self.tenant, "Acme", ["shipped to prod"])
        self.assertTrue(report.grounded)
        self.assertTrue(report.term_reachable["shipped to prod"])

    def test_multiple_terms_all_required(self):
        self._doc("daily", "2026-06-02", "Acme — shipped to prod. Budget approved too.")
        report = probe_grounding(self.tenant, "Acme", ["shipped to prod", "never mentioned"])
        self.assertTrue(report.term_reachable["shipped to prod"])
        self.assertFalse(report.term_reachable["never mentioned"])
        self.assertFalse(report.grounded)  # one missing → RED

    def test_no_terms_grounded_iff_topic_has_docs(self):
        empty = probe_grounding(self.tenant, "Nonexistent", [])
        self.assertFalse(empty.grounded)
        self._doc("project", "zeta", "Zeta initiative — planning notes.")
        found = probe_grounding(self.tenant, "Zeta", [])
        self.assertTrue(found.grounded)

    def test_misfiled_to_daily_is_reachable_but_not_project_grounded(self):
        # The exact misfile bug: a status update appended to a daily doc (wrong
        # kind) instead of the canonical project doc. Reachable via search, but
        # the project doc stays stale → grounded True, grounded_in_project False.
        self._doc("project", "acme", "Acme project: kicked off the build in April.")
        self._doc("daily", "acme", "Acme — shipped to prod today.")
        report = probe_grounding(self.tenant, "Acme", ["shipped to prod"])
        self.assertTrue(report.grounded)  # journal search finds it in the daily doc
        self.assertFalse(report.grounded_in_project)  # but NOT in the project doc — misfile caught
        self.assertFalse(report.term_in_project["shipped to prod"])

    def test_project_doc_substance_is_project_grounded(self):
        self._doc("project", "acme", "Acme project. Update: shipped to prod today.")
        report = probe_grounding(self.tenant, "Acme", ["shipped to prod"])
        self.assertTrue(report.grounded)
        self.assertTrue(report.grounded_in_project)
        self.assertTrue(report.term_in_project["shipped to prod"])
