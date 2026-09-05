from contextlib import redirect_stdout
from dataclasses import dataclass
from io import StringIO
from pathlib import Path

from django.test import SimpleTestCase

from apps.pii.golden_check import _parse_expected_misses, run_golden_check


@dataclass(frozen=True)
class _FakeSpan:
    entity_type: str
    start: int
    end: int


class _FakeDetector:
    def __init__(self, *, misses=()):
        self.misses = set(misses)

    def __call__(self, text):
        if text in self.misses or text == "ordinary words":
            return []
        return [_FakeSpan(entity_type="PERSON", start=0, end=len(text))]


class GoldenCheckAllowlistTests(SimpleTestCase):
    golden = [
        {
            "id": "control-alice",
            "text": "Alice",
            "expect": [{"span": "Alice", "type": "PERSON"}],
        },
        {
            "id": "control-bob",
            "text": "Bob",
            "expect": [{"span": "Bob", "type": "PERSON"}],
        },
        {"id": "clean-ordinary", "text": "ordinary words", "expect": "clean"},
    ]

    def _run(self, *, misses=(), expected=()):
        output = StringIO()
        with redirect_stdout(output):
            status = run_golden_check(
                self.golden,
                expected_misses=set(expected),
                redacted_spans=_FakeDetector(misses=misses),
            )
        return status, output.getvalue()

    def test_empty_allowlist_passes_when_every_phrase_passes(self):
        status, output = self._run()

        self.assertEqual(status, 0)
        self.assertIn("PII golden failing ids: {}", output)
        self.assertIn("PII golden expected miss ids: {}", output)
        self.assertIn("PII golden-set OK: 3/3 phrases pass", output)

    def test_exact_expected_miss_set_passes(self):
        status, output = self._run(misses={"Alice"}, expected={"control-alice"})

        self.assertEqual(status, 0)
        self.assertIn("PII golden failing ids: {'control-alice'}", output)
        self.assertIn("PII golden expected miss ids: {'control-alice'}", output)
        self.assertIn("PII golden-set OK: 2/3 phrases pass", output)

    def test_extra_miss_fails_with_drift_message(self):
        status, output = self._run(misses={"Alice"})

        self.assertEqual(status, 1)
        self.assertIn("golden drift: unexpected pass {} / unexpected miss {'control-alice'}", output)

    def test_listed_id_that_passes_fails_with_drift_message(self):
        status, output = self._run(expected={"control-alice"})

        self.assertEqual(status, 1)
        self.assertIn("golden drift: unexpected pass {'control-alice'} / unexpected miss {}", output)

    def test_drift_in_both_directions_is_reported_deterministically(self):
        status, output = self._run(misses={"Bob"}, expected={"control-alice"})

        self.assertEqual(status, 1)
        self.assertIn(
            "golden drift: unexpected pass {'control-alice'} / unexpected miss {'control-bob'}",
            output,
        )

    def test_expected_miss_env_parsing_trims_and_deduplicates(self):
        self.assertEqual(
            _parse_expected_misses(" control-bob,control-alice,,control-bob "),
            {"control-alice", "control-bob"},
        )

    def test_ci_runs_both_engines_and_sources_expected_misses_once(self):
        workflow = (Path(__file__).parents[2] / ".github/workflows/ci-cd.yml").read_text()
        known_misses = "fuel-catalog-control-002,fuel-catalog-control-004,control-018"

        self.assertEqual(workflow.count(known_misses), 1)
        self.assertIn("-e PII_DETECTOR_ENGINE=deberta", workflow)
        self.assertIn("-e PII_DETECTOR_ENGINE=liquid", workflow)
        self.assertIn(
            "-e PII_GOLDEN_EXPECTED_MISSES=${{ env.PII_GOLDEN_EXPECTED_MISSES }}",
            workflow,
        )
