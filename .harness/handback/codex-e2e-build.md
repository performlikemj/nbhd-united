# Codex handback: NBHD E2E harness

## Outcome

- Added the allowlisted production E2E CLI under `scripts/e2e/` with JWT login/refresh, mandatory tenant gating, fixed-host enforcement, adaptive reply polling, disposable thread cleanup, metadata-only output, and fixed synthetic fixtures.
- Added the nine-step smoke. The durable `redaction_confirmed` assertion alone feature-detects an older serializer and reports `SKIPPED`; all mapping, reply, registry, denylist, and cleanup assertions remain strict.
- Added pure mocked unit coverage for tenant fail-closed behavior, 401 refresh/gating, polling boundaries, receipt feature detection, output privacy, fixed-message CLI shape, and Keychain stdin writes.
- Added `docs/agents/e2e.md` and one matching `CLAUDE.md` router row.
- Installed the host skill at `~/.claude/skills/nbhd-e2e/SKILL.md` with a strict command allowlist. `logs` is explicitly unavailable pending the tenant-log-digest lane.

## Safety properties

- Production hosts are limited to the custom API domain and the known legacy Container Apps host. Localhost requires explicit development mode; no arbitrary URL or tenant-id input exists.
- Tokens use macOS Keychain service `org.nbhd.e2e`, accounts `access` and `refresh`. Reads are captured in memory and writes pass the secret to `security ... -w` through stdin, never argv.
- Every login, stored-token authentication, and refresh calls `/api/v1/tenants/me/` and requires the checked-in tenant ID.
- Sending commands build fixture content internally, use a new non-main thread, and delete it in `finally`.
- Reply text and mapping values are inspected only in memory. CLI history, receipts, PII surfaces, and smoke logs emit metadata/counts only; `pii count` prints only an integer.

## Verification

- `repo venv python scripts/e2e/test_e2e_cli.py` — 14 tests passed.
- `repo venv ruff check scripts/e2e` — passed.
- `repo venv ruff format --check scripts/e2e` — passed.
- `repo venv python -m py_compile` for every Python file in `scripts/e2e/` — passed.
- `git diff --check` — passed.
- No repository docs-router validator was found under `checks/`, `scripts/`, or `.github`; skipped as specified.
- Docker gate intentionally not run because no app code was touched.
- No real host was contacted.

## Provisioning handoff

- Replace `REPLACE-AFTER-PROVISION` in `scripts/e2e/allowed-tenants.json` only after the dedicated tenant is provisioned and operator-verified.
- Smoke requires `/api/v1/tenants/me/` to expose `is_synthetic=true` and `is_eval_sink=false`; this checkout's current serializer does not expose those fields, so smoke fails closed until that API contract is available.
- Re-running smoke after its stop-hiding step is not guaranteed to reproduce the initial two-redaction state because the public PII API retires mappings and has no full-reset operation.
- Tenant log digest remains unavailable until commit `860b8982` (or its successor) is merged/deployed and Azure query RBAC is verified.
