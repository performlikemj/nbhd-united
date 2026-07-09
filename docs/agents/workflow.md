# Git / CI / deploy workflow

The `/deploy` skill walks the full commit→push→verify sequence. This doc is the rules and the traps.

## Branching

- `main` is protected — everything goes through a PR branch (`feat/`, `fix/`, `refactor/`, `docs/`).
- **Cross-branch work always uses a worktree**: `git worktree add .claude/worktrees/<name> -b <branch> origin/main` (that dir is gitignored). Never `git checkout` away from a dirty working tree; never stash-juggle.
- Stage specific files only. `git add -A` / `git add .` are blocked by a hook — they risk committing `.env`, models, build artifacts.
- Never `--no-verify` (only exception: pre-commit scanner false positive in scanner code itself — ask first). Never force-push main.

## Pre-push checklist (each maps to a CI gate that `make lint`/local tests do NOT cover)

1. `.venv/bin/ruff check <files>` **and** `.venv/bin/ruff format <files>` — CI runs `ruff format --check .` as a separate, stricter gate. (The on-edit hook now auto-formats, but verify before push.)
2. `python manage.py makemigrations --check --dry-run` — CI fails on any model↔migration drift, including `help_text` typos. Always **generate** migrations (`makemigrations <app> --name <slug>`), never hand-write.
3. Adding ANY migration to an app with `public.*` tables? Add a fresh tenants relock migration depending on it (topo-sort shifts can push the RLS lockdown before new tables exist). Fast check: `python manage.py test apps.tenants.test_public_schema_lockdown --noinput`.
4. Local venv is Python 3.11 and can lag CI's pins (e.g. Django 6 removed `CheckConstraint(check=)` → use `condition=`). When CI fails on an apparently-unrelated subsystem, suspect version-pin/topo drift, not your code.
5. Never pip-compile `requirements.txt` on macOS — it strips Linux CUDA/triton torch pins. Hand-edit the lockfile.

## Merging

- This repo has **no required status checks** — `gh pr merge --auto` merges instantly, before CI finishes. The *deploy* job is what's gated on tests, so prod stays safe, but a red commit can land on main. If you want green-before-merge, watch CI yourself first.
- **Stacked-PR phantom merge**: before merging, `gh pr view <n> --json baseRefName` MUST be `main` — merging a PR based on a deleted/merged branch "succeeds" without ever advancing main. After ANY merge, verify main actually advanced (`git ls-tree origin/main <changed-file>`).
- `gh pr checks --watch` / `gh run watch`: run **bare**, no pipe, no trailing `; echo` — anything after it steals the exit code and a red run reads as green. Confirm by reading the output, not `$?`.

## Deploy pipeline (push to main → `.github/workflows/ci-cd.yml`)

frontend lint+build → backend checks+tests (incl. `ruff format --check`, `makemigrations --check`, pgvector/pg16) → OpenClaw config-doctor smoke → build+push `django:<sha>` + `nbhd-openclaw:<ocver>-<shortsha>` → deploy Django (single-revision) → health check → bump pending configs → register QStash crons → deploy frontend.

After deploy: verify per `docs/agents/debugging.md` §post-deploy. OpenClaw fleet rollouts are separate (`workflow_dispatch`); merging to main does NOT roll tenant images.

## Parallel work & deploy serialization

Multiple things in flight at once (two sessions, a person + dependabot, three PRs going green in the same minute) used to race the production deploy. Single-revision mode makes `az containerapp update` last-writer-wins, so on 2026-07-09 the *oldest* of three near-simultaneous merges deployed last and prod served stale code — with every run green. Full write-up: `docs/deploy-concurrency-directive.md`. What's now in place (`ci-cd.yml`):

- **Main deploys are serialized** by a workflow-level `concurrency` group (`ci-cd-${{ github.ref }}`, `cancel-in-progress: false` on main). Only one deploy is ever in flight; the rest queue. A multi-merge burst therefore deploys strictly one-at-a-time — expect a full ~15–20 min run of queue latency, that's the design working, not a hang.
- **A "cancelled (superseded)" main run is expected, not a failure.** With at most one running + one pending run per group, a third push supersedes the *pending* (middle) run. The newest commit already contains the middle one's code, so this converges to newest faster. Do not treat those cancelled runs as broken.
- **The freshness guard skips green.** Each deploy job first checks whether its commit is still the tip of `origin/main`; if not, every step is skipped and the job ends green with a loud `::notice::`. This is the backstop if concurrency is ever removed/misconfigured or an old run is re-run by hand (`gh run rerun`).
- **The identity assertion fails the run RED.** After the health check, the backend job asserts the serving revision's image tag equals the run's own `github.sha` (the immutable full-sha tag, never `:latest`). Liveness ≠ identity — old code is healthy too, which is why the incident was all-green. A mismatch now means a deploy race/overwrite and the run goes red.
- **Emergency recovery** (a raced/overwritten deploy) — replay the deploy directly with the correct sha; the image already exists in ACR tagged by full sha:
  ```bash
  az containerapp update --name nbhd-django-westus2 --resource-group rg-nbhd-prod \
    --image nbhdunited.azurecr.io/django:<full-sha-of-origin/main> \
    --set-env-vars OPENCLAW_IMAGE_TAG=<oc-version>-<7char-sha> SENTRY_RELEASE=<full-sha>
  ```
  ALWAYS finish by re-checking the serving image sha equals `git rev-parse origin/main` — otherwise you don't know if the recovery itself raced something.
- **Identity gate + daily reconcile** (later hardening): the "Wait for Django" step passes only when `/health/` reports `build == github.sha`, so post-deploy steps can never fire against the old revision during the cutover window; and a daily `reconcile-system-crons` QStash task (04:20 UTC) re-syncs system cron schedules so registration drift self-heals within 24h even if a deploy-time call is missed.
- **Expand/contract discipline** (the within-run residual the pipeline can't fix): `deploy-frontend` and `deploy-backend` run in *parallel* within a run, and migrations run at container boot — so there's always a brief window of new-frontend-vs-old-backend (or an older image booting against an already-migrated schema). Keep migrations additive-first (expand/contract) and API changes additive so either side survives the handover.

The CI layer above is the last line of defense; the local train catches the same races *before* they reach GitHub, on your laptop where they're cheap and reversible:

- **One worktree per session; the primary checkout is the integration station.** Each session works on its own `git worktree add .claude/worktrees/<name>` branch and the bare `~/Projects/nbhd-united` checkout stays clean — it's where trains run and PRs get merged from.
- **Run `/integrate` before merging whenever more than one branch or PR is in flight.** It octopus-merges everything in flight onto a throwaway worktree and runs `make integrate-gate` (the CI-mirroring gate) against the *combination* — catching merge skew: two branches each green alone but broken together with no textual conflict (A renames a helper B just started calling; both PRs green, `main` broken). A lone PR with nothing else open needs no train — there's no combination to test.
- **An octopus refusal is a free cross-branch collision detector.** It names the offending pair and files (`CONFLICTING PAIR: feat/a <-> feat/b (files: …)`) and stops — nothing is landed, no stamp is written. Resolve it on the branches themselves (rebase one onto the other, or coordinate the edit), then re-run.
- **A green train writes a machine-local stamp** at `$(git rev-parse --git-common-dir)/integration-pass.json` — inside `.git/` so it can never be committed by accident (if it shows up in `git status`, that's a bug). `git_guard` reads it: when more than one of your PRs is open and you go to merge, it warns if no fresh stamp covers that PR's head. Running the train clears the warning; the stamp goes stale after 24h or the moment `origin/main` moves.

## Commit convention

Prefixes `feat:` `fix:` `fix(scope):` `refactor:` `docs:` `merge:` — concise, focused on the why. Plan complex features first in a `CONTINUITY_<feature>.md` and implement phase by phase.

<!-- race-test B: no-op marker (deploy-serialization §5.3 live test, 2026-07-09) -->
