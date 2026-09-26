# Basecamp Yuki TEST stack

This is a local-only synthetic install. It never loads launchd jobs. Use this
worktree, not the primary checkout. The normal production E2E harness remains
unchanged; its host/account allowlist must not be weakened for this stack.

## Install and start

```sh
cd /Users/mjjones/worktrees/united-yuki-test
python3 deploy/local-test/bootstrap.py
python3 deploy/local-test/compose.py
python3 deploy/local-test/run.py manage migrate --noinput
```

Bootstrap creates a worktree venv that reads basecamp's existing repo Python
packages via a `.pth` file (no mutation of that venv), installs CI's Ruff pin,
and installs checksum-verified Compose 5.5.1 inside `.state/docker/cli-plugins`.
It copies the already-cached DeBERTa model into the test home for offline CPU PII
detection. It never downloads/pulls/restarts an Ollama model. `install.py` reads
Ollama `/api/tags` and requires the already-pulled
`qwen3.8:27b-obliterated-q8`. Re-running preserves install secrets.

Compose uses the repo `docker-compose.yml` plus `compose.yaml`, project
`nbhd-yuki-test`, its own volume/network, DB/user `nbhd_yuki_test`. It starts only
Postgres and Redis. Ports: Postgres **55441**, Redis **56381**, Django **18080**,
OpenClaw **19443**; all host listeners bind loopback. The host `postgres16`
service and other containers are not used. `compose.py ps` inspects this project.

The orchestrator, not Codex, runs these after reviewing the generated plists:

```sh
launchctl bootstrap gui/$(id -u) "$PWD/deploy/local-test/.state/com.mj.yuki-united.plist"
# Load gateway only AFTER the account/provision steps below:
launchctl bootstrap gui/$(id -u) "$PWD/deploy/local-test/.state/com.mj.yuki-united-gateway.plist"
```

There is no need to copy plists into `~/Library/LaunchAgents`. The launchers read
the gitignored `.env.local-test`; plist XML contains no tokens. Gateway startup
refuses if the process list contains a loanarmy process. It never uses `--force`,
installs a personal daemon, or restarts Ollama. Keep GPU scheduling coordinated
with the loanarmy lane for the whole inference run.

## Throwaway account and tenant preparation

MJ approved agent-created throwaway accounts on this loopback stack on
2026-09-20. The scoped exception allows `yuki_local signup` to generate a
`secrets.token_urlsafe(24)` password and store it **only** in the ignored
`deploy/local-test/.state/yuki-account.json` (0600), alongside `yuki@example.com`.
The normal signup view accepts this synthetic address and uses Django password
validation. Never pass the password in argv, print it, log it, or copy it to
another file. Django retains its normal password hash.

Bootstrap order: `yuki_local signup` (bootstrap guard: DEBUG, AZURE_MOCK,
LOCAL_TEST_ROOT, NBHD_TENANT_ID, no foreign tenant) →
`prepare_local_test_tenant --email yuki@example.com --persona-file deploy/local-test/.state/yuki-v3.json` →
start the gateway → `link` / `chat-plan` / `tonight` (strict tenant guard).

Prepare the designated tenant after normal account creation:

   ```sh
   python3 deploy/local-test/run.py manage prepare_local_test_tenant \
     --email yuki@example.com \
     --persona-file /Users/mjjones/worktrees/sautai-yuki-lane/sim/persona/yuki.json
   ```

   No account is created by this command. The actual manifest contains
   `persona=yuki`, `persona_version="3"` and 26 confirmed facts, retaining IDs and
   citations. It was verified against the canonical
   `/Users/mjjones/Projects/harness/core/personas/yuki.md` and its source digest
   `811c280b12d42f349632942a6ad72487e585b41121276e3c4aed2b41c5d18fe6`.
   Facts Y-021 through Y-026 were explicitly confirmed by MJ on 2026-09-04.
   The manifest generator excludes proposed facts. No guessed biography is seeded.
   To refresh the manifest without writing into another lane:

   ```sh
   node /Users/mjjones/worktrees/sautai-yuki-lane/sim/persona/generate-manifest.mjs \
     /Users/mjjones/Projects/harness/core/personas/yuki.md \
     "$PWD/deploy/local-test/.state/yuki-v3.json"
   # Then use --persona-file deploy/local-test/.state/yuki-v3.json above.
   ```

The single tenant UUID is generated at install in `NBHD_TENANT_ID`. It is
synthetic, non-eval-sink, starter, $1 monthly cap, 100,000 token cap, zero purchased
credit, no budget exemption or Stripe, and a 30-day trial. Re-running does not
extend the trial or create another tenant. `provision_tenant` runs under
`AZURE_MOCK=true`; the script then points it at `127.0.0.1:19443` and applies
`update_tenant_config` through the normal share-write validator/sanitizer.

The actual generator in this revision is `generate_openclaw_config`, not
`build_openclaw_config`. Its local adapter selects only Ollama's OpenAI-compatible
endpoint at `http://127.0.0.1:11434/v1`, with no cloud fallbacks and no `num_ctx`
override (`injectNumCtxForOpenAICompat=false` also disables OpenClaw’s automatic
injection). It retains generated runtime plugin configuration and resolves
`OPENCLAW_*_PLUGIN_PATH` defaults into this repo's `runtime/openclaw/plugins`.
The 2026.9.1 binary already requires several schema moves gated at 9.4 in the
fleet generator; the local adapter handles these without changing fleet output.

`USER.md` confirmed facts are persisted in the synthetic user's preferences and
rendered inside its managed envelope, so refresh/apply preserves them. Config
and workspace files land in `~/openclaw-yuki-test` via the same
`upload_config_to_file_share` / `_put_share_file` path used for containers.
The normal container shell entrypoint is Linux-specific; the host launcher uses
its file/config/env protocol and directly runs the installed macOS gateway.
There is no container proxy: the gateway itself listens on 19443.
`openclaw-paths.mjs` redirects the installed runtime’s hardcoded `/tmp` lifecycle
lock location into the worktree via a process-local Node loader hook. It refuses
an unrecognized runtime source shape and does not modify the shared binary.

## Yuki helper

Run from this worktree with `HARNESS_ROOT` unset. Each subcommand ends stdout
with exactly one JSON object and exits zero only on success. HTTP is fixed to
`http://127.0.0.1:18080`, ignoring proxy environment variables and redirects.
The account file is never deleted or overwritten; a failed signup can be retried
with its saved credentials. An existing file must already have mode 0600.

```sh
unset HARNESS_ROOT
python3 deploy/local-test/run.py manage yuki_local signup
python3 deploy/local-test/run.py manage yuki_local link
python3 deploy/local-test/run.py manage yuki_local chat-plan --week YYYY-MM-DD
python3 deploy/local-test/run.py manage yuki_local tonight
```

- `signup`: no stdin. Returns `{"account":"created"}` or
  `{"account":"exists"}` after verified login. Requires the local tenant guard
  described above; never overwrites an existing credential file.
- `link`: supply one connect-key line on stdin (never argv or a file). Logs in,
  verifies the tenant gate, POSTs the real console endpoint, then reads the
  Integration row. Returns `linked`, `sautai_user_id`, `linked_at`.
  Failure returns `{"linked":false,"reason":"<short code>"}` and exit 1.
- `chat-plan --week YYYY-MM-DD`: requires an ISO Monday and one stdin message
  of at most 400 characters. Include the desired calendar date in the message;
  `--week` is the expected result, not a hidden instruction to the assistant.
  Creates a non-main thread titled `Yuki weekly plan <week>`, polls each reply
  up to 900 seconds, sends `Yes, please go ahead.` at most twice when confirmation
  is requested or the expected job does not exist, then waits at most 1800
  seconds for a ready job. Only the assistant invokes the plugin and uses its
  preview/confirm token. Wrong-week jobs created during the run fail with both
  `week` and `job_week`. Ready jobs must have a linked identity, result and no
  error. Returns `proof`, `status`, `week`, `turns` (user/assistant exchanges),
  `confirm_turns`, `job_id`, `addressed_by`, `meal_count` (or null), `transcript`,
  `reply_excerpts` (first 160 characters of each assistant reply).
  Threads remain as evidence. Timestamped messages/replies, including partial
  evidence on failure, are saved in `.state/proof/chat-plan-<week>-<UTC>.json`
  (0600); failures include `reason` and, when available, `transcript`.
- `tonight`: no stdin. Reads console link status and Fuel's Tonight endpoint.
  Returns `linked`, `meals_today`, `meal_names`, `week_start`. The Fuel response
  supplies the actual Monday it queried (also preserved in cached responses).
  Empty results add `empty_reason`: `no_meal_today` for a valid plan with no
  displayed meals today, `not_linked`, or `plan_unavailable` for degraded reads.
  An empty day does not fail the link. Other helper failures return a short
  `reason` with `account:"failed"` or `proof`/`status:"failed"` as applicable.

**S3 means the real console link:** while the sibling sim is alive, first hand
off only `base_url` and `platform_secret` over the private socket below (omit
`sautai_user_id` or send null). This stores the secret only in Django settings
and writes nothing to Integration. Mint a fresh connect key in sautai and pipe
it in memory to `yuki_local link`; nbhd resolves it server-side and stores the
returned identity. Then invoke `chat-plan` and `tonight` before the sim tears
down. Never pre-resolve/burn that key in the sibling lane. A hand-off supplying
a positive integer ID still supports the legacy S2 fixture path, but it is not
S3 console-link evidence. Never commit `.state/`.

## Local sautai hand-off — no invented token or sim result

The Django launcher opens the owner-only Unix socket
`deploy/local-test/.state/sautai-handoff.sock` (directory 0700, socket 0600).
Source inspection located the real values in the sibling lane:

- `sim/run.mjs:531` creates `handshakeSecret` in memory, adds it to the redaction
  set, and `bootServers` passes it as the backend's `NBHD_PLATFORM_SECRET`.
- `sim/run.mjs:597` passes `handshakeSecret` into each journey.
- `sim/journeys/yuki-week.mjs:108-109` resolves the connect key and captures
  `linked = result.body`; the real identity is `linked.sautai_user_id`.
- `sim/run.mjs:601-602` tears servers down in `finally`. The S2 proof must be
  awaited inside that managed lifetime. A successful runner exit leaves no sim
  backend running and cannot be used for later proof.

The following legacy S2 adapter is retained for reference. For S3, omit the ID
and hand off before resolving any key, as described above. For S2, the sautai
lane integrates this adapter immediately after its successful
`nbhd-link-resolve` step, using those actual in-scope values. This worktree does
not modify the other lane. For S2, replace that step's random NBHD tenant UUID
with this install's `NBHD_TENANT_ID` (non-secret), so the sim also records the
correct link. Use the self-hosted runner: its external mode deliberately returns
an empty handshake secret and cannot satisfy this contract.

```js
import net from 'node:net';
// In yuki-week.mjs, after linked = result.body and successful link validation:
await new Promise((resolve, reject) => {
  const socket = net.createConnection('/Users/mjjones/worktrees/united-yuki-test/deploy/local-test/.state/sautai-handoff.sock');
  let reply = '';
  socket.setTimeout(10_000, () => socket.destroy(new Error('S2 handoff timeout')));
  socket.on('error', () => reject(new Error('S2 handoff transport failed')));
  socket.on('connect', () => socket.write(JSON.stringify({
    base_url: backendUrl,
    platform_secret: handshakeSecret,
    sautai_user_id: linked.sautai_user_id,
  }) + '\n'));
  socket.on('data', chunk => { reply += chunk.toString(); });
  socket.on('end', () => {
    try {
      if (JSON.parse(reply).accepted !== true) throw new Error();
      resolve();
    } catch { reject(new Error('S2 handoff rejected')); }
  });
});
// Await the real S2 proof here before run.mjs tears down its backend.
// Log only acknowledgement/proof metadata; never handshakeSecret or raw replies.
```

The lane must also choose an unused proof week, avoiding its own journey's plan
and deletion steps. An S2 callback awaited at this point should run
`prove_local_sautai --confirmed-week <that Monday>` in the united worktree and
require exit zero. Local Ollama generation must be attested by the sim (its
`llmMetadata.mode` is `local-ollama`); a deterministic sim fixture is not evidence
of a real model-generated plan. Alternatively the lane can pipe the same JSON
in memory to `python3 deploy/local-test/run.py sautai-handoff`; never put it in
argv or a file. No adapter invocation or genuine hand-off has occurred yet.

The listener requires the exact local sim URL and stores the token only in the
running Django settings. Absent/null `sautai_user_id` leaves Integration untouched;
a supplied positive integer links that fixture ID on the designated tenant. This is explicit local fixture setup, not a fabricated OAuth
link. The token is neither put in the DB nor persisted to `.env.local-test`.
Re-send after either process restarts. An accepted hand-off proves transfer,
not successful plan generation.

QStash stays blank. The existing fallback is `apps/cron/publish.py::publish_task`
→ `apps/cron/views.py::execute_task_sync`. For this local setting only, a daemon
Timer calls that same executor after transaction commit and honors delay_seconds;
this preserves fast runtime acknowledgements and the sautai polling delay.
Jobs/timers are not durable across Django restarts. No periodic fleet/QStash
registration is run. Normal gateway cron capability remains available; call
single-tenant maintenance explicitly when needed.

## Real proof commands (after prerequisites)

```sh
# Read the tenant UUID from NBHD_TENANT_ID locally, without dumping the env file.
python3 deploy/local-test/run.py manage check_gateway_health --tenant-id '<tenant UUID>' --gateway-only
.venv/bin/python deploy/local-test/proof.py --email 'yuki@example.com'
# Operator expressly confirms this unused synthetic Monday; no regeneration.
python3 deploy/local-test/run.py manage prove_local_sautai --confirmed-week YYYY-MM-DD
```

Health checks tenant state, host+port resolution, gateway token, `/health`, and
real `cron.list`. `--gateway-only` excludes the old Azure-share and proxy
`daily-note` diagnostic; that full command is not a host-gateway round-trip
proof. The separate sautai command proves the runtime plugin round trip.

Chat proof prompts MJ for the password in memory, logs in normally, checks the
exact tenant/synthetic/non-sink gate, creates a disposable non-main thread, sends
one `[NBHD E2E SYNTHETIC]` fixture via normal chat API, and polls up to 900 seconds.
It succeeds only for `status=ready`, `source=tenant`, no error and nonempty reply;
it prints metadata only and deletes its disposable control-plane thread.
Deletion does not erase gateway memory.

Sautai proof calls the installed `nbhd_generate_meal_plan` plugin via the gateway,
validates the preview against the operator-confirmed week, then submits its
confirmation token. The real runtime view creates `SautaiMealPlanJob`; the normal
task and `sautai_client` must call the real local sim. The command requires a new
job to become ready; it prints flags/status only. No mocked reply is accepted.

## Isolation and generated secrets

`.env.local-test` is 0600 and gitignored. Generated per install:

| Name | Purpose |
| --- | --- |
| `SECRET_KEY` | Django signing, including preview confirmations |
| `JWT_SECRET` | Local account JWT signing |
| `NBHD_INTERNAL_API_KEY` | Local gateway and designated tenant runtime auth |
| `LOCAL_TEST_DB_PASSWORD` | Dedicated Compose PostgreSQL password; also in the two DB URLs |
| `LOCAL_TEST_KEK_SEED` | Stable, per-tenant derived **mock** KEK across local process restarts |

These are machine credentials, not MJ/Yuki passwords. Their canonical source is
only the ignored env file; config uses env references. Normal application
storage still includes the tenant internal-key column and wrapped random DEK
row in the local DB. The mock KEK is not production cryptography; deletion,
purge and recovery operations are refused in this persistent local adapter.
Ollama's required client key is a public dummy string, not a credential.
`SAUTAI_PLATFORM_SECRET` remains blank on disk and arrives only through hand-off.

All example integration values are blanked before local allowlisted overrides.
Cloud/provider credentials, email, Stripe, APNs, Azure, QStash, Sentry and admin
gateway are blank/disabled. Email uses the dummy backend. Django starts with a
clean environment, rejects other DB/home URLs, and denies external DNS/network
connections. OpenClaw starts with isolated HOME/OPENCLAW_HOME/STATE_DIR/CONFIG_PATH,
loopback binding, a Seatbelt network/write boundary, and no exec/browser/web
or cloud model fallback tools. Personal OpenClaw state is explicitly unreadable.
Do not weaken these guards to make a proof pass.

## Gates

```sh
python3 deploy/local-test/run.py manage check
python3 deploy/local-test/run.py manage makemigrations --check --dry-run
python3 deploy/local-test/run.py manage test \
  apps.orchestrator.test_gateway_url apps.orchestrator.test_local_test_stack \
  apps.orchestrator.test_services apps.router.test_services \
  apps.orchestrator.test_config_write_validation apps.integrations.test_sautai_client --noinput
.venv/bin/ruff check .
.venv/bin/ruff format --check .
DOCKER_GATE_CACHE="$PWD/deploy/local-test/.state/docker-gate-cache" \
TMPDIR="$PWD/deploy/local-test/.state/tmp" make docker-gate
```

Tests delegate to the repo `scripts/test-local.sh` with a worktree-hashed
`test_nbhd_united_yuki_test_<hash>` name only on Compose 55441. Existing DBs
are refused unless the caller explicitly sets `NBHD_TEST_DB_REUSE=1`; Python
child processes retain the network guard via scoped `sitecustomize.py`. The Docker
gate creates its own disposable containers. Its snapshot excludes `.state`
(including sockets, binary/cache files, and the snapshot itself).
See `REPORT-S2.md` for observed results and unresolved acceptance gates.
