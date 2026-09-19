# REPORT-S2 — Yuki local TEST stack

2026-09-19, basecamp/JST. Branch `feat/yuki-local-test-stack`, worktree
`/Users/mjjones/worktrees/united-yuki-test`, base `9dee7814` (`origin/main` at
resume). Initial `git status/log` found a clean requested branch and no surviving
S2 changes. Work was continued there; no branch reset or primary-checkout edits.

**Acceptance status: implementation prepared; live tenant/chat/sautai acceptance
is still blocked on external prerequisites. No real chat or sim result is claimed.**

## Delivered

- `deploy/local-test/`: repeatable prerequisite/env installer, repo Compose
  overlay/launcher, clean Django/gateway launcher, owner-only in-memory sautai
  hand-off, signup page using the normal signup API, metadata-only chat proof,
  sandbox policy, and operating instructions.
- `.env.local-test` generated from the example, mode 0600, ignored. Both launchd
  plists generated under ignored `.state`; **neither loaded by Codex**.
- Dedicated Compose Postgres/Redis are up on 127.0.0.1:55441/56381. Database/user
  `nbhd_yuki_test`; migrations completed. No host `postgres16` database used.
- Django targets 127.0.0.1:18080; test gateway targets 127.0.0.1:19443, with HOME,
  OPENCLAW_HOME, OPENCLAW_STATE_DIR and OPENCLAW_CONFIG_PATH in the test home.
- Single guarded URL helper; all tenant gateway URL construction sites routed
  through it. Production and non-synthetic output stays HTTPS.
- Persistent local mock share adapter preserves the normal config validator and
  `_put_share_file` sanitization. Stable mock encryption supports process restart;
  mock key deletion/recovery is explicitly unsupported in this local install.
- Post-signup `prepare_local_test_tenant` uses `provision_tenant` under Azure mock,
  with the fixed install tenant UUID, synthetic/non-sink flags, starter tier,
  $1/100k-token caps, no exemption/credits/Stripe and a 30-day trial.
- Persona import accepts the actual harness v3 manifest, retaining fact IDs and
  citations in the managed USER.md region. All 26 facts and their source digest
  were verified against the canonical harness persona; no facts were invented.
- `prove_local_sautai` drives the installed gateway plugin → runtime view → real
  job/task → `sautai_client` → local sim, with a fresh operator-confirmed fixture
  week and strict ready/result/identity checks. It has not yet been run live.

Full setup, exact commands and hand-off contract:
[`deploy/local-test/README.md`](deploy/local-test/README.md).

## Observed validation

- Django `check`: passed; migrations applied; `makemigrations --check --dry-run`:
  no changes detected.
- Repo-wide Ruff 0.15.21 lint and format checks: passed (1,651 Python files).
- Final targeted regression run: **143 tests passed**, including native gateway
  boot, the real mocked provisioning path, delayed/on-commit dispatch, and
  public-schema lockdown checks. The final runner delegates to the repository
  `scripts/test-local.sh` using `test_nbhd_united_yuki_test_aaeb7c`; existing DBs
  require explicit reuse authorization. Python child processes retain the
  local network guard.
- Generated config passes the **installed OpenClaw 2026.9.1** schema validator.
- Native gateway boot smoke, using a disposable synthetic test DB fixture and
  isolated config under `.state`, passed `/health` inside the network/filesystem
  sandbox. No chat/inference was performed. The process was terminated and
  port 19443 verified free afterward. This is not the actual Yuki tenant proof.
- HTTP smoke: `/health/` returned `status=ok`; `/local-test/signup/` served its
  password form. The owned temporary Django process was stopped; both Django
  18080 and gateway 19443 were verified free afterward.
- DeBERTa copied from basecamp's existing model cache to the test home and loaded
  successfully offline on CPU. No model download/inference GPU job started.
- Both plists pass `plutil -lint`. Seatbelt profile parses successfully.
- Network guard rejects remote hosts, port 10443 and host Postgres 5432. No
  personal gateway health request was made; these were guard-function checks.
- Final `make docker-gate` against implementation commit `90bbe080`:
  **both Linux legs passed**, **9,038 backend tests in 531.544s**, plus frontend
  lint/build. Earlier snapshots also passed (9,034 and 9,036 backend tests).
  The final focused suite passed **7 adapter tests** in 6.271s, including native
  gateway boot, canonical persona import, and the hand-off disconnect regression.
  The actual 26-fact manifest imports with citations; the documented Node
  hand-off adapter passes syntax validation.
- `.state` is excluded from Docker snapshots: the initial attempt encountered
  the live Unix socket and nested snapshot path; it was stopped and restarted
  after fixing the exclusion.


Machine-local logs: `deploy/local-test/.state/{migrate,ruff-check,ruff-format,
local-adapter-tests,gateway-boot-tests,targeted-tests,pii-smoke,docker-gate,docker-gate-final,final-adapter-tests}.log`.
They are ignored, not committed. No password/reply/token values in this report.

## Gateway URL call-site inventory

`apps/orchestrator/gateway_url.py::gateway_base_url` permits HTTP only when
DEBUG is true, environment AZURE_MOCK is exactly true (case-insensitive),
`is_synthetic is True`, and authority is exactly `127.0.0.1:<1..65535>`.
Malformed authorities, alternate addresses, missing tenant and every production
or non-synthetic case retain HTTPS. This does not expand production SSRF policy.

All discovered builders now use the helper (line numbers after this change):

| File | Sites |
| --- | --- |
| `apps/orchestrator/hibernation.py` | 1269, 1564 — chat completion probes |
| `apps/orchestrator/management/commands/check_gateway_health.py` | 88 DNS host/port, 125 health, 223 runtime diagnostic |
| `apps/orchestrator/services.py` | 2559 — health |
| `apps/orchestrator/tasks.py` | 775 — chat completion |
| `apps/router/pending_queue.py` | 2108 health; 2487 LINE, 2617 Telegram, 2761 iOS chat |
| `apps/router/poller.py` | 1486 — health |
| `apps/router/tasks.py` | 150 — chat completion |
| `apps/router/services.py` | 330 — Telegram webhook; caller passes tenant explicitly |
| `apps/cron/gateway_client.py` | 215 — tools/invoke |
| `apps/evals/behavior/transport.py` | 342 — behavior transport |
| `apps/orchestrator/management/commands/prove_local_sautai.py` | local proof authority check and tools/invoke |

`ADMIN_OPENCLAW_GATEWAY_URL` is a separate configured operator URL, not a tenant
FQDN builder; it and its token are blank in this install. A search found no
remaining literal HTTPS tenant FQDN builder (apart from explanatory docstrings).

## Runtime and scheduling findings

The repo symbol is `generate_openclaw_config`, not `build_openclaw_config`.
Provision/apply still goes through the actual renderer, config security audit,
config write validator and share sanitization. Local files implement the mounted
share protocol used by `runtime/openclaw/entrypoint.sh`; its Linux shell plumbing
is not run on macOS. Runtime plugin paths resolve to this repo, including sautai.

Ollama `/api/tags` confirmed `qwen3.8:27b-obliterated-q8` is already pulled.
The local model uses the OpenAI-compatible `/v1` interface, without a `num_ctx`
option or cloud fallback. Installed runtime source inspection showed the compat
provider would auto-inject `options.num_ctx`; the adapter explicitly sets
`injectNumCtxForOpenAICompat=false` to retain the server default. No Ollama
pull/restart was performed.

OpenClaw 2026.9.1 requires pdfMaxMb, top-level memory.search, and removal of old
schema keys already handled by the fleet's 9.4 migration. The local adapter
applies those moves without adding the fleet's cloud Brave dependency.
Its native lifecycle coordinator also hardcodes `/tmp`, ignoring TMPDIR. The
sandbox boot test rejected that write. `openclaw-paths.mjs` redirects this one
installed-module path **in process** to the allowed worktree temp directory;
shared installed runtime files are not modified. Earlier unsandboxed schema
validation may have created native coordinator files under `/tmp`; no personal
OpenClaw state was read or written. All gateway boot tests now use the shim and
filesystem sandbox.

QStash fallback exists at `apps/cron/publish.py:150` and invokes
`apps/cron/views.py:31::execute_task_sync`. Local mode wraps that same executor
in transaction-on-commit daemon timers, honoring delay_seconds and allowing
runtime plugin requests to acknowledge before long plan generation finishes.
These timers are in-process and are lost on restart. Periodic fleet jobs and
external QStash schedules are not registered. The production fallback is unchanged.

## Secrets and hand-off

Generated names only: `SECRET_KEY`, `JWT_SECRET`, `NBHD_INTERNAL_API_KEY`,
`LOCAL_TEST_DB_PASSWORD`, `LOCAL_TEST_KEK_SEED`. Canonical values live in the
ignored 0600 `.env.local-test`; the DB password is also referenced by its two
local DB URLs. Normal local application persistence includes the tenant auth-key
column and wrapped DEK row. No human password was generated or captured.
Ollama uses a public no-auth dummy client string.

`SAUTAI_M2M_BASE_URL` is exactly `http://127.0.0.1:8000`.
`SAUTAI_PLATFORM_SECRET` remains blank on disk. The actual sim lane must send its
in-memory secret plus actual synthetic sautai user ID over the private socket;
Django holds the secret only in RAM. The README gives an adapter tied to the
actual `sim/run.mjs`
`handshakeSecret` and `yuki-week.mjs` `linked.sautai_user_id` variables. The sim
tears down its backend in `finally`, so proof must be awaited inside the journey,
with the actual NBHD tenant UUID used during link resolution. This does not
claim that the other lane has integrated or invoked the adapter. Re-hand-off
after restart. The listener now survives clients/probes disconnecting early.
No substitute secret, fake plan or fake success response was created.

## Remaining acceptance prerequisites

1. MJ must create the account at **http://127.0.0.1:18080/local-test/signup/** after
   the orchestrator loads the Django plist. At last check the local DB contained
   **zero accounts and zero tenants**. No account was manufactured to bypass MJ.
2. After signup, run the documented tenant preparation command with the verified
   `/Users/mjjones/worktrees/sautai-yuki-lane/sim/persona/yuki.json`. Its 26 facts
   match `/Users/mjjones/Projects/harness/core/personas/yuki.md` version 3 and
   source SHA-256 `811c280b12d42f349632942a6ad72487e585b41121276e3c4aed2b41c5d18fe6`.
   A generated copy is ready at `deploy/local-test/.state/yuki-v3.json`;
   actual DB/USER.md seeding awaits MJ’s account.
3. The loanarmy process guard detects an active process; an actual launcher check
   confirmed it refuses startup. Do not start
   inference until the lane/orchestrator confirms a safe run window; this lane
   has not touched those processes.
4. The orchestrator loads the gateway plist after tenant/config preparation.
5. The sautai lane runs its local sim and performs the genuine token/user-ID
   hand-off. No hand-off has been received.
6. Run `check_gateway_health --gateway-only`, the normal-chat proof (MJ supplies
   the password interactively), and `prove_local_sautai` for an unused confirmed
   synthetic week. Save only their metadata. These live acceptance results are
   **pending**, not replaced by the unit or disposable boot tests above.

MJ's masked-store instruction is printed by `run.py password-help`:
`security add-generic-password -s org.nbhd.yuki-test -a nbhd -w` — interactive;
MJ types the password. Do not pass it as an argument, log it, or add it to env.

No Azure calls, production endpoints, Stripe/APNs/email, personal gateway,
Ollama restart, host Postgres databases or loanarmy jobs were used. No plists
loaded. No push to main or merge. Final state check: Compose services up; Django
18080 and gateway 19443 free; zero accounts/tenants; loanarmy guard still active.

## Review handoff

Implementation commits: `d866edfc`, `c6890085`, `90bbe080`; documentation
checkpoint: `c18e582f`. All changes use explicit-path commits on
`feat/yuki-local-test-stack`. The feature branch is prepared for a **draft PR**
under the repository workflow; keep it draft until the live acceptance items
above are resolved. The final response supplies the resulting PR link.

This report is final for the work performed in this lane. S2 live acceptance
remains incomplete; neither a real Yuki chat reply nor a real sautai job result
has been observed. The prepared scripts and verified persona remove local
implementation setup work from the remaining operator handoff.
