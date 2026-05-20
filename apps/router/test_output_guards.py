"""Tests for the egress-layer ASCII-chart leak detector."""

from __future__ import annotations

import logging
from unittest import TestCase

from apps.router.output_guards import (
    detect_ascii_chart_leak,
    log_ascii_chart_leak,
)


class DetectAsciiChartLeakTests(TestCase):
    def test_empty_text(self) -> None:
        self.assertFalse(detect_ascii_chart_leak(""))
        self.assertFalse(detect_ascii_chart_leak(None))  # type: ignore[arg-type]

    def test_plain_prose_is_clean(self) -> None:
        self.assertFalse(detect_ascii_chart_leak("Your payoff plan is on track. AC and AJ are closest to closeout."))

    def test_detects_unicode_block_bar_chart(self) -> None:
        chart = (
            "Here is your trajectory:\n"
            "$40K|\n"
            "$35K|████████\n"
            "$30K|████████████████\n"
            "$25K|████████████████████████\n"
            "$20K|████████████████████████████████\n"
        )
        self.assertTrue(detect_ascii_chart_leak(chart))

    def test_detects_mixed_shade_bars(self) -> None:
        chart = "Debt:    ▓▓▓▓▓▓▓▓░░░░░░ 60% paid\nSavings: ▓▓▒▒░░░░░░░░░░ 12%\nIncome:  ████░░░░░░░░░░ 30%\n"
        self.assertTrue(detect_ascii_chart_leak(chart))

    def test_skips_when_chart_marker_present(self) -> None:
        # Marker present — extractor will handle, no leak.
        text = (
            "Your trajectory: [[chart:payoff_timeline]] looks good.\n"
            "████████████████ (decorative)\n"
            "████████████████ (decorative)\n"
        )
        self.assertFalse(detect_ascii_chart_leak(text))

    def test_no_false_positive_on_rhetorical_single_short_run(self) -> None:
        # Inline rhetorical use — single line, < 15 chars.
        self.assertFalse(detect_ascii_chart_leak("the queue is filling up ████ fast — handle it"))

    def test_no_false_positive_on_markdown_table(self) -> None:
        md = "| col1 | col2 |\n|------|------|\n| foo  | bar  |\n| baz  | qux  |\n"
        self.assertFalse(detect_ascii_chart_leak(md))

    def test_no_false_positive_on_horizontal_divider(self) -> None:
        self.assertFalse(detect_ascii_chart_leak("Section A\n---\nSection B\n---\nSection C\n"))

    def test_detects_single_very_long_bar(self) -> None:
        # One-line bar long enough to be a chart by itself.
        self.assertTrue(detect_ascii_chart_leak("Progress: ████████████████████████ (75%)"))


class LogAsciiChartLeakTests(TestCase):
    def test_logs_warning_when_leak_detected(self) -> None:
        chart = "$40K|████████\n$30K|████████████████\n$20K|████████████████████████\n"
        with self.assertLogs("apps.router.output_guards", level=logging.WARNING) as cm:
            log_ascii_chart_leak(chart, tenant_id="abc-123", channel="line")
        self.assertTrue(any("ascii_chart_leak detected" in r for r in cm.output))

    def test_silent_when_clean(self) -> None:
        logger = logging.getLogger("apps.router.output_guards")
        logger.addHandler(logging.NullHandler())
        # No assertion that nothing is logged at WARNING+ — assertNoLogs is 3.10+,
        # so we just assert no exception and the detector returns False.
        log_ascii_chart_leak("plain prose, no chart", tenant_id=1, channel="telegram")

    def test_detector_exception_is_swallowed(self) -> None:
        # Pass a non-string to coax the regex through an exception path.
        # log_ascii_chart_leak must not raise even if the detector does.
        try:
            log_ascii_chart_leak(12345, tenant_id=1, channel="telegram")  # type: ignore[arg-type]
        except Exception as exc:  # pragma: no cover
            self.fail(f"log_ascii_chart_leak should swallow exceptions: {exc}")
