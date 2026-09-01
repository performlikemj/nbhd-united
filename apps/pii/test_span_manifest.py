import io
import json
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from apps.pii.redactor import DetectedEntity
from apps.pii.span_manifest import _canonical_detected, _canonical_raw_spans, _load_corpus, main


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

    def test_main_emits_canonical_raw_and_final_spans_with_fake_pipeline(self):
        text = "Alice"
        raw_span = {
            "entity_group": "identity.person_name",
            "word": text,
            "score": 1.0,
            "start": 0,
            "end": len(text),
        }
        fake_pipeline = Mock(return_value=[raw_span])
        output = io.StringIO()
        with (
            patch(
                "apps.pii.span_manifest._load_corpus",
                return_value=[{"id": "control-name", "source": "golden", "text": text}],
            ),
            patch("apps.pii.engine.get_pii_pipeline", return_value=fake_pipeline),
            patch("apps.pii.engine.get_pattern_recognizers", return_value={}),
            redirect_stdout(output),
        ):
            self.assertEqual(main(["--corpus", "golden"]), 0)

        rendered = output.getvalue()
        manifest = json.loads(rendered)
        self.assertEqual(
            rendered,
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        )
        expected_case = {
            "detect_pii": [
                {
                    "end": 5,
                    "entity_type": "PERSON",
                    "score": 1.0,
                    "source": "neural",
                    "start": 0,
                    "text": text,
                }
            ],
            "id": "control-name",
            "raw_spans": [raw_span],
            "source": "golden",
            "text": text,
        }
        self.assertEqual(manifest["engines"], {"deberta": [expected_case], "liquid": [expected_case]})
        self.assertEqual(fake_pipeline.call_count, 4)
