# OpenClaw 2026.9.4 — USER.md cap and cron parameter carry

## Delivery status

Implemented in `fix/openclaw-9-4-usermd-and-cron-sync`, continuing the saved work, based on `71f0ca40`. **Not committed and not image-build verified: sandbox permissions block both Git's worktree index and the Docker socket.** The requested two commits remain outstanding. No push, PR, deployment, production access, persistent DB access/mutations, or escalation-timer implementation occurred. Latency implementation files were not edited.

Passed: 31 Node tests; 15 database-free Django tests; Ruff formatting and repository-wide lint; migration drift check; full-package patch/idempotence and imported-helper smoke check. The exact Docker RUN step must still pass on a machine with Docker access before rollout.

## Root causes and implementation

9.4 clamps USER.md to 4,000 characters after resolving the configurable budgets. Our 26,000/80,000 settings cannot override it; 5.28 had no USER-specific cap. The container cron adapter independently omitted typed parameters and the enforcement description when constructing CLI arguments.

| Changed file:line | Change |
| --- | --- |
| `runtime/openclaw/patches/user-bootstrap-cap.mjs:8` | Pins the exact 9.4 standalone bootstrap, worker, and analyzer filenames. |
| `runtime/openclaw/patches/user-bootstrap-cap.mjs:24` | Checks package name/version, inventories all JS bundles containing the cap, asserts exact constants/clamp/export/import aliases, raises both constants to `26e3`, and replaces both analyzer literal predicates with the imported constant. Checks all proposed module syntax before writes, then `node --check`s every resulting file. Reruns are byte-for-byte no-ops. |
| `runtime/openclaw/patches/user-bootstrap-cap.smoke.mjs:7` | Real-helper test: USER.md and SOUL.md each inject the entire 9,500-character Unicode-containing input without warnings; lower per-file and aggregate budgets still truncate. CLI entry point imports the installed helper and real dependencies. |
| `runtime/openclaw/patches/user-bootstrap-cap.test.mjs:51` | Executes actual standalone and worker helper code with upstream string/UTF-16 helpers; compares non-USER behavior before/after; tests analyzer diagnostics, idempotence, drift refusal, and unexpected bundle copies. Corrected the saved analyzer test fixture to declare `hasTruncation: true`. |
| `Dockerfile.openclaw:45` | Copies the patch tools and runs patch + imported-helper smoke immediately after global OpenClaw installation. |
| `apps/orchestrator/test_openclaw_9_4_migration.py:72` | Retains the saved regression proving 26,000/80,000 budgets survive migration. No config production code changed. |
| `runtime/openclaw/nbhd-cron-sync.mjs:100` | Builds individual CLI flags for the supported agentTurn parameters and top-level description; applies existing safety validation at the mapper boundary too. Refuses explicit empty tool lists because the CLI would widen them to default tools. |
| `runtime/openclaw/nbhd-cron-sync.mjs:176` | Adds `sameCron()` comparing the CLI-applicable projection, including model, ordered fallbacks, timeout, tools, light context, and contract. Ignores runtime state/interval anchors and normalizes the default wake mode and runtime-owned default tools. |
| `runtime/openclaw/nbhd-cron-sync.mjs:204` | Retains full listed jobs for comparison. Reconcile lists first, adds missing/changed jobs, skips unchanged jobs, preserves existing desired jobs after failed adds, and limits removals to nbhd declarations. List failure causes no mutations. |
| `runtime/openclaw/nbhd-cron-sync.test.mjs:120` | Field/absence mappings; real pinned flag registration; field-only changes and removals; fallback order; reconcile upsert/skip/retry; namespace, signature, and shell-payload rejection tests. |
| `apps/cron/test_share_cron_sync.py:30` | Database-free Django test runs the real typed pre-save receiver, row rendering, signing, and writer on pure-reminder and task-hygiene rows, with and without fallback stamps. Checks full payload, contract including limits, declaration key and HMAC. Only ORM reads and share upload are mocked. |

**Checkout discrepancy:** this branch originally had no `sameCron()` and upserted all jobs on every pass. The comparator and its reconcile use were added here to satisfy the reviewer's field-aware diff requirement. No changes to polling cadence or poll-to-push architecture were made.

`ALLOWED_PAYLOAD_KINDS` and `DENY_FIELD_RE` are byte-identical to HEAD. The allowlist remains `{agentTurn, systemEvent}`. No shell/command/script payload flags are added; spawning still uses `execFile`, not a shell. `--json` is used only for list output.

## CLI verification and gaps

Both local copies have the same pinned `dist/cron-cli-BTI9dsDQ.mjs`: `/tmp/openclaw-src-9.4/package/` and `/Users/mjjones/oc94-probe/`. Flag registrations are at line 421; list parsers at 231/235; agentTurn payload mapping at 788–797; description at 831 and in the add parameters at 852. No online lookup was needed because the requested exact version's source is local.

| Desired field | Individual cron add flag | Carry status |
| --- | --- | --- |
| `payload.model` | `--model <model>` | Carried when present. |
| `payload.fallbacks` | `--fallbacks <list>` | Ordered comma-separated list; explicit `[]` becomes an empty argument and remains an empty override in the CLI parser. Absent stays omitted. |
| `payload.timeoutSeconds` | `--timeout-seconds <n>` | Carried; upstream validates a positive integer. |
| `payload.toolsAllow` | `--tools <list>` | Nonempty lists carried. Explicit empty lists are refused; see below. |
| `payload.lightContext: true` | `--light-context` | Carried, restoring pure_reminder's lightweight request. |
| `payload.lightContext: false` | None on **add** | Cannot be preserved as explicit false through this add-only adapter. |
| Top-level `description` / `nbhd.v1` contract | `--description <text>` | Entire string carried as one argument, including enforcement JSON/limits. There is no separate contract field to invent. |
| `agentId` | `--agent <id>` | Existing forwarding retained. |

**Explicit false is a real add CLI gap.** `--no-light-context` is registered only for `cron edit` (line 1049), not `cron add`. The add serializer emits `lightContext: opts.lightContext === true ? true : void 0` (795). Therefore false emits no flag and compares as absent. A true→false change is detected and the declarative upsert replaces the payload without true, but this does not persist explicit false. Multiline typed full-context jobs retain ordinary full bootstrap behavior; command-style single-line prompts could still trigger upstream's automatic lightweight heuristic. Explicit false requires a separate operator edit or upstream CLI support; this implementation does not invent an add flag or expand scope into edit sequencing.

**Empty tools are another CLI gap.** `parseCronToolsAllow` returns undefined for an empty list, which cannot mean “allow no tools.” The mapper refuses such a declaration instead of broadening it. Current typed jobs all have nonempty allowlists. Removing an explicit toolsAllow field also cannot clear a previously installed allowlist via add: upstream `applyDeclarativeJobSpec` preserves previous tools when the field is omitted (`dist/list-snapshot-revision-Dgg3Ai6M.mjs:1265`). Clearing that policy requires the operator edit path (`--clear-tools`). These restrictions are not hidden by the comparator.

Django production changes were unnecessary: `apps/cron/signals.py:134` derives the full payload and `:162` writes the contract into description; `apps/orchestrator/cron_reconcile.py:412` copies row.data and removes only gateway metadata; `apps/cron/share_cron_sync.py:38,70,95` reads that shape, stamps declaration keys, signs, and writes it. The new test verifies all this without a DB. Fallback stamping is synthetic test input only; no escalation logic was added.

## Verification commands and verbatim tails

Node 24.19.0. Commands below were run in this worktree. Logs are local `/tmp/usermd-fix-*.log` files.

### Node tests and syntax

```sh
node --check runtime/openclaw/nbhd-cron-sync.mjs
node --test runtime/openclaw/patches/user-bootstrap-cap.test.mjs runtime/openclaw/nbhd-cron-sync.test.mjs
```

Syntax check exited 0 with no output. Node test tail:

```text
[nbhd:cron-sync] WARN crons file signature INVALID — ignoring (not written by Django)
✔ reconcileOnce: signature and kind controls hold; removals stay in nbhd namespace (0.787292ms)
[nbhd:cron-sync] WARN cron list failed, skipping reconcile: synthetic list failure
✔ reconcileOnce: list failure makes no mutations (0.506959ms)
✔ pinned 9.4 source registers each emitted flag; negative light context is edit-only (0.308416ms)
✔ real 9.4 bundles: full USER.md, unchanged non-USER files, budgets, idempotence (3030.460333ms)
✔ fails before any write on changed version (117.7385ms)
✔ fails before any write on changed export alias (159.06775ms)
✔ fails before any write on changed worker clamp alias (272.888083ms)
✔ fails before any write on changed diagnostic predicate (844.065666ms)
✔ fails before any write on changed duplicate constant (153.706208ms)
✔ refuses an unexpected bundled copy (78.929917ms)
ℹ tests 31
ℹ suites 0
ℹ pass 31
ℹ fail 0
ℹ cancelled 0
ℹ skipped 0
ℹ todo 0
ℹ duration_ms 4749.758833
```

### Full package patch and runtime import

Copied the **entire** pristine 218 MB 9.4 package to a temporary directory; patched the copy twice and ran `user-bootstrap-cap.smoke.mjs` on it. The complete dist inventory was checked, not just fixtures. Original staged packages were unchanged.

The copy's `node_modules` was a read-only-use symlink to dependencies of locally installed OpenClaw **2026.9.1**. This proves the actual patched 9.4 helper imports and runs on this host with those dependencies; it does **not** establish the exact 9.4 Docker dependency environment or gateway startup.

```text
[user-bootstrap-cap] changed 3 bundles; USER.md cap=26000; node --check passed
[user-bootstrap-cap] changed 0 bundles; USER.md cap=26000; node --check passed
[user-bootstrap-cap] installed runtime loaded; USER.md and SOUL.md inject all 9500 chars without warnings; lower budgets enforced
```

### Python formatting and lint

```sh
.venv/bin/ruff format apps/cron/test_share_cron_sync.py apps/orchestrator/test_openclaw_9_4_migration.py
.venv/bin/ruff check
```

```text
2 files left unchanged
All checks passed!
```

### Targeted Django tests

The worktree's saved `.venv/bin/python` symlink initially failed to import Django; invoking the actual existing parent virtualenv interpreter works. No dependencies were installed or shared virtualenv files changed.

```sh
env DJANGO_SETTINGS_MODULE=config.settings.test DATABASE_URL=sqlite:///:memory:   SECRET_KEY=local-verification-only NBHD_DISABLE_BACKGROUND_THREADS=True AZURE_MOCK=true   /Users/mjjones/Projects/nbhd-united/.venv/bin/python manage.py test   apps.cron.test_share_cron_sync.SignedParameterCarryTest   apps.orchestrator.test_openclaw_9_4_migration.OpenClaw94MigrationTransformTest   apps.orchestrator.test_cron_reconcile_system_jobs --noinput
```

These are SimpleTestCase tests: no test database is created, no real DB is queried or mutated. DB-backed TestCase classes/full suite were not run under the no-DB fence.

```text
...............
----------------------------------------------------------------------
Ran 15 tests in 0.005s

OK
Found 15 test(s).
System check identified no issues (0 silenced).
```

### Migration drift

```sh
env DJANGO_SETTINGS_MODULE=config.settings.development DATABASE_URL=sqlite:///:memory:   SECRET_KEY=local-verification-only NBHD_DISABLE_BACKGROUND_THREADS=True AZURE_MOCK=true   /Users/mjjones/Projects/nbhd-united/.venv/bin/python manage.py makemigrations --check --dry-run
```

```text
No changes detected
```

### Required Docker image build — blocked

```sh
docker build -f Dockerfile.openclaw -t nbhd-openclaw:9.4-usermd-cron-sync .
```

```text
DEPRECATED: The legacy builder is deprecated and will be removed in a future release.
            Install the buildx component to build images with BuildKit:
            https://docs.docker.com/go/buildx/

permission denied while trying to connect to the docker API at unix:///Users/mjjones/.colima/default/docker.sock
time="2026-09-20T17:00:50+09:00" level=error msg="Can't add file /Users/mjjones/Projects/nbhd-united/.claude/worktrees/usermd-fix/.dockerignore to tar: io: read/write on closed pipe"
```

No image was built; the patch RUN step did not execute in Docker. Approval policy is `never`, so this session cannot request expanded Docker access.

### Docker gate — blocked

The script needs no production credentials and creates disposable test containers, but its Docker preflight cannot pass in this sandbox.

```sh
DOCKER_GATE_CACHE=/tmp/usermd-fix-docker-cache bash scripts/docker-gate.sh
```

```text
Docker daemon is unavailable.
```

### Git and remaining commits — blocked

`git diff --check` passes with no output. Explicit-path staging for fix #1 failed before any index update:

```text
fatal: Unable to create '/Users/mjjones/Projects/nbhd-united/.git/worktrees/usermd-fix/index.lock': Operation not permitted
```

The worktree's real Git directory is outside writable roots. No commits could be made, no hooks were bypassed, and no refs were updated. When Git access is available, keep the fixes in these **two separate commits** (commands are handoff instructions, not executed commits):

```sh
git add Dockerfile.openclaw runtime/openclaw/patches/user-bootstrap-cap.mjs runtime/openclaw/patches/user-bootstrap-cap.smoke.mjs runtime/openclaw/patches/user-bootstrap-cap.test.mjs apps/orchestrator/test_openclaw_9_4_migration.py
git commit -m "fix(openclaw): raise USER.md bootstrap cap in every 9.4 bundle"
git add runtime/openclaw/nbhd-cron-sync.mjs runtime/openclaw/nbhd-cron-sync.test.mjs apps/cron/test_share_cron_sync.py REPORT.md
git commit -m "fix(openclaw): carry cron parameters and compare field changes"
```

Preexisting untracked `BUILD_BRIEF.md`, `COMBINED_BRIEF.md`, `MUSTDOS.md`, and `codex_run.pid` are left alone and excluded from those commits.

## Deploy handoff — orchestrator/reviewer only

1. Review the explicit CLI gaps above, finish the two commits, and rerun the exact Docker build plus docker-gate where Docker is available. Require both build-time patch and runtime-import smoke success lines. Do not treat this report's host smoke as a successful image build.
2. The orchestrator/reviewer builds and pushes the **OpenClaw runtime image** from these commits to the normal registry, preserving `OPENCLAW_VERSION=2026.9.4`, and records its immutable digest. The file's author owns the manual canary image roll; no deployment was performed here.
3. Roll the canary tenant onto that digest. Apply/regenerate its config through the normal control-plane workflow, retaining `agents.defaults.bootstrapMaxChars=26000` and `bootstrapTotalMaxChars=80000`. Ensure the desired cron document is regenerated/signed from canonical rows with typed data and the intended fallback stamp. A config-only update cannot install this runtime patch.
4. After the in-container sync, use the operator `openclaw cron list --json` path to inspect the canary declarations. Confirm model, fallback ordering, timeout, nonempty toolsAllow, true lightContext for pure_reminder, and complete description/contract. Confirm a fields-only fallback change reaches the installed declaration on the next sync pass; configuration of model aliases/availability remains the control plane's responsibility.
5. Observe the **next scheduled full-context cron run** (daily briefing/task hygiene), recording its session/run identity. Confirm the `agent/embedded` log no longer contains `workspace bootstrap file USER.md ... (limit 4000); truncating in injected context`. Check that the canary has the expected roughly 9–10K USER.md and sufficient aggregate bootstrap budget. Absence of the warning from a lightweight pure_reminder run alone is not proof of full USER injection. If a warning remains at 26K or a smaller aggregate remainder, inspect the effective per-agent budgets/file lengths; the patch retains normal budget enforcement.
