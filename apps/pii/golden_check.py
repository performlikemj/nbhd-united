"""Golden-set regression check for the PII detection chain.

Runs the REAL detection + filter chain (``_detect_pii`` → ``_filter_results``)
over ``golden_set.json`` exactly as ``_redact_user_message`` Step 2 does for a
brand-new user (empty ``allow_names`` and ``denylist``), and asserts:

  * every ``clean`` phrase produces ZERO redaction spans (no false positive), and
  * every control span is covered by a detected span (no leak / false negative).

Exits 0 when the whole set passes, 1 with a readable diff otherwise. Wired into
CI (``.github/workflows/ci-cd.yml``) so a change that regresses PII detection
fails before deploy rather than as a silent degrade in prod. The model and code
are already baked into the Django image, so this needs no extra dependencies.

Run locally:
    DJANGO_SETTINGS_MODULE=config.settings.base \
        .venv/bin/python -m apps.pii.golden_check
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

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


def main() -> int:
    _ensure_django()

    with open(GOLDEN_PATH, encoding="utf-8") as fh:
        golden = json.load(fh)

    failures: list[str] = []
    n_clean = n_control = 0

    for row in golden:
        text, expect = row["text"], row["expect"]
        spans = _redacted_spans(text)

        if expect == "clean":
            n_clean += 1
            if spans:
                got = [(s.entity_type, text[s.start : s.end]) for s in spans]
                failures.append(
                    f"[{row['id']}] FALSE POSITIVE — clean phrase redacted:\n    text : {text!r}\n    got  : {got}"
                )
            continue

        n_control += 1
        for exp in expect:
            span = exp["span"]
            idx = text.find(span)
            covered = idx != -1 and any(s.start < idx + len(span) and idx < s.end for s in spans)
            if not covered:
                got = [(s.entity_type, text[s.start : s.end]) for s in spans]
                failures.append(
                    f"[{row['id']}] LEAK — control span not redacted:\n"
                    f"    text     : {text!r}\n"
                    f"    expected : {span!r} ({exp['type']})\n"
                    f"    detected : {got}"
                )

    total = len(golden)
    if failures:
        print(f"PII golden-set FAILED: {len(failures)} problem(s) across {total} phrases\n")
        print("\n\n".join(failures))
        return 1

    print(f"PII golden-set OK: {total} phrases ({n_clean} clean / {n_control} control) all pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
