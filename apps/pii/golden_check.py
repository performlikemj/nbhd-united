"""Golden-set regression check for the PII detection chain.

Runs the REAL detection + filter chain (``_detect_pii`` → ``_filter_results``)
over ``golden_set.json`` exactly as ``_redact_user_message`` Step 2 does for a
brand-new user (empty ``allow_names`` and ``denylist``), and asserts:

  * every ``clean`` phrase produces ZERO redaction spans (no false positive), and
  * every control span is covered by a detected span (no leak / false negative).

By default, exits 0 only when the whole set passes. Set
``PII_GOLDEN_EXPECTED_MISSES`` to a comma-separated list of golden ids to accept
exactly that miss set; a new miss or a listed id that starts passing exits 1.
Wired into CI (``.github/workflows/ci-cd.yml``) so detector drift is loud before
deploy. The model and code are already baked into the Django image, so this
needs no extra dependencies.

Run locally:
    DJANGO_SETTINGS_MODULE=config.settings.base \
        .venv/bin/python -m apps.pii.golden_check
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

GOLDEN_PATH = Path(__file__).with_name("golden_set.json")


def _ensure_django() -> None:
    """Configure Django if the caller has not already (bare ``python -m`` run)."""
    from django.conf import settings

    if settings.configured:
        return
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.base")
    import django

    django.setup()


def _redacted_spans(text: str):
    """Filtered detection spans for a new user (empty allow_names + denylist)."""
    from apps.pii import redactor as r
    from apps.pii.config import TIER_POLICIES

    policy = TIER_POLICIES["starter"]
    results = r._detect_pii(text, policy["entities"], policy["score_threshold"])
    results = r._filter_results(results, text, set(), denylist={}, tenant=None)
    ph_ranges = [(m.start(), m.end()) for m in r._PLACEHOLDER_RE.finditer(text)]
    return [hit for hit in results if not r._hit_inside_placeholder(hit, ph_ranges)]


def _parse_expected_misses(value: str) -> set[str]:
    return {golden_id.strip() for golden_id in value.split(",") if golden_id.strip()}


def _format_ids(golden_ids: set[str]) -> str:
    return "{" + ", ".join(repr(golden_id) for golden_id in sorted(golden_ids)) + "}"


def run_golden_check(
    golden: list[dict[str, Any]],
    *,
    expected_misses: set[str],
    redacted_spans: Callable[[str], Iterable[Any]],
) -> int:
    """Evaluate one detector and fail unless its miss set matches exactly."""
    failures: list[str] = []
    failing_ids: set[str] = set()
    n_clean = n_control = 0

    for row in golden:
        text, expect = row["text"], row["expect"]
        spans = list(redacted_spans(text))

        if expect == "clean":
            n_clean += 1
            if spans:
                failing_ids.add(row["id"])
                got = [(span.entity_type, text[span.start : span.end]) for span in spans]
                failures.append(
                    f"[{row['id']}] FALSE POSITIVE — clean phrase redacted:\n    text : {text!r}\n    got  : {got}"
                )
            continue

        n_control += 1
        for expected in expect:
            expected_span = expected["span"]
            index = text.find(expected_span)
            covered = index != -1 and any(
                span.start < index + len(expected_span) and index < span.end for span in spans
            )
            if not covered:
                failing_ids.add(row["id"])
                got = [(span.entity_type, text[span.start : span.end]) for span in spans]
                failures.append(
                    f"[{row['id']}] LEAK — control span not redacted:\n"
                    f"    text     : {text!r}\n"
                    f"    expected : {expected_span!r} ({expected['type']})\n"
                    f"    detected : {got}"
                )

    print(f"PII golden failing ids: {_format_ids(failing_ids)}")
    print(f"PII golden expected miss ids: {_format_ids(expected_misses)}")

    unexpected_passes = expected_misses - failing_ids
    unexpected_misses = failing_ids - expected_misses
    if unexpected_passes or unexpected_misses:
        print(
            "golden drift: "
            f"unexpected pass {_format_ids(unexpected_passes)} / "
            f"unexpected miss {_format_ids(unexpected_misses)}"
        )
        if failures:
            print("\n" + "\n\n".join(failures))
        return 1

    total = len(golden)
    passing = total - len(failing_ids)
    print(
        f"PII golden-set OK: {passing}/{total} phrases pass "
        f"({n_clean} clean / {n_control} control; {len(failing_ids)} expected miss(es))"
    )
    if failures:
        print("\nExpected miss details:\n\n" + "\n\n".join(failures))
    return 0


def main() -> int:
    _ensure_django()

    with open(GOLDEN_PATH, encoding="utf-8") as fh:
        golden = json.load(fh)

    expected_misses = _parse_expected_misses(os.environ.get("PII_GOLDEN_EXPECTED_MISSES", ""))
    return run_golden_check(
        golden,
        expected_misses=expected_misses,
        redacted_spans=_redacted_spans,
    )


if __name__ == "__main__":
    sys.exit(main())
