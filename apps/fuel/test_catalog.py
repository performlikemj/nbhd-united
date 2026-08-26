"""Parity and integrity tests for the bundled iOS Workout Guide catalog."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from django.test import SimpleTestCase

from apps.common.llm_lookups import _EXERCISE_REGISTRY

from . import catalog
from .catalog import Catalog, Entry

_PATH = Path(__file__).with_name("data") / "workout_guide_catalog.json"
_EXPECTED_SHA256 = "8ab831def6058d3029df2d514efe00f935368f9d3d14ecdc5f64a237f72eeba5"

# These category/metric-inference keys are intentionally broader than the
# illustrated iOS catalog. Keep this explicit so new registry names are reviewed.
_INTENTIONAL_NON_CATALOG_KEYS = {
    "back lever",
    "barbell curl",
    "box jump",
    "bridge hold",
    "broad jump",
    "clean",
    "foam roll",
    "front lever",
    "handstand hold",
    "hang",
    "headstand",
    "human flag",
    "incline press",
    "knees to chest",
    "mobility",
    "muscle up",
    "muscle-up",
    "power clean",
    "row",
    "ruck",
    "seal row",
    "sit up",
    "sit-up",
    "snatch",
    "sprint",
    "stretch",
    "stretching",
    "thruster",
    "toes to bar",
    "tricep extension",
    "yoga",
}


def _fixture_catalog() -> Catalog:
    return Catalog(
        [
            Entry("push-up", "Push-up", "Bodyweight", "Chest", False, 3),
            Entry("crunch", "Crunch", "Bodyweight", "Core", False, 2),
        ],
        {"pushups": "push-up", "unknown-target": "does-not-exist"},
    )


class WorkoutGuideCatalogTests(SimpleTestCase):
    def test_normalize_apostrophes_and_punctuation(self):
        self.assertEqual(catalog.normalize("Child's Pose"), "childs-pose")
        self.assertEqual(catalog.normalize("World’s Greatest Stretch"), "worlds-greatest-stretch")
        self.assertEqual(catalog.normalize("  Push  Ups! "), "push-ups")
        self.assertEqual(catalog.normalize(""), "")

    def test_match_exact_and_case_insensitive(self):
        subject = _fixture_catalog()
        self.assertEqual(subject.match("Push-up").slug, "push-up")
        self.assertEqual(subject.match("push-ups").slug, "push-up")
        self.assertEqual(subject.match("PUSH-UPS").slug, "push-up")
        self.assertEqual(subject.match("Pushups").slug, "push-up")

    def test_match_strips_trailing_s(self):
        self.assertEqual(_fixture_catalog().match("Pushups").slug, "push-up")

    def test_match_strips_trailing_es(self):
        self.assertEqual(_fixture_catalog().match("Crunches").slug, "crunch")

    def test_match_none_for_unknown_names(self):
        subject = _fixture_catalog()
        for name in ("Squat", "Back squat", "", "    "):
            with self.subTest(name=name):
                self.assertIsNone(subject.match(name))

    def test_alias_to_unknown_slug_is_ignored(self):
        subject = _fixture_catalog()
        self.assertIsNone(subject.match("unknown-target"))
        self.assertIsNone(subject.match("does not exist"))

    def test_image_name_formats_frame(self):
        entry = Entry("push-up", "Push-up", "Bodyweight", "Chest", False, 3)
        self.assertEqual(entry.image_name(2), "wg-push-up-2")

    def test_shared_catalog_shipped_in_packaged_path(self):
        self.assertTrue(_PATH.is_file())
        self.assertEqual(len(catalog._catalog().entries), 302)
        self.assertEqual(catalog.match("Jumping jacks").slug, "jumping-jack")
        self.assertEqual(catalog.match("Bench press").slug, "bench-press")
        self.assertEqual(catalog.match("Back squat").slug, "squat")
        self.assertEqual(catalog.match("Lat pulldowns").slug, "lat-pulldown")


class WorkoutGuideCatalogSearchTests(SimpleTestCase):
    def setUp(self):
        self.subject = Catalog(
            [
                Entry("push-up", "Push-up", "Bodyweight", "Chest", False, 3),
                Entry("bench-press", "Bench Press", "Barbell", "Chest", False, 3),
                Entry("plank", "Plank", "Bodyweight", "Core", False, 3),
            ],
            {"pushup": "push-up", "bench": "bench-press"},
        )

    def test_empty_query_lists_everything_sorted_by_name(self):
        self.assertEqual(
            [entry.slug for entry in self.subject.search("", limit=20)],
            ["bench-press", "plank", "push-up"],
        )

    def test_query_matches_name_substring(self):
        self.assertEqual([entry.slug for entry in self.subject.search("push")], ["push-up"])
        self.assertEqual([entry.slug for entry in self.subject.search("PLA")], ["plank"])

    def test_query_matches_alias_and_exact_match_sorts_first(self):
        self.assertEqual([entry.slug for entry in self.subject.search("bench")], ["bench-press"])
        self.assertEqual(self.subject.search("Pushups")[0].slug, "push-up")

    def test_filters_by_muscle_and_equipment(self):
        self.assertEqual(len(self.subject.search("", muscle="Chest")), 2)
        self.assertEqual(
            [entry.slug for entry in self.subject.search("", muscle="Chest", equipment="Bodyweight")],
            ["push-up"],
        )
        self.assertEqual([entry.slug for entry in self.subject.search("", equipment="Barbell")], ["bench-press"])
        self.assertEqual(self.subject.search("plank", muscle="Chest"), [])

    def test_menus_list_sorted_unique_values(self):
        self.assertEqual(self.subject.muscles(), ["Chest", "Core"])
        self.assertEqual(self.subject.equipment_types(), ["Barbell", "Bodyweight"])

    def test_shared_catalog_search_finds_gym_moves(self):
        self.assertIn("lat-pulldown", [entry.slug for entry in catalog.search("lat pull")])
        self.assertEqual(catalog.search("rdl")[0].slug, "romanian-deadlift")
        self.assertGreater(len(catalog.muscles()), 5)

    def test_query_matches_primary_muscle_and_plural_filters(self):
        self.assertIn("romanian-deadlift", [entry.slug for entry in catalog.search("hamstring", limit=302)])
        self.assertEqual(
            [entry.slug for entry in self.subject.search("", muscle="Chests")],
            ["bench-press", "push-up"],
        )


class WorkoutGuideCatalogIntegrityTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.raw = _PATH.read_bytes()
        cls.document = json.loads(cls.raw)

    def test_catalog_sha256_is_pinned(self):
        # Update deliberately when nbhd-ios bumps the catalog.
        self.assertEqual(hashlib.sha256(self.raw).hexdigest(), _EXPECTED_SHA256)

    def test_counts_targets_and_alias_keys(self):
        entries = self.document["entries"]
        aliases = self.document["aliases"]
        slugs = {entry["slug"] for entry in entries}
        self.assertEqual(len(entries), 302)
        self.assertEqual(len(aliases), 315)
        self.assertTrue(set(aliases.values()) <= slugs)
        normalized_aliases = [catalog.normalize(alias) for alias in aliases]
        self.assertEqual(len(normalized_aliases), len(set(normalized_aliases)))
        self.assertFalse(set(normalized_aliases) & slugs)

    def test_frames_are_ints_and_eight_names_have_distinct_slug_forms(self):
        distinct = [entry for entry in self.document["entries"] if entry["slug"] != catalog.normalize(entry["name"])]
        self.assertEqual(len(distinct), 8)
        self.assertTrue(all(type(entry["frames"]) is int for entry in self.document["entries"]))
        for entry in distinct:
            with self.subTest(slug=entry["slug"]):
                self.assertEqual(catalog.match(entry["slug"]).slug, entry["slug"])
                self.assertEqual(catalog.match(entry["name"]).slug, entry["slug"])

    def test_spot_names_match(self):
        self.assertEqual(catalog.match("Nordic Hamstring Curl").slug, "nordic-hamstring-curl")
        self.assertEqual(catalog.match("Romanian Deadlift").slug, "romanian-deadlift")

    def test_registry_keys_are_cataloged_or_intentionally_excluded(self):
        missing = {key for key in _EXERCISE_REGISTRY if catalog.match(key) is None}
        self.assertEqual(missing, _INTENTIONAL_NON_CATALOG_KEYS)

    def test_loader_is_cached(self):
        self.assertIs(catalog._catalog(), catalog._catalog())
