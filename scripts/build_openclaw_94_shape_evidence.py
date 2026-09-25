"""Build the default-deny manifest solely from recorded successful round trips.

Run after capture with the project Python environment and PYTHONPATH=.
The contract tests independently check every referenced evidence file.
"""

import json
from pathlib import Path

from apps.orchestrator.migration_preservation import declaration_digest, declaration_shape

root = Path(__file__).resolve().parents[1] / "apps/orchestrator"
fixtures = root / "fixtures/openclaw_94_cron_contract"
shapes = {}
for path in sorted(fixtures.rglob("*.json")):
    case = json.loads(path.read_text())
    if not case.get("selected") or not case.get("writerSameCron") or len(case.get("stability", [])) != 2:
        continue
    desired = case["declaration"]
    rows = [r for r in case["list"]["json"]["jobs"] if r.get("declarationKey") == desired["declarationKey"]]
    if len(rows) != 1 or declaration_digest(rows[0], pins={}) != declaration_digest(desired):
        continue
    stable = True
    for poll in case["stability"]:
        observed = (
            [r for r in poll["list"]["json"]["jobs"] if r.get("declarationKey") == desired["declarationKey"]]
            if "json" in poll["list"]
            else [
                r
                for r in json.loads(poll["list"]["stdout"])["jobs"]
                if r.get("declarationKey") == desired["declarationKey"]
            ]
        )
        stable &= poll["reconciliation"] == {"ok": True, "applied": 0, "removed": 0, "skipped": 0}
        stable &= len(observed) == 1 and observed[0]["id"] == rows[0]["id"]
        stable &= len(observed) == 1 and declaration_digest(observed[0], pins={}) == declaration_digest(desired)
    if not stable:
        continue
    shape = declaration_shape(desired)
    key = json.dumps(shape, sort_keys=True)
    entry = shapes.setdefault(key, {"shape": shape, "fixtures": []})
    entry["fixtures"].append(str(path.relative_to(root)))
(root / "cron-proven-shapes.json").write_text(json.dumps({"shapes": list(shapes.values())}, indent=2) + "\n")
print(json.dumps({"accepted_shapes": len(shapes), "reason": "evidence_manifest_built"}))
