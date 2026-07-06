# Production debugging playbooks

There is no staging — prod is where things are verified. The `/production-logs` skill wraps the common commands; this doc is the diagnostic knowledge.

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
