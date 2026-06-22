"""Regression tests for fix-cluster A19 (adversarial review).

FA-0596-incomplete: _upsert_voice_pref_impl must be atomic — no orphaned
proposed topics if update_or_create fails after resolve_topic succeeds.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from apps.insights.models import TopicRegistry, UserVoicePref
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


def _make_tenant(*, display_name: str, chat_id: int) -> Tenant:
    """Create a minimal active tenant for insights tests.

    Uses .update() to bypass post-save signals (config bumps, cron seeding)
    that open background connections and break test teardown.
    """
    tenant = create_tenant(display_name=display_name, telegram_chat_id=chat_id)
    Tenant.objects.filter(pk=tenant.pk).update(status=Tenant.Status.ACTIVE)
    tenant.refresh_from_db()
    return tenant


class UpsertVoicePrefImplAtomicTest(TestCase):
    """FA-0596-incomplete — _upsert_voice_pref_impl is atomic: no orphaned proposed topic on failure."""

    def setUp(self):
        self.tenant = _make_tenant(display_name="A19Tenant", chat_id=91900001)

    def test_no_orphaned_topic_when_update_or_create_fails(self):
        """
        If UserVoicePref.objects.update_or_create raises after resolve_topic
        has created a proposed TopicRegistry row, the proposed row must be
        rolled back (not committed to the DB).

        Before the fix: resolve_topic's own @transaction.atomic committed the
        PROPOSED row, then update_or_create raised — leaving an orphan.
        After the fix: both operations run inside _tx.atomic(), so
        resolve_topic's block degrades to a savepoint; a failure in
        update_or_create rolls back the savepoint and the proposed row.
        """
        from apps.insights.views import _upsert_voice_pref_impl

        topic_created_pk: dict = {}

        original_resolve = __import__("apps.insights.topic_resolver", fromlist=["resolve_topic"]).resolve_topic

        def capturing_resolve(pillar, candidate, **kw):
            topic = original_resolve(pillar, candidate, **kw)
            topic_created_pk["pk"] = topic.pk
            return topic

        with (
            patch("apps.insights.views.resolve_topic", side_effect=capturing_resolve),
            patch.object(
                UserVoicePref.objects,
                "update_or_create",
                side_effect=Exception("simulated DB failure"),
            ),
            self.assertRaises(Exception),
        ):
            _upsert_voice_pref_impl(
                tenant=self.tenant,
                pillar="gravity",
                topic_slug="a19_orphan_test_topic",
                register_offset=0,
                tone=None,
                volume=None,
            )

        # The proposed topic must NOT survive in the DB.
        if "pk" in topic_created_pk:
            self.assertFalse(
                TopicRegistry.objects.filter(pk=topic_created_pk["pk"]).exists(),
                "Orphaned proposed topic was committed despite update_or_create failure",
            )

    def test_happy_path_creates_pref_and_topic(self):
        """Happy path: novel topic_slug → proposed topic created + voice pref upserted."""
        from apps.insights.views import _upsert_voice_pref_impl

        pref = _upsert_voice_pref_impl(
            tenant=self.tenant,
            pillar="gravity",
            topic_slug="a19_novel_topic_xyz",
            register_offset=1,
            tone="direct",
            volume=None,
        )

        self.assertIsInstance(pref, UserVoicePref)
        self.assertEqual(pref.register_offset, 1)
        self.assertEqual(pref.tone, "direct")
        # The proposed topic must have been created and linked.
        self.assertIsNotNone(pref.topic)
        self.assertEqual(pref.topic.status, TopicRegistry.Status.PROPOSED)

    def test_happy_path_no_topic_slug(self):
        """Pillar-wide pref (topic_slug=None) works and leaves no topic rows."""
        from apps.insights.views import _upsert_voice_pref_impl

        initial_count = TopicRegistry.objects.count()

        pref = _upsert_voice_pref_impl(
            tenant=self.tenant,
            pillar="gravity",
            topic_slug=None,
            register_offset=-1,
            tone=None,
            volume="live",
        )

        self.assertIsInstance(pref, UserVoicePref)
        self.assertIsNone(pref.topic)
        # No spurious topic rows created.
        self.assertEqual(TopicRegistry.objects.count(), initial_count)

    def test_existing_topic_slug_uses_existing_row(self):
        """If the slug already exists in TopicRegistry, resolve_topic short-circuits."""
        from apps.insights.views import _upsert_voice_pref_impl

        existing = TopicRegistry.objects.create(
            pillar="gravity",
            slug="a19_existing_topic",
            display_name="A19 Existing Topic",
            status=TopicRegistry.Status.CANONICAL,
            source=TopicRegistry.Source.SEED,
        )

        pref = _upsert_voice_pref_impl(
            tenant=self.tenant,
            pillar="gravity",
            topic_slug="a19_existing_topic",
            register_offset=0,
            tone="gentle",
            volume=None,
        )

        self.assertEqual(pref.topic_id, existing.pk)
        # No extra topic rows created.
        self.assertEqual(TopicRegistry.objects.filter(pillar="gravity", slug="a19_existing_topic").count(), 1)
