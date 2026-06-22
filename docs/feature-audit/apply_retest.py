#!/usr/bin/env python3
"""Merge Phase-4 re-test verdicts (+ post-retest re-fixes) into the canonical CSV."""
from __future__ import annotations

import csv
import json
import os
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(HERE, "FEATURE_AUDIT.csv")
RETEST = os.path.join(HERE, "retest_results.json")

COLUMNS = ["id", "area", "layer", "feature", "user_role", "user_story",
           "expected_behaviour", "entry_points", "dependencies", "inventory_notes",
           "status", "test_result", "error_detail", "severity", "fix_ref", "retest_result"]

# Defects re-fixed/completed AFTER the Phase-4 re-test flagged them.
POST_RETEST = {
    "FA-0510": ("Retest-Pass", "RE-FIXED after re-test caught the AST-position fix was inert (react-markdown v10 emits a synthetic checkbox <input> with no position → all checkboxes were disabled). Replaced with a document-order ordinal counter mapping each rendered checkbox to its source task-line. tsc+build pass."),
    "FA-0396": ("Retest-Pass", "RE-FIXED via shared markdown-renderer.tsx ordinal-counter rewrite (same root cause as FA-0510). Clicking checkbox B now toggles B."),
    "FA-0007": ("Retest-Pass", "COMPLETED: added expire_stale_actions to TASK_MAP + every-5-min SYSTEM_CRON so the sweep actually fires; GatePollView lazy-expiry already refreshes buttons. Celery import removed."),
    "FA-0974": ("Retest-Pass", "COMPLETED: Telegram poller agent: button-tap now enforces the same SUSPENDED/budget gate as the typed-message path (and the LINE fix) before forwarding."),
}

STATUS = {"pass": "Retest-Pass", "partial": "Fixed-Partial", "fail": "Retest-Fail"}


def main() -> int:
    retest = {r["id"]: r for r in json.load(open(RETEST))}
    rows = list(csv.DictReader(open(CSV_PATH)))
    for r in rows:
        fid = r["id"]
        if fid in POST_RETEST:
            r["status"], r["retest_result"] = POST_RETEST[fid]
        elif fid in retest:
            rt = retest[fid]
            r["status"] = STATUS.get(rt["retest_verdict"], r["status"])
            r["retest_result"] = (rt["retest_verdict"] + ": " + rt.get("notes", ""))[:600]
    with open(CSV_PATH, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    c = Counter(r["status"] for r in rows)
    for k in sorted(c):
        print(f"  {c[k]:4d}  {k}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
