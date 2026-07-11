"""Deterministic, labeled regression corpus for PII redaction (Wave C1 evals).

Every case below is a synthetic ``(input_text, must_redact, must_survive)``
tuple mined from a real incident class (weight/measurement false positives
#1104, house-number addresses, real names, fitness-vocabulary garbling, and
denylisted brands/false-positive words) rather than an ad hoc example. Names,
addresses, and brands are ENTIRELY INVENTED — nothing here resembles a real
person or place.

This file is a single scannable table, not a replacement for the existing
scenario-specific suites it complements: ``tests.py`` (``BuildingNumberMeasurementGuardTest``,
``FitnessFalsePositiveTest``, ``PinScoreOverrideTest``) and ``test_hygiene.py``
(``IsJunkSpanTest``) already pin the underlying guard mechanics case-by-case;
this corpus exercises the same guards through the PUBLIC entry points
(``redact_text``, ``redact_known_entities``, ``RedactionSession``) as one
table per entry point, so a new incident is added as one row instead of a
new test method.

Three tables, one per entry point under test:

* ``DETECT_CASES`` — fresh-detection path (``redact_text`` + a fake NER
  pipeline stubbed at ``apps.pii.engine.get_pii_pipeline``, mirroring the
  ``_fake_pipeline`` pattern in ``tests.py``). Covers weight/measurement
  guards, house-number addresses, real names, and fitness/garbling
  false-positives — the classes where WHAT the model would have emitted
  matters.
* ``KNOWN_ENTITY_CASES`` — reuse-only path (``redact_known_entities``, no
  detection). Covers known-name substitution and denylisted
  brands/false-positive words.
* ``PinnedBehaviorTest`` — a few EXACT-output cases where a substring
  assertion would hide the point: longest-match-first precedence, and the
  documented same-name-fusion behavior (two different people sharing a
  surface string collapse onto one placeholder — see
  ``pii_same_name_fusion`` in the PII docs; not a bug, a pinned trade-off).

A row that fails against current code is a FINDING, not something to
loosen — see the module-level ``# FINDING:`` comments for any such case.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.pii.redactor import DetectedEntity, RedactionSession, redact_known_entities, redact_text


def _fake_pipeline(hits):
    """Stand-in for the HF token-classification pipeline.

    ``hits`` is a list of ``(raw_label, word, score)``; positions are resolved
    by locating ``word`` in the text. Returns the dict shape the real pipeline
    emits (``entity_group``/``word``/``score``/``start``/``end``) so
    ``_detect_pii`` runs its real label-map + threshold + raw-label guards.
    Duplicated from the identically-named helper in ``tests.py`` — test-only
    infra, kept local so this file has no cross-test-module coupling.
    """

    def _run(text):
        out = []
        for raw_label, word, score in hits:
            idx = text.find(word)
            if idx < 0:
                continue
            out.append({"entity_group": raw_label, "word": word, "score": score, "start": idx, "end": idx + len(word)})
        return out

    return _run


# ---------------------------------------------------------------------------
# Table 1 — fresh detection path (redact_text + fake NER pipeline)
# ---------------------------------------------------------------------------
#
# Columns: (case_id, incident_class, text, hits, must_redact, must_survive)
#   hits          — synthetic raw model output: [(raw_label, span_text, score), ...]
#   must_redact   — substrings that must be ABSENT from the output
#   must_survive  — substrings that must be PRESENT in the output verbatim
DETECT_CASES = [
    # --- weight / measurement: BUILDINGNUMBER->LOCATION distrust (#1104) ---
    # A bare number/measurement with no adjacent street/city span is a
    # weight or lift number, not an address, and must survive untouched.
    (
        "weight_kg_bare",
        "weight_measurement",
        "my weight is 82kg",
        [("BUILDINGNUMBER", "82kg", 0.8)],
        [],
        ["82kg"],
    ),
    (
        "weight_lbs_spaced",
        "weight_measurement",
        "weighed 180 lbs this morning",
        [("BUILDINGNUMBER", "180 lbs", 0.75)],
        [],
        ["180 lbs"],
    ),
    (
        "lift_number_no_unit",
        "weight_measurement",
        "benched 225 for a new PR",
        [("BUILDINGNUMBER", "225", 0.7)],
        [],
        ["225", "new PR"],
    ),
    (
        "bare_decimal_no_unit",
        "weight_measurement",
        "logged 18.5 today",
        [("BUILDINGNUMBER", "18.5", 0.6)],
        [],
        ["18.5"],
    ),
    (
        "intl_decimal_comma_weight",
        "weight_measurement",
        "weight 82,5 kg",
        [("BUILDINGNUMBER", "82,5", 0.8)],
        [],
        ["82,5"],
    ),
    (
        "lone_building_number_accepted_tradeoff",
        "weight_measurement",
        "I'm at number 82",
        [("BUILDINGNUMBER", "82", 0.9)],
        [],
        ["82"],
    ),
    # Adjacency control: the SAME bare number redacts once a real street
    # span sits next to it — proves the guard is adjacency-gated, not a
    # blanket "never redact BUILDINGNUMBER" rule.
    (
        "buildingnumber_redacts_with_adjacent_street",
        "weight_measurement",
        "I live at 82 Baker Street, please ship it there",
        [("BUILDINGNUMBER", "82", 0.8), ("STREET", "Baker Street", 0.9)],
        ["82 Baker Street"],
        ["please ship it there"],
    ),
    # Control: a correctly-labeled ZIPCODE never enters the BUILDINGNUMBER
    # guard at all (only the raw BUILDINGNUMBER label is distrusted) — a
    # 5-digit ZIP still redacts even though it is "just a bare number".
    (
        "zipcode_label_still_redacts",
        "weight_measurement",
        "zip is 90210",
        [("ZIPCODE", "90210", 0.9)],
        ["90210"],
        [],
    ),
    # --- house numbers / numeric location that SHOULD redact ---
    (
        "alphanumeric_house_number_with_street_and_city",
        "house_number",
        "ship to 221B Baker Street, London",
        [("BUILDINGNUMBER", "221B", 0.85), ("STREET", "Baker Street", 0.9), ("CITY", "London", 0.9)],
        ["221B", "Baker Street", "London"],
        ["ship to", "[LOCATION_1]", "[LOCATION_2]"],
    ),
    (
        "european_word_order_street_then_number",
        "house_number",
        "Hauptstrasse 82 is home",
        [("STREET", "Hauptstrasse", 0.9), ("BUILDINGNUMBER", "82", 0.8)],
        ["Hauptstrasse"],
        ["is home"],
    ),
    (
        "zip_plus_four_redacts",
        "house_number",
        "postal code 94103-1234 for shipping",
        [("ZIPCODE", "94103-1234", 0.9)],
        ["94103-1234"],
        ["postal code", "for shipping"],
    ),
    # --- real names (should redact) ---
    (
        "single_full_name",
        "real_name",
        "Had a great call with Priya Raman about the launch",
        [("FULLNAME", "Priya Raman", 0.95)],
        ["Priya Raman"],
        ["about the launch"],
    ),
    (
        "two_distinct_people_numbered_separately",
        "real_name",
        "Please contact Marcus Webb and Elena Torres about scheduling",
        [("FULLNAME", "Marcus Webb", 0.95), ("FULLNAME", "Elena Torres", 0.93)],
        ["Marcus Webb", "Elena Torres"],
        ["Please contact", "[PERSON_1]", "[PERSON_2]", "about scheduling"],
    ),
    (
        "hyphenated_name_redacts_whole_span",
        "real_name",
        "Jean-Luc Moreau signed the lease yesterday",
        [("FULLNAME", "Jean-Luc Moreau", 0.9)],
        ["Jean-Luc Moreau"],
        ["signed the lease yesterday"],
    ),
    # --- fitness vocabulary / garbling false positives ---
    (
        "exercise_name_and_rep_scheme_survive",
        "fitness_false_positive",
        "did Romanian Deadlifts 5x5 at 315 lbs",
        [("FULLNAME", "Romanian Deadlifts", 0.85), ("STREET", "315 lbs", 0.7)],
        [],
        ["Romanian Deadlifts", "5x5", "315 lbs"],
    ),
    (
        "weight_with_unit_mislabeled_street_survives",
        "fitness_false_positive",
        "squatted 140kg today, new PR!",
        [("STREET", "140kg", 0.76)],
        [],
        ["140kg", "new PR"],
    ),
    (
        "real_pin_above_override_redacts",
        "fitness_false_positive",
        "my PIN is 4821, definitely not my rep count",
        [("PIN", "4821", 0.8)],
        ["4821"],
        ["not my rep count"],
    ),
    (
        "marginal_pin_below_override_survives",
        "fitness_false_positive",
        "my PIN is 4821",
        [("PIN", "4821", 0.6)],
        [],
        ["4821"],
    ),
    (
        "real_name_redacts_alongside_lift_number",
        "fitness_false_positive",
        "tell Jordan Blake I benched 225 for 5x5",
        [("FULLNAME", "Jordan Blake", 0.9), ("BUILDINGNUMBER", "225", 0.7)],
        ["Jordan Blake"],
        ["225", "5x5"],
    ),
    (
        "imperative_mark_at_sentence_start_survives",
        "fitness_false_positive",
        "Mark task done, then rest",
        [("FULLNAME", "Mark", 0.9)],
        [],
        ["Mark task"],
    ),
    # Contrast case: the SAME collision word mid-sentence is a real name.
    (
        "mark_mid_sentence_redacts",
        "fitness_false_positive",
        "I met Mark for coffee yesterday",
        [("FULLNAME", "Mark", 0.9)],
        ["Mark"],
        ["I met", "for coffee yesterday"],
    ),
    (
        "multiword_fitness_phrase_token_level_guard",
        "fitness_false_positive",
        "started with a vinyasa flow warmup",
        [("FULLNAME", "vinyasa flow", 0.6)],
        [],
        ["vinyasa flow", "warmup"],
    ),
]


class DetectionCorpusTest(SimpleTestCase):
    """Data-driven over ``DETECT_CASES`` — the fresh-detection entry point."""

    def _redact(self, text, hits):
        with (
            patch("apps.pii.engine.get_pii_pipeline", return_value=_fake_pipeline(hits)),
            patch("apps.pii.engine.get_pattern_recognizers", return_value={}),
        ):
            return redact_text(text, tier="starter")

    def test_corpus(self):
        for case_id, incident_class, text, hits, must_redact, must_survive in DETECT_CASES:
            with self.subTest(case=case_id, incident_class=incident_class):
                result = self._redact(text, hits)
                for needle in must_redact:
                    self.assertNotIn(needle, result, f"{case_id!r}: {needle!r} should have been redacted")
                for needle in must_survive:
                    self.assertIn(needle, result, f"{case_id!r}: {needle!r} should have survived redaction")


# ---------------------------------------------------------------------------
# Table 2 — reuse-only path (redact_known_entities, no detection)
# ---------------------------------------------------------------------------
#
# Columns: (case_id, incident_class, entity_map, denylist, text, must_redact, must_survive)
KNOWN_ENTITY_CASES = [
    (
        "case_insensitive_known_name_all_variants",
        "real_name",
        {"[PERSON_1]": {"name": "Devon Okafor"}},
        {},
        "devon okafor stopped by, so did DEVON OKAFOR again",
        ["Devon Okafor", "devon okafor", "DEVON OKAFOR"],
        ["stopped by", "again"],
    ),
    (
        "denylisted_brand_name_stops_redacting",
        "denylisted_brand",
        {"[PERSON_1]": {"name": "Nimbus Kitchen"}},
        {"nimbus kitchen": {"reason": "false_positive"}},
        "Nimbus Kitchen posted a new recipe today",
        [],
        ["Nimbus Kitchen", "posted a new recipe today"],
    ),
    (
        "denylisted_common_word_stops_redacting",
        "denylisted_brand",
        {"[PERSON_1]": {"name": "goal"}},
        {"goal": {"reason": "arbiter"}},
        "my goal this week is consistency",
        [],
        ["goal", "consistency"],
    ),
    (
        "denylist_is_per_entry_not_per_map",
        "denylisted_brand",
        {"[PERSON_1]": {"name": "Nimbus Kitchen"}, "[PERSON_2]": {"name": "Priya Raman"}},
        {"nimbus kitchen": {"reason": "false_positive"}},
        "Nimbus Kitchen told Priya Raman about the plan",
        ["Priya Raman"],
        ["Nimbus Kitchen", "about the plan"],
    ),
]


class KnownEntityCorpusTest(SimpleTestCase):
    """Data-driven over ``KNOWN_ENTITY_CASES`` — the reuse-only entry point.

    Pure function, no DB: ``redact_known_entities`` only reads
    ``pii_entity_map`` / ``pii_denylist`` off the tenant, so a
    ``SimpleNamespace`` stub is enough (mirrors ``test_redact_known_entities.py``).
    """

    def _redact(self, entity_map, denylist, text):
        tenant = SimpleNamespace(pii_entity_map=entity_map, pii_denylist=denylist)
        return redact_known_entities(tenant, text)

    def test_corpus(self):
        for case_id, incident_class, entity_map, denylist, text, must_redact, must_survive in KNOWN_ENTITY_CASES:
            with self.subTest(case=case_id, incident_class=incident_class):
                result = self._redact(entity_map, denylist, text)
                for needle in must_redact:
                    self.assertNotIn(needle, result, f"{case_id!r}: {needle!r} should have been redacted")
                for needle in must_survive:
                    self.assertIn(needle, result, f"{case_id!r}: {needle!r} should have survived redaction")


# ---------------------------------------------------------------------------
# Table 3 — pinned exact-output behavior (substring checks would hide the point)
# ---------------------------------------------------------------------------


class PinnedBehaviorTest(SimpleTestCase):
    """A few cases where the EXACT output matters, not just presence/absence.

    Same-name fusion is documented, accepted behavior (see the PII
    same-name-fusion assessment), not a bug — these tests pin it so a future
    change to the mint/reuse logic surfaces as an intentional decision, not a
    silent regression.
    """

    def test_longest_name_wins_precedence(self):
        # "Devon Okafor Jr." must match before the shorter "Devon" entry, or
        # the short name would corrupt it into "[PERSON_2] Jr.".
        tenant = SimpleNamespace(
            pii_entity_map={
                "[PERSON_1]": {"name": "Devon Okafor Jr."},
                "[PERSON_2]": {"name": "Devon"},
            },
            pii_denylist={},
        )
        out = redact_known_entities(tenant, "Devon Okafor Jr. called Devon")
        self.assertEqual(out, "[PERSON_1] called [PERSON_2]")

    def test_same_name_fuses_onto_existing_placeholder(self):
        # Two different people sharing an exact surface string collapse onto
        # ONE placeholder — the mapping key is the casefolded name, not an
        # identity. This is the documented fusion trade-off: a fresh
        # detection of an already-known name never mints a second binding.
        tenant = SimpleNamespace(pii_entity_map={"[PERSON_1]": {"name": "Priya Raman"}}, pii_denylist={})
        session = RedactionSession(tenant=tenant)
        text = "My cousin, also named Priya Raman, is visiting next week"
        start = text.index("Priya Raman")
        hit = [DetectedEntity("PERSON", start, start + len("Priya Raman"), 0.95)]
        with patch("apps.pii.redactor._detect_pii", return_value=hit):
            out = session.redact(text)
        self.assertIn("[PERSON_1]", out)
        self.assertNotIn("[PERSON_2]", out)
        # No NEW mint — session.entity_map carries only fresh mints, and
        # reusing a known placeholder is not one (see reference on
        # RedactionSession seeding).
        self.assertEqual(session.entity_map, {})

    def test_distinct_surface_form_does_not_fuse(self):
        # Contrast case: a name that does NOT casefold-match anything in the
        # map mints its OWN placeholder rather than fusing — fusion only
        # happens on an exact (casefolded) string match, never a fuzzy one.
        tenant = SimpleNamespace(pii_entity_map={"[PERSON_1]": {"name": "Priya Raman"}}, pii_denylist={})
        session = RedactionSession(tenant=tenant)
        text = "Priya R. is joining the call"
        hit = [DetectedEntity("PERSON", 0, len("Priya R."), 0.95)]
        with patch("apps.pii.redactor._detect_pii", return_value=hit):
            out = session.redact(text)
        self.assertIn("[PERSON_2]", out)
        self.assertNotIn("[PERSON_1]", out)
        self.assertEqual(session.entity_map, {"[PERSON_2]": "Priya R."})
