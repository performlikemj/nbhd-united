"""``eval_smoke`` — the trivial suite that proves the chassis end-to-end.

One always-pass, fully deterministic case. It exists so that open_run -> record
-> close_run can be fired in PRODUCTION (via the QStash trigger) and observed to
write real rows and emit the summary line, BEFORE any real suite depends on the
chassis. It touches no tenant, no container, no model — so a failure here means
the chassis (or the DB) is broken, never that a probe is flaky.

Safe to re-fire anytime: each run writes its own EvalRun + EvalResult rows.
"""

from __future__ import annotations

import time

from apps.evals.models import EvalResult, EvalRun
from apps.evals.runner import record, record_run

SUITE = "eval_smoke"
CASE_ID = "chassis_writes_rows"


def run_smoke_suite(*, trigger: str = EvalRun.Trigger.MANUAL) -> EvalRun:
    """Run the chassis smoke and return the CLOSED run.

    Uses ``record_run`` so the run is always closed — a crash mid-suite becomes
    an ``error`` run, never a stranded ``running`` one.
    """
    # image_tag=None: the smoke exercises no container, so the run stores NULL
    # rather than pinning an OpenClaw tag it never touched.
    with record_run(SUITE, trigger, image_tag=None) as run:
        started = time.monotonic()
        # Deterministic, always-pass. `details` carries a duration only — counts /
        # ids / durations, never content (INVARIANT #1). Kind.SMOKE keeps this row
        # out of the deterministic-corpus aggregation.
        record(
            run,
            CASE_ID,
            EvalResult.Kind.SMOKE,
            passed=True,
            details={"duration_ms": int((time.monotonic() - started) * 1000)},
        )

    return run
