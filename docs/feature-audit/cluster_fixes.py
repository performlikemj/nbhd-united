#!/usr/bin/env python3
"""Group confirmed defects into FILE-DISJOINT clusters for safe parallel fixing.

Two defects that touch any common source file are placed in the same cluster
(connected components over the defect<->file bipartite graph). Each resulting
cluster's file set is disjoint from every other cluster's, so one fix-agent per
cluster can edit the shared worktree concurrently without file conflicts.
"""
from __future__ import annotations

import json
import os
import re
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DEFECTS = os.path.join(HERE, "DEFECTS.json")
OUT = os.path.join(HERE, "fix_clusters.json")

# Repo-relative source-path patterns.
PATH_RE = re.compile(
    r"(?:apps|config|frontend|runtime|infra|scripts|templates|tests)/[A-Za-z0-9_./\-\[\]]+?\.(?:py|tsx|ts|css|mjs|cjs|js|json|sh|html)"
    r"|(?:startup|entrypoint|manage)\.py"
    r"|startup\.sh|entrypoint\.sh|Dockerfile[A-Za-z0-9.\-]*"
)


def files_for(defect: dict) -> set[str]:
    # Cluster only on files the fix will MODIFY. entry_points names the
    # feature's home file; suggested_fix names the files to change. evidence/
    # detail merely CITE context files and create false bridges, so exclude them.
    blobs = []
    blobs.extend(defect.get("entry_points", "").split(" ; "))
    blobs.append(defect.get("suggested_fix", ""))
    files = set()
    for b in blobs:
        for m in PATH_RE.findall(b or ""):
            files.add(m)
    return files


def main() -> int:
    defects = json.load(open(DEFECTS))
    dfiles = {d["id"]: files_for(d) for d in defects}

    # Union-Find over defects sharing a file.
    parent = {d["id"]: d["id"] for d in defects}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    file_to_def = defaultdict(list)
    for did, fs in dfiles.items():
        for f in fs:
            file_to_def[f].append(did)
    for f, dids in file_to_def.items():
        for i in range(1, len(dids)):
            union(dids[0], dids[i])

    # defects with NO extracted file each become their own singleton cluster
    comps = defaultdict(list)
    for d in defects:
        comps[find(d["id"])].append(d["id"])

    by_id = {d["id"]: d for d in defects}
    clusters = []
    for i, (root, ids) in enumerate(sorted(comps.items(), key=lambda kv: -len(kv[1])), start=1):
        ids_sorted = sorted(ids)
        fileset = sorted(set().union(*[dfiles[x] for x in ids_sorted]) if ids_sorted else set())
        sev = [by_id[x]["severity"] for x in ids_sorted]
        sev_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3, "": 4}
        top = min(sev, key=lambda s: sev_rank.get(s, 4))
        clusters.append({
            "cluster_id": f"C{i:02d}",
            "defect_ids": ids_sorted,
            "files": fileset,
            "count": len(ids_sorted),
            "top_severity": top,
            "areas": sorted({by_id[x]["area"] for x in ids_sorted}),
        })

    # verify disjointness
    seen = {}
    overlap = []
    for c in clusters:
        for f in c["files"]:
            if f in seen:
                overlap.append((f, seen[f], c["cluster_id"]))
            seen[f] = c["cluster_id"]

    json.dump(clusters, open(OUT, "w"), indent=1)
    print(f"{len(clusters)} clusters over {len(defects)} defects")
    print(f"file-overlap violations between clusters: {len(overlap)}")
    for o in overlap[:20]:
        print("  OVERLAP", o)
    print("--- clusters (size desc) ---")
    for c in clusters:
        print(f"  {c['cluster_id']}  n={c['count']:2d}  top={c['top_severity']:8}  files={len(c['files']):2d}  areas={','.join(c['areas'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
