"""Dashboard cache-invalidation receiver tests.

Each model the dashboard reads should bump the per-tenant "dashboard"
cache tag on save and delete. Without this, the @tenant_cache wrapper
on HorizonsView / DashboardView serves stale data for up to 60 seconds
after a mutation — which silently reverts the frontend's optimistic
confirm/refute updates.
"""

from __future__ import annotations

from django.test import TestCase

from apps.common.cache import get_tag_version
from apps.insights.models import AssistantInsight, TopicRegistry, UserVoicePref
from apps.insights.pillars import Pillar
from apps.journal.models import Document, Goal
from apps.tenants.services import create_tenant


class DashboardCacheInvalidationTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="CacheInv", telegram_chat_id=900800)
        self.other_tenant = create_tenant(display_name="CacheInvOther", telegram_chat_id=900801)
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
        # Seed each tenant's tag so we have a baseline to compare against.
        # get_tag_version is the right primer — it lazily creates version=1.
        self.baseline = get_tag_version(self.tenant.id, "dashboard")
        self.other_baseline = get_tag_version(self.other_tenant.id, "dashboard")

    def _v(self, tenant=None) -> int:
        return get_tag_version((tenant or self.tenant).id, "dashboard")

    # --- AssistantInsight -----------------------------------------------

    def test_assistant_insight_save_bumps_tag(self):
        AssistantInsight.objects.create(
            tenant=self.tenant,
            pillar=Pillar.GRAVITY.value,
            topic=self.topic,
            statement="An observation.",
        )
        self.assertGreater(self._v(), self.baseline)

    def test_assistant_insight_status_change_bumps_tag(self):
        insight = AssistantInsight.objects.create(
            tenant=self.tenant,
            pillar=Pillar.GRAVITY.value,
            topic=self.topic,
            statement="An observation.",
        )
        before = self._v()
        insight.status = AssistantInsight.Status.CONFIRMED
        insight.save()
        self.assertGreater(self._v(), before)

    def test_assistant_insight_delete_bumps_tag(self):
        insight = AssistantInsight.objects.create(
            tenant=self.tenant,
            pillar=Pillar.GRAVITY.value,
            topic=self.topic,
            statement="An observation.",
        )
        before = self._v()
        insight.delete()
        self.assertGreater(self._v(), before)

    # --- UserVoicePref --------------------------------------------------

    def test_voice_pref_save_bumps_tag(self):
        UserVoicePref.objects.create(
            tenant=self.tenant,
            pillar=Pillar.GRAVITY.value,
            topic=self.topic,
            register_offset=1,
        )
        self.assertGreater(self._v(), self.baseline)

    def test_voice_pref_register_offset_flip_bumps_tag(self):
        pref = UserVoicePref.objects.create(
            tenant=self.tenant,
            pillar=Pillar.GRAVITY.value,
            topic=self.topic,
            register_offset=0,
        )
        before = self._v()
        pref.register_offset = 1
        pref.save()
        self.assertGreater(self._v(), before)

    # --- Goal -----------------------------------------------------------

    def test_goal_save_bumps_tag(self):
        Goal.objects.create(
            tenant=self.tenant,
            title="Pay down loan",
            status=Goal.Status.ACTIVE,
        )
        self.assertGreater(self._v(), self.baseline)

    def test_goal_mark_achieved_bumps_tag(self):
        goal = Goal.objects.create(
            tenant=self.tenant,
            title="Pay down loan",
            status=Goal.Status.ACTIVE,
        )
        before = self._v()
        goal.mark_achieved()
        self.assertGreater(self._v(), before)

    # --- Document -------------------------------------------------------

    def test_document_save_bumps_tag(self):
        # create_tenant already seeded starter Docs which bumped on initial
        # save — read a fresh baseline before the explicit update.
        before = self._v()
        Document.objects.filter(
            tenant=self.tenant,
            kind=Document.Kind.GOAL,
            slug="goals",
        ).update(markdown="new content")
        # .update() bypasses post_save; force a save() to verify receiver fires.
        doc = Document.objects.get(tenant=self.tenant, kind=Document.Kind.GOAL, slug="goals")
        doc.markdown = "newer content"
        doc.save()
        self.assertGreater(self._v(), before)

    def test_document_delete_bumps_tag(self):
        doc = Document.objects.filter(tenant=self.tenant, kind=Document.Kind.GOAL, slug="goals").first()
        self.assertIsNotNone(doc)
        before = self._v()
        doc.delete()
        self.assertGreater(self._v(), before)

    # --- Tenant isolation ----------------------------------------------

    def test_bumps_are_per_tenant(self):
        AssistantInsight.objects.create(
            tenant=self.tenant,
            pillar=Pillar.GRAVITY.value,
            topic=self.topic,
            statement="Tenant A only.",
        )
        # tenant_a bumped; other_tenant must be unchanged.
        self.assertGreater(self._v(self.tenant), self.baseline)
        self.assertEqual(self._v(self.other_tenant), self.other_baseline)
