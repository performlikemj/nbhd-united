# Handback: Datebook Gate Expiry Clamp

## Outcome

- Datebook gate expiry is now `max(now + 5 minutes, min(now + 24 hours, earliest dated item target))`.
- The gate derives the earliest target from every canonicalized calendar/reminder item in the batch; undated items do not participate, and an all-undated batch retains the 24-hour window.
- A gate clamped by an item target reports that the item's time passed before approval. Ordinary 24-hour expiry keeps its existing narration.
- Pending actions return the stored clamped `expires_at` unchanged.
- QStash sweeping expires naturally clamped gates and preserves the target-passed reason even when the sweep runs later.

## Implementation

- `apps/datebook/gate.py`: authoritative target derivation, expiry calculation, expiry-cause classification, and narration.
- `apps/datebook/runtime_views.py`: reuses the gate's canonical target helper.
- `apps/actions/tasks.py` and `apps/actions/views.py`: delegate lapse classification to the datebook gate.
- `apps/datebook/test_approval_ux.py`: clamp, narration, legacy 24-hour, and sweep coverage.
- `apps/datebook/test_b2c.py`: exact pending `expires_at` passthrough coverage.

No model or migration changes were required.

## Verification

- Focused datebook gate set: PASS (15 tests).
- `.venv/bin/python manage.py test apps.datebook apps.actions --noinput`: PASS (170 tests).
- Ruff format/checks for all touched Python files: PASS.
- `make docker-gate`: PASS.
  - Backend: 7,851 tests passed.
  - Frontend: lint passed with four existing warnings and zero errors; production build passed.

## Scope and Delivery

- `runtime/openclaw/plugins/**` and `apps/datebook/envelope.py` were not changed.
- Tests use mocks only; no external calls were added.
- No push or PR was performed.
- Commit message: `fix(datebook): gate expiry clamps to the earliest item due time — approvals never outlive their moment`
