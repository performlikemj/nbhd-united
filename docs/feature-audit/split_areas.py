#!/usr/bin/env python3
"""Split FEATURE_AUDIT.csv into per-area chunk files for Phase-2 testing.

Emits docs/feature-audit/areas/<chunk_id>.json (each a list of feature dicts
with the canonical FA-NNNN ids) and a manifest.json describing the chunks.
"""
from __future__ import annotations

import csv
import json
import os
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(HERE, "FEATURE_AUDIT.csv")
AREAS = os.path.join(HERE, "areas")
CHUNK = 18

FIELDS = ["id", "area", "layer", "feature", "user_role", "user_story",
          "expected_behaviour", "entry_points", "dependencies", "inventory_notes"]


def main() -> int:
    os.makedirs(AREAS, exist_ok=True)
    by_area = defaultdict(list)
    with open(CSV_PATH, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            by_area[row["area"]].append({k: row[k] for k in FIELDS})

    manifest = []
    for area in sorted(by_area):
        feats = by_area[area]
        n_chunks = (len(feats) + CHUNK - 1) // CHUNK
        for ci in range(n_chunks):
            part = feats[ci * CHUNK:(ci + 1) * CHUNK]
            suffix = f"__{ci+1}" if n_chunks > 1 else ""
            chunk_id = f"{area}{suffix}"
            path = os.path.join(AREAS, f"{chunk_id}.json")
            json.dump(part, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            manifest.append({
                "chunk_id": chunk_id,
                "area": area,
                "file": path,
                "ids": [f["id"] for f in part],
                "count": len(part),
            })

    json.dump(manifest, open(os.path.join(AREAS, "manifest.json"), "w"), indent=1)
    print(f"{len(manifest)} chunks, {sum(m['count'] for m in manifest)} features")
    for m in manifest:
        print(f"  {m['count']:3d}  {m['chunk_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
