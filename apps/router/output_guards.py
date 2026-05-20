"""Egress-layer detectors for agent output shape.

These run after rehydrate and before send on every outbound reply. They
log only — no remediation — so we can measure residual rates of failure
modes that AGENTS.md rules are supposed to prevent. If a rate stays high
after a prompt fix, that is the signal to add a stripping / retry layer.

See PR #647 (chart-marker rule restored to AGENTS.md) for the prompt-side
fix this instrumentation is measuring.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Multi-line block-bar pattern: at least two lines containing a run of
# unicode block / shade characters. Excludes inline rhetorical use like
# "filling up ████" because that almost never spans multiple lines.
_MULTILINE_BAR_RE = re.compile(r"(?:^.*[▀-▟░-▓]{4,}.*$\n){2,}", re.MULTILINE)

# Single-line very long bar — catches the rare one-row chart.
_LONG_SINGLE_BAR_RE = re.compile(r"[▀-▟░-▓]{15,}")

# If a chart marker is present, the agent did the right thing — even if
# it also drew ASCII bars (which the extractor strips around). Skip.
_CHART_MARKER_RE = re.compile(r"\[\[chart:")


def detect_ascii_chart_leak(text: str) -> bool:
    """True if outbound text looks like an ASCII bar chart and contains no chart marker.

    Heuristic, not exhaustive. False negatives are acceptable here — the goal
    is to measure roughly how often the prompt rule from #647 fails to land.
    False positives should be rare; the multi-line requirement filters out
    rhetorical use of block characters.
    """
    if not text:
        return False
    if _CHART_MARKER_RE.search(text):
        return False
    if _MULTILINE_BAR_RE.search(text):
        return True
    if _LONG_SINGLE_BAR_RE.search(text):
        return True
    return False


def log_ascii_chart_leak(
    text: str,
    *,
    tenant_id: str | int | None,
    channel: str,
) -> None:
    """Detect + log. Safe to call on every outbound reply.

    Caller passes the rehydrated outbound text. We log a sample (truncated)
    so the team can audit detections without storing full message content.
    """
    try:
        if detect_ascii_chart_leak(text):
            logger.warning(
                "ascii_chart_leak detected",
                extra={
                    "tenant_id": str(tenant_id) if tenant_id is not None else None,
                    "channel": channel,
                    "text_len": len(text),
                    "sample": text[:200],
                },
            )
    except Exception:
        logger.exception("ascii_chart_leak detector raised")
