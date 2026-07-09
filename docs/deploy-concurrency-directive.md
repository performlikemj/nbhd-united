# DIRECTIVE: Serialize CI/CD so concurrent merges can never race the production deploy

**Status:** Ready for implementation. Not started.
**Date:** 2026-07-09
**Repo:** nbhd-united (`.github/workflows/ci-cd.yml`)
**Origin:** Real production incident, 2026-07-09 ~01:00Z (details below). Memory: `project_cicd_deploy_race_no_concurrency_group`.

---

## 1. The incident (what actually happened — all evidence verified)

Three PRs were merged to `main` seconds apart on 2026-07-09:

| PR | Merge commit | Merged at (UTC) |
|---|---|---|
| #1069 (fuel save fix) | `729efe8c` | 00:33:18 |
| #1070 (workout congrats) | `b1321cdf` | 00:33:21 |
| #1071 (PDF ingress) | `6108a38d` | 00:33:24 |

Each push to `main` triggered its own full `ci-cd.yml` run. All three ran **concurrently** — the workflow has **no `concurrency:` block anywhere**. Each run independently builds `django:<github.sha>`, pushes to ACR, then runs:

```
az containerapp update --image nbhdunited.azurecr.io/django:<github.sha> \
  --set-env-vars OPENCLAW_IMAGE_TAG=2026.5.28-<shortsha> SENTRY_RELEASE=<sha>
```

The app runs in **single-revision mode** (required for the Telegram poller), so whichever run executes `az containerapp update` **last** owns production — regardless of commit order. Build times vary by minutes, so finish order is effectively random.

**Observed outcome:** the run for `6108a38d` (the newest code, containing all three PRs) finished its deploy at ~00:59 (revision `0001044`). Then the run for `729efe8c` — the **oldest** merge, containing only #1069 — finished at 01:00:17 and created revision `0001045`, which took 100% traffic. Production was serving code **missing two of the three just-merged features**, while **every workflow run showed green SUCCESS**. Nothing in CI, Azure, or logs flagged it. It was caught only by manually comparing the serving revision's image sha against `git rev-parse origin/main`.

**Blast radius beyond the image:** the deploy step also stamps env vars. The raced overwrite regressed `OPENCLAW_IMAGE_TAG` to `2026.5.28-729efe8` (wrong OpenClaw image for tenant wakes/provisions) and `SENTRY_RELEASE` (misattributed errors).

**Secondary hazard (got lucky):** each run also fires post-deploy side effects against the live control plane: `bump-all-pending-configs`, `force-reseed-crons`, `register-system-crons`, `backfill-welcomes`. These ran three times, interleaved, while an out-of-date revision was serving. Per-tenant config applies **regenerate config from whatever code is serving at apply time** — an apply executed during the wrong-code window consumes the tenant's pending flag with a stale config (it then won't reapply until the next bump). We verified via Log Analytics that no apply ran in the 01:00–01:05 window — but that was luck, not design.

**Recovery used** (documented for runbooks, not as an acceptable steady state): replay the deploy step directly with the correct sha — the image already exists in ACR tagged by full commit sha:

```
az containerapp update --name nbhd-django-westus2 --resource-group rg-nbhd-prod \
  --image nbhdunited.azurecr.io/django:<full-sha-of-origin/main> \
  --set-env-vars OPENCLAW_IMAGE_TAG=2026.5.28-<7char-sha> SENTRY_RELEASE=<full-sha>
```
then confirm `/health/` returns 200 and the serving revision's image matches `origin/main`.

## 2. Root cause

1. `ci-cd.yml` defines **no concurrency control**, so N pushes to `main` = N unserialized pipelines.
2. Single-revision Container Apps semantics make the deploy **last-writer-wins**, decoupled from commit order.
3. The deploy step has **no freshness guard** — it happily deploys a sha that `origin/main` has already moved past.

Any one of the three fixed this incident class; ship the first and third (defense in depth).

## 3. Required changes

### 3.1 Workflow-level concurrency (primary fix)

Add to the top level of `.github/workflows/ci-cd.yml` (alongside `on:`/`env:`):

```yaml
concurrency:
  group: ci-cd-${{ github.ref }}
  cancel-in-progress: ${{ github.ref != 'refs/heads/main' }}
```

Semantics to understand before implementing (verify against current GitHub docs):

- **On `main`** (`cancel-in-progress: false`): a running deploy is never killed mid-flight (a cancelled half-deploy is worse than a stale one). New runs **queue**. GitHub keeps at most one pending run per group — when a third push arrives, the *older pending* run is superseded/cancelled and the *newest* waits. Net effect: the currently-running run finishes, then the newest commit deploys. Final state always converges to newest code. Intermediate commits may show "cancelled" runs — that is correct and expected; note it in the PR body so nobody treats those as failures.
- **On PR branches** (`cancel-in-progress: true`): a force-push/re-push cancels the obsolete run and saves CI minutes. `github.ref` is `refs/pull/<n>/merge`, so each PR gets its own group — PRs never queue behind each other.
- The group name embeds the ref, so PR runs and main runs never share a queue. Keep the `ci-cd-` prefix so this workflow never collides with other workflows' groups (dependabot auto-merge, etc.).

Do **not** use job-level concurrency as the primary mechanism: job-level groups queue by *when jobs become ready*, not by commit order, so an older run's deploy job can still land after a newer run's — it serializes without fixing ordering.

### 3.2 Pre-deploy freshness guard (belt-and-suspenders)

At the top of **both** deploy jobs (`deploy-backend`, `deploy-frontend`) — and `fleet-rollout` if it ever deploys — add a step that skips the job when the commit being deployed is no longer the tip of `main`:

```yaml
- name: Skip if superseded by a newer main commit
  id: freshness
  run: |
    git fetch origin main --quiet
    TIP=$(git rev-parse origin/main)
    if [ "$TIP" != "${{ github.sha }}" ]; then
      echo "superseded=true" >> "$GITHUB_OUTPUT"
      echo "::notice::Skipping deploy: origin/main is $TIP, this run is ${{ github.sha }} (superseded). The newer run will deploy."
    else
      echo "superseded=false" >> "$GITHUB_OUTPUT"
    fi
```

then gate every subsequent step in those jobs with `if: steps.freshness.outputs.superseded != 'true'` (or restructure with an early-exit pattern — implementer's choice, but the job must end **green** when skipping, not red, and the skip must be loudly visible in the log). This guard is what saves you if concurrency is ever removed, misconfigured, or a re-run of an old run is triggered by hand — the exact scenario `gh run rerun` makes easy.

Note the checkout in these jobs must have enough history for `rev-parse` of a fetched ref (a plain `actions/checkout@v4` + `git fetch origin main` as shown is sufficient; do not assume `fetch-depth: 0` exists).

### 3.3 Post-deploy side-effect steps ride the same serialization

`Bump pending config version`, `Reseed tenant cron jobs`, `Register system QStash cron schedules`, `Backfill welcome crons` already live inside `deploy-backend`, so 3.1 + 3.2 automatically serialize them and guarantee they only run for the commit that actually deployed. No separate change needed — but **verify** none of them were moved to a separate unguarded job since this directive was written.

## 4. Explicitly out of scope (do not do)

- Do NOT set `cancel-in-progress: true` for `main` — killing `az containerapp update` or the migration-running boot mid-flight creates partial states this platform has no reconciler for.
- Do NOT reorder or "fix" the Telegram single-revision mode, the poller 409 handling, or the post-deploy endpoints. The transient poller 409 burst during every revision handover is known-benign and self-settles (~1 min).
- Do NOT add required status checks / branch-protection changes — separate decision with its own tradeoffs (dependabot auto-merge currently relies on their absence).
- No changes to `Dockerfile`, image tagging, or ACR retention.

## 5. Acceptance criteria (all must pass — verify, don't assume)

1. `ci-cd.yml` has the workflow-level `concurrency` block exactly as specced (or equivalent with written justification), and both deploy jobs carry the freshness guard.
2. **Static check:** `gh workflow view` / YAML parses; a PR push still runs tests; a PR force-push cancels the prior PR run.
3. **Live race test** (cheap, safe): create two trivial no-op PRs (e.g. touch two different comment lines under `docs/`), merge them ~seconds apart, and observe: the two main runs do NOT execute concurrently (second queues or is superseded by GitHub's pending-slot rules); after the dust settles, `az containerapp revision list` shows the serving revision's image sha == `git rev-parse origin/main`; the superseded/queued run's behavior matches the semantics documented in your PR body.
4. The freshness guard's skip path is exercised at least once (the live race test usually does it; otherwise `gh run rerun` an old completed run and confirm it skips green with the notice).
5. `docs/agents/workflow.md` gains a short section: multi-merge deploys are serialized; what a "cancelled (superseded)" main run means; the recovery command from §1 for emergencies.
6. Standard repo gates: PR to `main` (never direct push), CI green before merge, and after merge verify the deploy landed by checking the serving image sha — using the very property this directive establishes.

## 6. Context an implementing agent needs

- Repo harness: read `CLAUDE.md` (router) → `docs/agents/workflow.md` BEFORE touching CI. Iron rules apply: worktree for the branch, stage specific files, no `--no-verify`, `gh pr merge` semantics ("no required checks → merges instantly — watch CI yourself").
- The workflow file is ~550+ lines; the deploy jobs are `deploy-backend` (~line 283) and `deploy-frontend` (~line 520), both `needs: [frontend-test, backend-test, openclaw-config-smoke]` and gated `if: github.event_name == 'push' && github.ref == 'refs/heads/main'`. `fleet-rollout` exists and is usually `skipped` — check whether it deploys anything before deciding if it needs the guard.
- Known trap from the same incident cluster: frontend-test failures block deploys entirely (`needs:`) — if your test PRs hit an unrelated red frontend job, check for a dependency/lockfile breakage on main before assuming your change caused it (see memory `project_dependabot_tiptap_lockfile_eresolve`).

<!-- race-test A: no-op marker (deploy-serialization §5.3 live test, 2026-07-09) -->
