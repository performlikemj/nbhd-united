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

## Fix round 1

- Removed production development/localhost support. The host-installed skill now authorizes only `scripts/e2e/nbhd_e2e_skill.py`, whose runtime command allowlist rejects global flags and all argument shapes outside the fixed production commands.
- Disabled redirects on every HTTP request and made every 3xx a hard, content-free error.
- Added `is_synthetic` and `is_eval_sink` to `/api/v1/tenants/me/`; the universal tenant gate now requires the allowlisted ID, `is_synthetic=true`, and `is_eval_sink=false` after login, stored-token authentication, and refresh. Missing fields fail closed with the required deployment-contract message. This supersedes the earlier note that the serializer did not expose those fields.
- Hardened `allowed-tenants.json` to an exact tenant UUID4/account-email schema and refuse placeholders, symlinks, non-regular files, unexpected keys, duplicate keys, wrong ownership, and group/world-writable files.
- Closed output and argument surfaces: server free strings normalize to closed enums or `other`, timestamps and IDs are shape-checked, login has no account argument, receipt IDs and cursors are bounded, and `pii keep` discovers and intersects only current fixed-fixture bindings.
- Threaded one absolute monotonic deadline through authentication, requests, `Retry-After`, polling, and managed-thread cleanup. Managed creation records the thread ID before response validation, cleans up validation failures, and reports cleanup failure without replacing a primary error.
- Replaced split token items with one atomic JSON Keychain item (`account=credentials`), added migration reads from the former items, and persist rotated refresh tokens with the new access token only after the tenant gate passes.
- Expanded the pure mocked CLI suite to 28 tests covering all nine findings. `ruff check scripts/e2e`, `ruff format --check scripts/e2e`, `py_compile` for every changed Python file, and `git diff --check` pass. The isolated `/tenants/me/` serializer test also passes against a dedicated test database. No real host was contacted.

## Fix round 2

- Disposable-thread deletion now uses a fresh, bounded 15-second monotonic cleanup deadline, including when the command/poll deadline is already exhausted.
- Keychain reads treat only macOS `errSecItemNotFound` (`security` exit 44) as migration eligibility. Other read failures remain content-free `KeychainError` failures without probing legacy items.
- Legacy migration now writes and re-reads the atomic credential pair before attempting deletion of both former items. Both deletions are attempted, and any partial failure is reported explicitly without exposing Keychain output.
- Every `security` read, write, verification, and deletion subprocess receives a timeout capped at 30 seconds and the command's remaining deadline; `TimeoutExpired` becomes a content-free `KeychainError`.
- The pure mocked suite now has 32 passing tests, including cleanup after deadline exhaustion, exact Keychain error classification, bounded subprocess timeout, verified migration cleanup, and partial deletion. Ruff check and format check pass for `scripts/e2e`.
