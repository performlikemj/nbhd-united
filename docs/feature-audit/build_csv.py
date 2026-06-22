#!/usr/bin/env python3
"""Deterministic builder for the canonical feature-audit spreadsheet.

Reads features_raw.json (list of feature dicts emitted by the inventory
workflow) and writes/merges the canonical FEATURE_AUDIT.csv.

Canonical CSV is the single source of truth. Status/test columns are
preserved across rebuilds by matching on a stable content key so that
re-running inventory never clobbers test results already recorded.
"""
from __future__ import annotations

import csv
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "features_raw.json")
CSV_PATH = os.path.join(HERE, "FEATURE_AUDIT.csv")

COLUMNS = [
    "id",
    "area",
    "layer",
    "feature",
    "user_role",
    "user_story",
    "expected_behaviour",
    "entry_points",
    "dependencies",
    "inventory_notes",
    "status",          # Inventoried | Tested-Pass | Tested-Fail | Fixed | Retest-Pass | Retest-Fail
    "test_result",     # free text from phase 2
    "error_detail",    # description of the bug/UX issue
    "severity",        # blank | low | medium | high | critical
    "fix_ref",         # commit/file ref from phase 3
    "retest_result",   # free text from phase 4
]


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def content_key(area: str, feature: str) -> str:
    return f"{norm(area)}::{norm(feature)}"


def load_raw() -> list[dict]:
    with open(RAW, encoding="utf-8") as fh:
        data = json.load(fh)
    # Accept either {"features":[...]} or a bare list
    if isinstance(data, dict) and "features" in data:
        data = data["features"]
    return data


def load_existing() -> dict[str, dict]:
    if not os.path.exists(CSV_PATH):
        return {}
    out = {}
    with open(CSV_PATH, encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            out[content_key(row.get("area", ""), row.get("feature", ""))] = row
    return out


def main() -> int:
    raw = load_raw()
    existing = load_existing()

    # Dedup raw inventory by content key (some areas overlap, e.g. orchestrator/tenants).
    seen: dict[str, dict] = {}
    order: list[str] = []
    for f in raw:
        area = f.get("area", "")
        name = f.get("name") or f.get("feature") or ""
        key = content_key(area, name)
        if key in seen:
            # merge entry_points / notes from the duplicate
            ep = seen[key].get("_entry_points", [])
            for x in (f.get("entry_points") or []):
                if x not in ep:
                    ep.append(x)
            seen[key]["_entry_points"] = ep
            continue
        seen[key] = {
            "area": area,
            "layer": f.get("layer", ""),
            "feature": name,
            "user_role": f.get("user_role", ""),
            "user_story": f.get("user_story", ""),
            "expected_behaviour": f.get("expected_behaviour", ""),
            "_entry_points": list(f.get("entry_points") or []),
            "dependencies": f.get("dependencies", ""),
            "inventory_notes": f.get("notes", ""),
        }
        order.append(key)

    # Stable sort: group by area (alpha), preserve discovery order within area.
    pos = {k: i for i, k in enumerate(order)}
    order.sort(key=lambda k: (seen[k]["area"], pos[k]))

    rows = []
    for i, key in enumerate(order, start=1):
        rec = seen[key]
        prev = existing.get(key, {})
        rows.append({
            "id": f"FA-{i:04d}",
            "area": rec["area"],
            "layer": rec["layer"],
            "feature": rec["feature"],
            "user_role": rec["user_role"],
            "user_story": rec["user_story"],
            "expected_behaviour": rec["expected_behaviour"],
            "entry_points": " ; ".join(rec["_entry_points"]),
            "dependencies": rec["dependencies"],
            "inventory_notes": rec["inventory_notes"],
            "status": prev.get("status") or "Inventoried",
            "test_result": prev.get("test_result", ""),
            "error_detail": prev.get("error_detail", ""),
            "severity": prev.get("severity", ""),
            "fix_ref": prev.get("fix_ref", ""),
            "retest_result": prev.get("retest_result", ""),
        })

    with open(CSV_PATH, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)

    # quick stats
    by_area: dict[str, int] = {}
    for r in rows:
        by_area[r["area"]] = by_area.get(r["area"], 0) + 1
    print(f"Wrote {len(rows)} features to {CSV_PATH}")
    for a in sorted(by_area):
        print(f"  {by_area[a]:3d}  {a}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
