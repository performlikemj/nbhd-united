# Production debugging playbooks

There is no staging — prod is where things are verified. The `/production-logs` skill wraps the common commands; this doc is the diagnostic knowledge.

## Investigation discipline — the failure is always the same one

**When a theory forms, your next action is the cheapest read that could KILL it — never more theory.** Every wrong conclusion below was plausible, and every correction was not better reasoning but a read that had been skipped. The tell that you are about to be wrong: your sentence makes a claim about an artifact that has never appeared in your tool results.

Each line is a real 2026-07-14 miss, all five in one investigation:

- **A model doing something strange is usually obeying something you haven't read.** Open what it was TOLD — `templates/openclaw/AGENTS.md`, `USER.md` on the tenant's share, the tool descriptions — before theorising about its behaviour. The assistant "refusing to set reminders" was following a capability list to the letter; reminders simply weren't on it.
- **The datapoint your theory can't explain is the verdict, not the noise.** If you are explaining one away, the theory is already dead. A "leftover cron blocks it" theory survived three runs by ignoring the fourth, which had failed before any cron existed.
- **Before theorising about why a call failed, prove the call HAPPENED.** One log query showed zero requests ever reached the endpoint; a whole 409-conflict theory died in thirty seconds.
- **Before citing a log line as evidence, grep what emits it and when.** `tool-search: cataloged 62 tools` fires on every session boot, not when the model searches. Counting those lines built an entire false mechanism.
- **A local test green is a LOGIC SMOKE, never CI parity.** The venv drifts from `requirements.txt` and cannot always be fixed by pip (Django 6 has no py3.11 wheels). An RLS failure was called "pre-existing, not ours" on the strength of a local green. It was ours — dj-stripe 2.10.3 ships no `0003_2_11` migration, so the two tables the test failed on could not exist locally at all; the run was *structurally incapable* of failing. `make setup` rebuilds the venv at parity. A `PostToolUse` hook flags drift on test/migration runs — but it is SILENT when clean, and silence means only "matched origin/main **as of your last fetch**", never "you are safe".

**The honest limit of this section.** No hook can fire when the failure is a skipped read, so only the last of these five is machine-caught; the other four are defended by nothing but this list and the review loop. Take that seriously, because the species recurred **three more times inside the PR written to prevent it** — a rebuild script that trusted a stale checkout (the very error it existed to fix), a hook comment asserting a `make integrate` target that does not exist, written without opening the Makefile, and a `make setup` recipe that silently stripped the prod CUDA pins. Every one was caught by a read or by a reviewer. Not one by prose — including this prose.

## Log sources — pick the right one

- **Live tail (last ~10 min only):** `az containerapp logs show --name <app> -g rg-nbhd-prod --tail 300 --follow false`. It caps at 300 lines and **will lie about absence** for anything older.
- **Historical / time-range (the default for investigation):**
  ```bash
  az monitor log-analytics query --workspace 035a49db-1da5-452d-8b32-b074d7a5d606 \
    --analytics-query "ContainerAppConsoleLogs_CL | where ContainerAppName_s == '<app>' | where TimeGenerated > ago(6h) | where Log_s contains '<needle>' | take 50" -o tsv
  ```
  `ContainerAppSystemLogs_CL` for crashes/OOM/image-pull/revision events.
- Sentry is wired but gated on `SENTRY_DSN` being set on Azure.

## Playbook: "assistant isn't responding" / "Sorry — I had trouble responding"

Check the **OC tenant container** logs first, NOT Django — the webhook/forward almost always worked; the failure is the LLM call inside OpenClaw. Grep for `FailoverError`, `401 User not found`, `rawError`. `HTTP 401: User not found` = OpenRouter rejecting the key (account-level; every fallback model fails identically) → rotate via `/rotate-keys` (KV `openrouter-api-key`); idle containers pick the new key up on next wake — have MJ send a message rather than restarting the fleet.

Not this: `cron.list` 404 (container URL routing), dashboard 401s (frontend JWT expiry), `Connection closed by server` from redis (Upstash drop, cache BYPASS handles it).

## Playbook: burst of tool timeouts at exactly 20000ms

Several unrelated `nbhd_*` tools timing out at `20000ms` within minutes = Django replica saturation, not the endpoints. The single replica queues past the KEDA threshold (concurrent ≥10); replica #2 cold-starts 10–20s; in-flight OC calls hit the hard 20s cliff. Check whether multiple tenants saw it (Django-side) vs one (tenant-scoped slow handler), and `az containerapp logs show --name nbhd-django-westus2 --type system` for restarts/OOM.

## Playbook: container looks running / looks dead

- `runningStatus: Running` + `minReplicas: 1` on `oc-*` apps means NOTHING — hibernation deactivates revisions, the template keeps those values. Trust the **Replicas metric**; `revision list` can return empty even for the active tenant.
- Same-tag re-bump wedge: re-running `update_container_image` to the SAME tag leaves a single-revision app with an empty revision list, edge 404s (`404 <!DOCTYPE html>`), cold start fails. Recover: `az containerapp update --image <tag> --revision-suffix <fresh>`.
- Cold starts mask errors: after a timeout, always check logs — the real error is often behind the cold start.

## Playbook: post-deploy verification

1. CI green isn't proof — read the pass/fail column or `gh run view <id> --json conclusion`. Never trust `$?` after a piped `gh ... --watch` (the pipe steals the exit code — run bare).
2. Deploys auto-migrate: `startup.sh` runs `migrate --noinput` before gunicorn; the old revision serves until the new one is up, so a just-merged migration can be invisible in the DB for a few minutes. Don't panic; check again.
3. Verify the **user-facing symptom**, not a proxy (send the message, load the page, hit the endpoint).
4. For multi-tenant config changes: bump configs and confirm at least one tenant picked it up (probe the file share: `openclaw.json` contents, `cron/runs/<id>.jsonl`).

## Playbook: silent pipeline failure (nothing errored, data is stale)

Suspects, in order: QStash dedup-id 400s (`Log_s contains 'DeduplicationId'` over 7 days — the publish error is a swallowed WARNING); reaped idle DB connection wedging a status row (see invariants §8); the log line you're grepping for was redaction-dropped (a zero count in logs is unreliable evidence of absence — verify by effect, e.g. file-share contents, DB rows).

## Prod DB access

Live control-plane DB = Supabase **us-west-1** `dljqtpunnobyztampxus` (query via the Supabase MCP with that explicit project_id). Local `.env` points at a DEV db. Verify any DB connection's freshness with `select max(updated_at) from cron_cronjob` before trusting or writing. Avoid `az containerapp exec` (needs a TTY).
