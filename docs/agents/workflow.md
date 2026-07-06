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

## Commit convention

Prefixes `feat:` `fix:` `fix(scope):` `refactor:` `docs:` `merge:` — concise, focused on the why. Plan complex features first in a `CONTINUITY_<feature>.md` and implement phase by phase.
