#!/usr/bin/env python3
"""Merge Phase-2 test + adversarial-verify results into the canonical CSV.

Reads the feature-test workflow output JSON, updates FEATURE_AUDIT.csv
(status/test_result/error_detail/severity per FA id), and writes
DEFECTS.json — the confirmed-real defect backlog for Phase 3.
"""
from __future__ import annotations

import csv
import json
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(HERE, "FEATURE_AUDIT.csv")
DEFECTS = os.path.join(HERE, "DEFECTS.json")

COLUMNS = ["id", "area", "layer", "feature", "user_role", "user_story",
           "expected_behaviour", "entry_points", "dependencies", "inventory_notes",
           "status", "test_result", "error_detail", "severity", "fix_ref", "retest_result"]


def find_result(o):
    if isinstance(o, dict):
        if isinstance(o.get("chunks"), list) and "testedCount" in o:
            return o
        for v in o.values():
            r = find_result(v)
            if r is not None:
                return r
    elif isinstance(o, list):
        for v in o:
            r = find_result(v)
            if r is not None:
                return r
    return None


def main(out_path: str) -> int:
    data = json.load(open(out_path))
    result = find_result(data)
    if not result:
        print("ERROR: could not locate result.chunks", file=sys.stderr)
        return 1

    test_by_id: dict[str, dict] = {}
    verify_by_id: dict[str, dict] = {}
    for c in result["chunks"]:
        for r in c.get("results", []):
            test_by_id[r["id"]] = r
        for v in c.get("verified", []):
            vr = v.get("verify")
            if vr:
                verify_by_id[v["id"]] = vr

    rows = list(csv.DictReader(open(CSV_PATH)))
    by_id = {r["id"]: r for r in rows}

    defects = []
    stats = Counter()
    for fid, row in by_id.items():
        t = test_by_id.get(fid)
        if not t:
            row["status"] = "Untested"
            stats["untested"] += 1
            continue
        if t["verdict"] == "pass":
            row["status"] = "Tested-Pass"
            row["test_result"] = "pass"
            row["error_detail"] = ""
            row["severity"] = ""
            stats["pass"] += 1
            continue
        # flagged (fail/concern) -> consult adversarial verify
        v = verify_by_id.get(fid)
        if v is None:
            # verifier died/absent — keep flagged, needs manual review
            row["status"] = "Tested-Fail"
            row["test_result"] = f"flagged ({t['verdict']}), verify-missing"
            row["error_detail"] = t.get("error_detail", "")
            row["severity"] = t.get("severity", "") or "low"
            stats["fail_unverified"] += 1
            defects.append(_defect(row, t, None))
            continue
        if v.get("is_real"):
            row["status"] = "Tested-Fail"
            row["test_result"] = f"confirmed defect ({v.get('category','')}/{v.get('severity','')})"
            row["error_detail"] = v.get("confirmed_detail", t.get("error_detail", ""))
            row["severity"] = v.get("severity", "") or t.get("severity", "") or "low"
            stats["fail_confirmed"] += 1
            stats[f"sev_{row['severity']}"] += 1
            defects.append(_defect(row, t, v))
        else:
            row["status"] = "Tested-Pass"
            row["test_result"] = "flagged then cleared by adversarial review"
            row["error_detail"] = ""
            row["severity"] = ""
            stats["false_positive"] += 1

    with open(CSV_PATH, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(by_id.values())

    # sort defects: severity desc, then complexity asc
    sev_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3, "": 4}
    cx_rank = {"trivial": 0, "small": 1, "moderate": 2, "large": 3, "": 4}
    defects.sort(key=lambda d: (sev_rank.get(d["severity"], 4), cx_rank.get(d["fix_complexity"], 4), d["id"]))
    json.dump(defects, open(DEFECTS, "w"), indent=1, ensure_ascii=False)

    print(f"tested={result.get('testedCount')} flagged={result.get('flaggedCount')} confirmed={result.get('confirmedCount')}")
    print("--- merge stats ---")
    for k in sorted(stats):
        print(f"  {stats[k]:4d}  {k}")
    print(f"--- {len(defects)} defects -> DEFECTS.json ---")
    by_area = Counter(d["area"] for d in defects)
    for a in sorted(by_area):
        print(f"  {by_area[a]:3d}  {a}")
    return 0


def _defect(row, t, v):
    return {
        "id": row["id"],
        "area": row["area"],
        "layer": row["layer"],
        "feature": row["feature"],
        "severity": row["severity"],
        "category": (v or t).get("category", ""),
        "detail": row["error_detail"],
        "suggested_fix": (v or {}).get("suggested_fix", ""),
        "fix_complexity": (v or {}).get("fix_complexity", ""),
        "evidence": (v or {}).get("evidence", t.get("evidence", [])),
        "entry_points": row["entry_points"],
        "expected_behaviour": row["expected_behaviour"],
        "verified": v is not None,
    }


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
