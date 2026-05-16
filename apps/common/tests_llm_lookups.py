"""Unit tests for apps.common.llm_lookups.

No DB. Plain unit tests — wrapping ``TestCase`` so Django's test runner
discovers them via ``manage.py test apps.common``.
"""

from django.test import SimpleTestCase

from apps.common.llm_lookups import (
    METRIC_BODYWEIGHT_REPS,
    METRIC_DISTANCE_TIME,
    METRIC_HOLD_TIME,
    METRIC_WEIGHTED_REPS,
    convert_distance,
    convert_weight,
    kg_to_lbs,
    lbs_to_kg,
    normalize_exercise,
)


class NormalizeExerciseTests(SimpleTestCase):
    def test_plank_is_calisthenics_hold_time(self):
        """The bug that started this project: plank should not be reps × weight."""
        self.assertEqual(
            normalize_exercise("plank"),
            ("calisthenics", METRIC_HOLD_TIME),
        )

    def test_side_plank_with_rotation_matches_via_substring(self):
        """Unknown variants still resolve via longest substring match."""
        self.assertEqual(
            normalize_exercise("side plank with rotation"),
            ("calisthenics", METRIC_HOLD_TIME),
        )

    def test_bench_press_is_strength_weighted_reps(self):
        self.assertEqual(
            normalize_exercise("bench press"),
            ("strength", METRIC_WEIGHTED_REPS),
        )

    def test_pull_up_is_calisthenics_bodyweight_reps(self):
        self.assertEqual(
            normalize_exercise("pull-up"),
            ("calisthenics", METRIC_BODYWEIGHT_REPS),
        )

    def test_weighted_pull_up_promotes_to_strength_weighted(self):
        """`weighted X` should never end up as bodyweight reps."""
        self.assertEqual(
            normalize_exercise("weighted pull-ups"),
            ("strength", METRIC_WEIGHTED_REPS),
        )

    def test_running_is_cardio_distance_time(self):
        self.assertEqual(
            normalize_exercise("running"),
            ("cardio", METRIC_DISTANCE_TIME),
        )

    def test_case_and_whitespace_insensitive(self):
        self.assertEqual(
            normalize_exercise("  Bench Press  "),
            ("strength", METRIC_WEIGHTED_REPS),
        )

    def test_unknown_exercise_returns_none(self):
        # Picked deliberately so it has no substring overlap with any
        # canonical name in the registry. If you add a registry entry
        # that contains "frob" or "snizzle", change this string.
        self.assertIsNone(normalize_exercise("frobnication snizzle"))

    def test_empty_returns_none(self):
        self.assertIsNone(normalize_exercise(""))
        self.assertIsNone(normalize_exercise("   "))


class UnitConversionTests(SimpleTestCase):
    def test_kg_lbs_roundtrip(self):
        self.assertAlmostEqual(lbs_to_kg(kg_to_lbs(100.0)), 100.0, places=6)

    def test_convert_weight_known_values(self):
        # 100 kg ≈ 220.462 lb (factor is 2.20462262185 by definition).
        self.assertAlmostEqual(convert_weight(100, "kg", "lbs"), 220.462262185, places=6)
        self.assertAlmostEqual(convert_weight(220.462262185, "lbs", "kg"), 100.0, places=6)

    def test_convert_weight_same_unit_passthrough(self):
        self.assertEqual(convert_weight(75.5, "kg", "kg"), 75.5)
        self.assertEqual(convert_weight(165, "lbs", "lbs"), 165)

    def test_convert_weight_case_insensitive(self):
        self.assertAlmostEqual(convert_weight(100, "KG", "Lbs"), 220.462262185, places=6)

    def test_convert_weight_rejects_unknown_unit(self):
        with self.assertRaises(ValueError):
            convert_weight(1, "kg", "stone")

    def test_convert_distance_known_values(self):
        # 1 mile = 1.609344 km exactly.
        self.assertAlmostEqual(convert_distance(1, "mi", "km"), 1.609344, places=6)
        self.assertAlmostEqual(convert_distance(1.609344, "km", "mi"), 1.0, places=6)

    def test_convert_distance_same_unit_passthrough(self):
        self.assertEqual(convert_distance(5.0, "km", "km"), 5.0)
