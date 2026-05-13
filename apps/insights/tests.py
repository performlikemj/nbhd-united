"""Tests for the Phase 0 foundation: topic resolver + seed."""

from __future__ import annotations

from django.test import TestCase

from .models import TopicAlias, TopicRegistry
from .pillars import Pillar
from .seed import seed_topics
from .topic_resolver import resolve_topic


class TopicResolverTests(TestCase):
    def setUp(self):
        self.canonical = TopicRegistry.objects.create(
            pillar=Pillar.GRAVITY.value,
            slug="dining",
            display_name="Dining",
            status=TopicRegistry.Status.CANONICAL,
            source=TopicRegistry.Source.SEED,
        )
        TopicAlias.objects.create(
            topic=self.canonical,
            alias="eating out",
            source=TopicAlias.Source.SEED,
        )

    def test_exact_slug_match(self):
        topic = resolve_topic(Pillar.GRAVITY.value, "dining")
        self.assertEqual(topic.pk, self.canonical.pk)

    def test_alias_match_case_insensitive(self):
        topic = resolve_topic(Pillar.GRAVITY.value, "Eating Out")
        self.assertEqual(topic.pk, self.canonical.pk)

    def test_alias_match_with_whitespace(self):
        topic = resolve_topic(Pillar.GRAVITY.value, "  eating out  ")
        self.assertEqual(topic.pk, self.canonical.pk)

    def test_proposed_creation_when_no_match(self):
        topic = resolve_topic(Pillar.GRAVITY.value, "Vintage Wine Hunting", model_version="opus-4.7")
        self.assertEqual(topic.status, TopicRegistry.Status.PROPOSED)
        self.assertEqual(topic.slug, "vintage_wine_hunting")
        self.assertEqual(topic.proposed_by_model_version, "opus-4.7")

    def test_proposed_slug_collision_increments(self):
        TopicRegistry.objects.create(
            pillar=Pillar.GRAVITY.value,
            slug="freelance_income",
            display_name="Freelance income",
            status=TopicRegistry.Status.PROPOSED,
            source=TopicRegistry.Source.PROPOSED_BY_MODEL,
        )
        topic = resolve_topic(Pillar.GRAVITY.value, "Freelance Income")
        self.assertEqual(topic.slug, "freelance_income_2")

    def test_pillar_scoped_resolution(self):
        TopicRegistry.objects.create(
            pillar=Pillar.FUEL.value,
            slug="dining",
            display_name="Dining (fuel meaning)",
            status=TopicRegistry.Status.CANONICAL,
            source=TopicRegistry.Source.SEED,
        )
        topic = resolve_topic(Pillar.GRAVITY.value, "dining")
        self.assertEqual(topic.pillar, Pillar.GRAVITY.value)
        self.assertEqual(topic.pk, self.canonical.pk)

    def test_non_canonical_slug_does_not_match(self):
        TopicRegistry.objects.create(
            pillar=Pillar.GRAVITY.value,
            slug="weekend_takeout",
            display_name="Weekend Takeout",
            status=TopicRegistry.Status.PROPOSED,
            source=TopicRegistry.Source.PROPOSED_BY_MODEL,
        )
        # Same string should create a second proposed (collision-suffixed) since
        # the existing row is not canonical, so step 1 misses; step 4 fires.
        topic = resolve_topic(Pillar.GRAVITY.value, "Weekend Takeout")
        self.assertEqual(topic.status, TopicRegistry.Status.PROPOSED)
        self.assertEqual(topic.slug, "weekend_takeout_2")


class SeedTopicsTests(TestCase):
    def test_seed_creates_gravity_and_fuel_canonical_topics(self):
        seed_topics()
        gravity_count = TopicRegistry.objects.filter(
            pillar=Pillar.GRAVITY.value, status=TopicRegistry.Status.CANONICAL
        ).count()
        fuel_count = TopicRegistry.objects.filter(
            pillar=Pillar.FUEL.value, status=TopicRegistry.Status.CANONICAL
        ).count()
        self.assertGreaterEqual(gravity_count, 5)
        self.assertGreaterEqual(fuel_count, 5)

    def test_seed_is_idempotent(self):
        seed_topics()
        first = TopicRegistry.objects.count()
        first_aliases = TopicAlias.objects.count()
        seed_topics()
        self.assertEqual(TopicRegistry.objects.count(), first)
        self.assertEqual(TopicAlias.objects.count(), first_aliases)

    def test_seeded_aliases_resolve_to_canonical(self):
        seed_topics()
        topic = resolve_topic(Pillar.GRAVITY.value, "eating out")
        self.assertEqual(topic.slug, "dining")
        self.assertEqual(topic.status, TopicRegistry.Status.CANONICAL)

    def test_seeded_topic_resolves_by_slug(self):
        seed_topics()
        topic = resolve_topic(Pillar.FUEL.value, "sleep_quality")
        self.assertEqual(topic.slug, "sleep_quality")
        self.assertEqual(topic.pillar, Pillar.FUEL.value)
