#!/usr/bin/env python3
"""Merge Phase-3 fix dispositions (+ orchestrator mop-up) into the canonical CSV."""
from __future__ import annotations

import csv
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(HERE, "FEATURE_AUDIT.csv")
DISP = os.path.join(HERE, "fix_dispositions.json")

COLUMNS = ["id", "area", "layer", "feature", "user_role", "user_story",
           "expected_behaviour", "entry_points", "dependencies", "inventory_notes",
           "status", "test_result", "error_detail", "severity", "fix_ref", "retest_result"]

# Orchestrator cross-cluster mop-up: defects whose final disposition differs
# from the per-cluster agent's (it was blocked by file-ownership constraints).
OVERRIDE = {
    "FA-0913": ("Fixed", "mop-up: wired redact_user_message into LINE + iOS ingestion (Telegram via C01) — all 3 channels redact inbound PII before it reaches the LLM"),
    "FA-0914": ("Fixed", "mop-up: inbound PII redaction wired across Telegram/LINE/iOS"),
    "FA-0023": ("Fixed", "mop-up: added every-minute run-due-automations SYSTEM_CRON + C03 TASK_MAP — automations now fire on schedule"),
    "FA-0586": ("Fixed", "mop-up: fuel runtime_views today→today_in_tenant_tz(tenant)"),
    "FA-0081": ("Fixed", "sibling cluster: tenants runtime_views _state() now uses _get_allowed_models(tenant)"),
    "FA-0007": ("Fixed", "C54 dropped dead celery import + mop-up: GatePollView lazy-expiry now calls update_gate_message to clear stale buttons"),
    "FA-0929": ("Fixed", "mop-up: removed unreachable DATE_OF_BIRTH from starter PII policy"),
    "FA-0536": ("Fixed", "mop-up: created_count counts only rows written + tests updated; monthly scheduling deferred (dormant, overlaps snapshot-gravity-weekly)"),
    "FA-0334": ("Fixed", "workout-detail cluster owner: unit-derived key remounts NumericInput"),
    "FA-0396": ("Fixed", "C27 fixed shared markdown-renderer.tsx (same root cause as FA-0510): checkbox now maps to its AST source line, so the clicked box is the one toggled"),
    "FA-0006": ("Fixed-Partial", "C33 interim guard: no silent expiry (clear warning when no Telegram/LINE channel). Full iOS gate path (APNs gate push + app approve/deny) = feature follow-up"),
    "FA-0335": ("Fixed-Partial", "C59 a11y hardening done; cross-device unit persistence (backend field+migration) = feature follow-up"),
    "FA-0494": ("Fixed", "C22 removed nested-anchor; logo now links to / (was /journal) — brand-logo asLink prop is a minor follow-up"),
}

STATUS = {"fixed": "Fixed", "partial": "Fixed-Partial", "noop": "Wont-Fix"}


def main() -> int:
    disp = {d["id"]: d for d in json.load(open(DISP))}
    rows = list(csv.DictReader(open(CSV_PATH)))
    n = 0
    for r in rows:
        fid = r["id"]
        if fid in OVERRIDE:
            r["status"], r["fix_ref"] = OVERRIDE[fid]
            n += 1
        elif fid in disp:
            d = disp[fid]
            r["status"] = STATUS.get(d["action"], r["status"])
            ref = f"{d['_cluster']}: {d.get('summary', '')}"
            if d["action"] != "fixed" and d.get("noop_or_partial_reason"):
                ref += f" | {d['noop_or_partial_reason']}"
            r["fix_ref"] = ref[:600]
            n += 1
    with open(CSV_PATH, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    from collections import Counter
    c = Counter(r["status"] for r in rows)
    print(f"updated {n} rows")
    for k in sorted(c):
        print(f"  {c[k]:4d}  {k}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
