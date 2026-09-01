from django.test import SimpleTestCase

from apps.pii.redactor import DetectedEntity
from apps.pii.span_manifest import _canonical_detected, _canonical_raw_spans, _load_corpus


class SpanManifestTests(SimpleTestCase):
    def test_corpus_is_stably_sorted_and_combines_golden_with_eval(self):
        corpus = _load_corpus("golden+eval")

        keys = [(row["source"], row["id"], row["text"]) for row in corpus]
        self.assertEqual(keys, sorted(keys))
        self.assertIn("golden", {row["source"] for row in corpus})
        self.assertIn("eval", {row["source"] for row in corpus})

    def test_raw_spans_are_canonical_and_sorted(self):
        spans = [
            {"entity_group": "Z", "word": "later", "score": 1, "start": 5, "end": 10},
            {"entity_group": "A", "word": "first", "score": 0.5, "start": 0, "end": 5},
        ]

        self.assertEqual(
            _canonical_raw_spans(spans),
            [
                {"end": 5, "entity_group": "A", "score": 0.5, "start": 0, "word": "first"},
                {"end": 10, "entity_group": "Z", "score": 1.0, "start": 5, "word": "later"},
            ],
        )

    def test_detected_spans_preserve_source_and_matched_text(self):
        text = "call example@example.com"
        spans = [
            DetectedEntity("PERSON", 5, 12, 1.0),
            DetectedEntity("EMAIL_ADDRESS", 5, 24, 0.8, source="presidio"),
        ]

        self.assertEqual(
            _canonical_detected(spans, text),
            [
                {
                    "end": 12,
                    "entity_type": "PERSON",
                    "score": 1.0,
                    "source": "neural",
                    "start": 5,
                    "text": "example",
                },
                {
                    "end": 24,
                    "entity_type": "EMAIL_ADDRESS",
                    "score": 0.8,
                    "source": "presidio",
                    "start": 5,
                    "text": "example@example.com",
                },
            ],
        )
