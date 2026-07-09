# DIRECTIVE: Parallel-work harness — isolate in worktrees, integrate locally, land serially

**Status:** Vision approved for phased implementation, pending MJ review.
**Date:** 2026-07-09
**Repos:** nbhd-united first, then nbhd-ios and the rest of the fleet.
**Companion:** `docs/deploy-concurrency-directive.md` is the ready-to-implement spec for Layer 3 below. This directive is the wider frame that Layer 3 sits inside; where the two overlap, the companion is the exact wording to ship.

---

## 1. The problem is three races, not one

When more than one thing is in flight at once — two agent sessions, or one person plus a dependabot bot, or three PRs that all go green within the same minute — work collides in three distinct places. They are easy to conflate because they all feel like "stuff stepped on stuff," but each has a different cause and a different fix. Fixing one does nothing for the other two.

**Race A — the working-copy race.** Two sessions share one checkout. One is mid-edit; the other runs `git checkout` to a different branch, or stages files, or rebases. Files and the index get stomped. Either every session owns its own working directory, or you eventually lose real work to a checkout-switch that landed in the middle of someone else's edit. This is a local, file-level race. It never reaches the network.

**Race B — the integration race (merge skew).** Two branches are each green on their own. Nobody ever built and tested the *combination* before it landed on `main`. This repo has **no required status checks and zero required reviews** (verified via `gh api`), so `gh pr merge --auto` merges the instant a PR is mergeable — which means CI is currently the *first* place two branches ever meet. And CI does not even test the combination: it tests each merge commit as it happens to land, one at a time. So if branch A renames a helper that branch B just started calling, both PRs are green, both merge, and `main` is broken — with no red anywhere until the next run happens to compile them together. Either the branches are married and test-driven as a pair before landing, or the mainline is where you discover they don't fit.

**Race C — the deploy race.** N pushes to `main` fire N full `ci-cd.yml` runs with **no `concurrency:` block anywhere** in the workflow. Each run independently builds its images and calls `az containerapp update`. The app runs in single-revision mode (required for the Telegram poller), so whichever run calls `update` *last* owns production — regardless of which commit is newest. On 2026-07-09, three PRs merged seconds apart; the run for the **oldest** of the three finished its deploy last, and production served code missing two of the three just-merged features while **every workflow run showed green**. It was caught only by manually comparing the serving revision's image sha against `git rev-parse origin/main`. Full write-up and the exact fix are in `docs/deploy-concurrency-directive.md`. Either the mainline deploys one build at a time and always converges to the newest tested code, or the last pipeline to cross the line wins and the newest code silently loses.

**They compound.** Race C's "three simultaneous pushes" doesn't appear from nowhere — it is what you get when three sessions (or people, or bots) each merge their own PR at roughly the same time. Race A lets sessions run in parallel at all; parallel sessions produce simultaneous merges; simultaneous merges produce the deploy race. So the fix is not one patch — it is a chain of gates, each catching what the one before it can't.

## 2. The vision — one sentence per layer

Every workstream is built in its own yard, on its own siding, where nothing another session does can touch it (**Layer 1 — isolation**). Before anything ships, the finished cars are coupled into one train and the whole train is test-driven on local track — if any two cars won't couple, you find out here, not downstream (**Layer 2 — local integration**). The mainline accepts one train at a time, and the moment a newer train is ready the yard converges to it, so production is never left holding an older consist (**Layer 3 — serialized landing**). And the rulebook that makes all of this hold ships to every yard on the network automatically, so no repo is running on verbal tradition (**Layer 4 — propagation**).

The single invariant these four layers exist to guarantee:

**CI is never the first place your branches meet, and production is never the first place your merge is tested.**

## 3. Layer 1 — Isolation (worktree-per-session)

**The rule to broaden.** The global working agreement today says (`~/.claude/CLAUDE.md`, line 16):

> - Cross-branch work always via `git worktree add`, never checkout-switching a dirty tree.

That is written as a rule about *cross-branch* work. Broaden it to a rule about *every* session: **every session claims its own worktree, full stop.** The primary checkout is demoted from a work surface to an "integration station" — it exists to fetch `origin/main`, run integration trains (Layer 2), and observe what is actually live. Nobody edits code in it. The moment you treat the primary checkout as a place to also do a quick edit, you have re-created Race A.

**Mechanics (all first-class in Claude Code today — verify against current docs before relying on exact flag names):**

- `claude --worktree <name>` creates a worktree under `.claude/worktrees/<name>` on a fresh branch. That directory is already gitignored in this repo (`.gitignore` line 63), so worktrees never show up as untracked noise.
- Subagents accept `isolation: worktree`, so a fanned-out agent gets its own working copy instead of sharing the parent's.
- Claude locks a worktree that is in use (`git worktree lock`), so a second session can't accidentally reuse or prune it out from under the first.
- Add a **`.worktreeinclude`** file to the repo root (gitignore syntax) listing the gitignored files a new worktree needs to actually run the gate — most importantly `.env` (gitignored at `.gitignore` line 27). Without this, a fresh worktree has no `.env` and can't run `make test` or `npm run build`, which defeats the whole point of isolating work that can be verified in place. Note the repo also gitignores `.venv/` and `frontend/out/` — those are rebuilt per-worktree, not copied.

**Thin session registry (optional tooth, add only if collisions keep happening).** A small `.claude/worktrees/registry.json` mapping session → branch claim. The global `~/.claude/hooks/git_guard.sh` — which already blocks `git add -A`/`.`, `--no-verify`, and force-push to `main` — gains one more check: block a `git commit` executed *in the primary checkout* while any registered session worktree is active. Roll this out warn-first (print the warning, allow the commit) for a week, then flip to block once it's clear it isn't firing on legitimate integration-station work. Do not lead with block mode; a hook that false-positives on day one gets disabled and never re-enabled.

**What this layer does NOT do.** Isolation stops two sessions from stomping each other's files and index. It does nothing about two branches that are each internally fine but semantically incompatible — the rename-the-helper case. Separate worktrees will both build clean and both be wrong together. That is Layer 2's job, and it is why Layer 1 alone is not enough.

## 4. Layer 2 — Local integration train (`/integrate`)

This is the core new capability and the direct answer to "I want branches merged together and checked locally *before* the CI/CD pipeline." There is no staging environment for this project (test-in-prod is the documented norm), so the local machine is the only place a *combination* can be built and tested before it becomes irreversible. That is exactly the Not Rocket Science Rule that the `bors` family of tools was built on: only land a combination that was tested *as that combination*. Here, "tested as that combination" happens on your laptop.

**Mechanics, step by step.** Housed as a global skill `~/.claude/skills/integrate/` that discovers each repo's gate; it does not hard-code nbhd-united's commands.

1. **Fetch** `origin/main` so the train is built on the true tip, not a stale local ref.
2. **Create a throwaway worktree** at that tip (e.g. `.claude/worktrees/integration-<timestamp>`). The train is assembled here and thrown away after; nothing about it is ever committed to a real branch.
3. **Octopus-merge all candidate branches in one commit**: `git merge branchA branchB branchC …`. Default candidate set — every local worktree branch that is ahead of `origin/main`, plus the user's open PR branches. Overridable with an explicit list when you only want to test a specific pair.
4. **If the octopus merge refuses** (git's octopus strategy refuses the *whole* merge the moment any pair needs manual conflict resolution), fall back to pairwise merges to **name the offending pair**, report exactly which two branches conflict and on which files, and stop. That refusal is a free cross-branch collision detector — it is Race A and the textual half of Race B caught before a single byte reaches GitHub.
5. **On a clean merge, run the repo's CI-mirroring gate.** For nbhd-united that is the pre-push checklist from `docs/agents/workflow.md`, which mirrors what `ci-cd.yml` will run:
   - `.venv/bin/ruff check` **and** `.venv/bin/ruff format` (CI runs `ruff format --check .` as a separate, stricter gate — `ci-cd.yml` line 113 — so a passing `ruff check` is not enough).
   - `python manage.py makemigrations --check --dry-run` (CI fails on any model↔migration drift — `ci-cd.yml` line 153).
   - The tenants RLS relock check where a migration touches `public.*` tables (`python manage.py test apps.tenants.test_public_schema_lockdown`), per the workflow playbook.
   - `python manage.py test apps/` (the full backend suite — `ci-cd.yml` line 156).
   - `cd frontend && npm run build` (the static export CI builds at `ci-cd.yml` lines 71-73).
6. **Green → write a stamp file** in the repo's git dir (e.g. `.git/integration-pass.json`) recording `{ origin/main sha tested against, the branch heads that were in the train, timestamp }`. The stamp lives in `.git/` precisely so it is impossible to commit it by accident — it is machine-local proof, not repo content.
7. **Enable `git rerere`** (`git config rerere.enabled true`) so that when you rebuild the train after one branch moves, git replays the conflict resolutions you already made instead of asking again. Retrains stay cheap, which is what makes running the train routinely feasible.

**Housing and per-repo discovery.** The `/integrate` skill discovers the gate for whatever repo it's run in: it looks for a `make integrate-gate` target first, and falls back to the `workflow.md` pre-push checklist if there isn't one. Each repo provides the `integrate-gate` target via harness-init (Layer 4). nbhd-united's Makefile today has `test`, `lint`, and `harness` targets but no `integrate` target — adding `integrate-gate` is part of Phase 2/3 here.

**Enforcement (phase-2 tooth).** `git_guard.sh` intercepts `gh pr merge`. When **more than one** of the user's PRs is open and mergeable at once — the actual precondition for Race B and Race C — require a fresh stamp that covers this PR's head sha. Warn-first, then block, same rollout discipline as Layer 1. Merging a single lone PR requires no train and no stamp; the train is only mandatory when there is genuinely more than one thing in flight.

**After a green train.** Merge the PRs in dependency order. With Layer 3 live, the pipeline finish-order no longer matters for correctness — production converges to the newest code regardless — but merging in dependency order keeps the intermediate `main` commits individually sane.

**Rejected alternatives (and why).**
- **GitHub-native merge queue.** Plan-gated: a private repo needs GitHub Enterprise Cloud to get it, which this repo does not have. It also requires CI to run on the `merge_group` event, which roughly doubles CI cost (every candidate is built once on the PR and again in the queue). Not available and not free even if it were.
- **Hosted merge bots (Mergify, bors-ng).** They work, but they add an always-on external service to operate, authorize, and pay for. Revisit only if the local train proves insufficient in practice — the local train costs nothing and needs no third party to hold a token.

## 5. Layer 3 — Serialized landing (CI)

This layer is mostly a pointer: **implement `docs/deploy-concurrency-directive.md` exactly as written.** That means the workflow-level `concurrency` block (`group: ci-cd-${{ github.ref }}`, `cancel-in-progress: false` on `main`), the pre-deploy freshness guard on **both** `deploy-backend` (`ci-cd.yml` line 281) and `deploy-frontend` (`ci-cd.yml` line 517), and the skip-green behavior for a superseded run. Read that doc for the exact YAML and its acceptance criteria; do not re-derive it here.

**Why the concurrency group is load-bearing and the guard is only a backstop.** The freshness guard is point-in-time — it checks "am I still the tip of `main`?" at the *start* of the deploy job. But the backend job then spends minutes building ~2.5GB images before it ever calls `az containerapp update`. A newer commit can win the race in that window (classic time-of-check to time-of-use gap). So the guard cannot be the only defense. The `concurrency` block is what actually serializes the runs so two deploys are never in flight together; the guard is what saves you if concurrency is ever removed, misconfigured, or an old run is re-run by hand (`gh run rerun` makes that one click). **Never ship the guard alone.**

**Middle-run supersession is correct, not a bug.** With `cancel-in-progress: false`, GitHub keeps at most one running plus one pending run per group; a third push supersedes the *pending* (middle) run. For a deploy that is exactly right — the newest commit contains the middle commit's code, so dropping the middle run's deploy loses nothing and converges to newest faster. (Research surfaced a claimed `queue: max` FIFO option that would instead run every queued item in order; treat that as **unverified against current GitHub docs**, and note we don't want it here anyway — converge-to-newest is the correct semantics for a deploy, and running the superseded middle commit would just deploy older code for no reason.)

**NEW addition this directive makes on top of the companion — the post-deploy identity assertion.** The health check in `ci-cd.yml` (lines 377-398) verifies *liveness*, not *identity*: it polls `/health/` for a 200. Old code is perfectly healthy — which is exactly why the 2026-07-09 incident was all-green. Add, immediately after the health check passes, an assertion that the serving revision's image sha equals `${{ github.sha }}`; on mismatch, **fail the run RED.** Concretely: query the active revision's image reference (via `az containerapp revision list` / `az containerapp show`) and confirm its tag is `django:${{ github.sha }}`. This converts the incident's silent-green into an unmissable red — the one signal that was missing when it actually happened. (Note the deploy also pushes `django:latest` on every run — `ci-cd.yml` lines 330 and 333 — so assert against the immutable full-sha tag, never `:latest`, which by definition always looks "current.")

**Also fix the `/deploy` skill.** The global `~/.claude/skills/deploy/SKILL.md` currently contradicts this repo in two ways, and both are in scope for Phase 0. First, its verify step (Step 7) only checks that "the latest revision should show Running status and 100% traffic weight" — the exact liveness-not-identity blind spot that let the incident pass. Change it to compare the serving revision's image sha against `git rev-parse origin/main`, the same property the pipeline now asserts. Second, the skill assumes a **no-PR, push-straight-to-the-branch** workflow ("commit directly to the current branch. Do not create a feature branch or open a PR") — which is false for this repo, where `main` is protected and everything goes through a PR. The skill's workflow model needs to match the repo's actual PR-based reality, or it will keep steering sessions toward a flow the branch protection rejects.

**Residuals this layer does not fix (documented, not silently left open).**
- **Within-run FE/BE skew.** `deploy-frontend` and `deploy-backend` run in *parallel* inside the same run, so even a perfectly clean deploy has a brief window where the new frontend is live against the old backend or vice versa. The guard fixes *cross-run* mismatch; it cannot fix this *within-run* window. The fix for that is discipline, not pipeline config: expand/contract (additive-first) migrations and additive API changes, so an old client against a new server — or the reverse — still works during the handover. Capture this as a `workflow.md` note, not a workflow change.
- **Boot-time migration skew.** Migrations run at container *boot* (`startup.sh` line 5), not at deploy time. So an older image that boots *after* a newer image already migrated will run old code against a new schema. That is safe for additive migrations and breaks on destructive ones — which is the same expand/contract discipline as above. The freshness guard is what prevents the stale-image boot in the first place; expand/contract covers whatever within-run skew remains.
- **Manual recovery must always end with the identity check.** The incident's recovery was a direct `az containerapp update` with the correct sha (see the companion doc §1). Any manual recovery like that must finish by confirming the serving revision's image sha equals `origin/main` — otherwise you have no idea whether the recovery itself raced something.

## 6. Layer 4 — Propagation and cross-repo

None of the above holds if it lives only in one repo's docs and one person's habits. The `harness-init` skill already exists to install and refresh a repo's CLAUDE.md router, `docs/agents/` playbooks, and CI-mirroring hooks; extend its templates so this discipline propagates instead of being re-explained per repo.

**Template additions:**
- The **worktree/session iron rule** goes into the CLAUDE.md router template: every session claims its own worktree; the primary checkout is an integration station, not a work surface.
- A **`workflow.md` "parallel work & deploy serialization" section**: what an octopus-refusal means, what a "cancelled (superseded)" main run means, the identity-assertion the pipeline now makes, and the expand/contract discipline that covers within-run skew.
- An **`integrate-gate` make-target stub** so `/integrate` can discover the gate uniformly across repos.
- **Refresh-mode CI audit checks**: when refreshing an existing repo, diff its deploy workflow for the presence of a `concurrency` block and a post-deploy identity assertion, and propose them if missing — the same way refresh mode already re-audits lint gates.

**Rollout order:** nbhd-united first (it is the repo the incident happened in and the reference implementation), then nbhd-ios, then the remaining repos via `harness-init` refresh.

**Cross-repo contract gate (phase 4, optional).** The Django backend and the iOS client evolve independently; today a breaking serializer change on the backend is discovered when the iOS app breaks at runtime on a device. Turn that into a local red test instead: check a generated OpenAPI schema snapshot (drf-spectacular) into the backend repo, and run a breaking-change diff (oasdiff) in both repos' gates. Then "backend evolved, iOS breaks" becomes a failing local gate in the backend repo *before* merge, with no staging environment required.

## 7. Phasing with acceptance criteria

Each phase is independently shippable and independently valuable. Do them in order — later phases assume earlier ones — but do not batch them into one giant change. **Verify every acceptance criterion by observing the actual behavior; a green icon or a passing static parse is not acceptance.**

### Phase 0 — Serialize the deploy (highest urgency, one PR)

Ship the companion directive's §3 (concurrency block + freshness guard on both deploy jobs), plus the new post-deploy identity assertion (§5 above), plus the `/deploy` skill fix (§5 above), plus the `workflow.md` "parallel work & deploy serialization" section.

**Acceptance:**
- The companion doc's live 2-PR race test passes: two trivial no-op PRs merged seconds apart do **not** run their deploys concurrently, and after the dust settles `az containerapp revision list` shows the serving image sha == `git rev-parse origin/main`.
- A deliberate stale re-run (`gh run rerun` on an old completed run) ends **green-skipped** via the freshness guard, with the skip loudly visible in the log.
- The identity assertion is **observed passing in a real deploy log** — not merely present in the YAML.

### Phase 1 — Worktree discipline (hours)

Broaden the global CLAUDE.md rule (§3), add a `.worktreeinclude` listing `.env` (and anything else a worktree needs to run the gate), confirm `.claude/worktrees/` stays gitignored (it is, `.gitignore` line 63), and define the `registry.json` format.

**Acceptance:** two simultaneous sessions on this repo each run in their own worktree, and **both successfully run `make test` in place** — proving the `.worktreeinclude` actually carried the `.env` each needs.

### Phase 2 — The integration train (`/integrate` + stamp + guard warn-mode)

Build the `~/.claude/skills/integrate/` skill, the `.git/integration-pass.json` stamp, `git rerere` enablement, and the `git_guard.sh` `gh pr merge` interception in **warn** mode.

**Acceptance:**
- Construct two branches that are each green alone but **red combined** — e.g. one renames a helper function that the other has just started calling. The train must catch it locally, and CI must **never** see it. This is the whole point of the layer; if the train doesn't catch this, it isn't done.
- The octopus-refusal path is exercised at least once and **names the conflicting pair** (and the files), rather than failing opaquely.

### Phase 3 — Propagate via harness-init

Add the template changes from §6 to `harness-init`, then run it (bootstrap where needed, refresh where a harness exists) on nbhd-united and nbhd-ios.

**Acceptance:** a fresh repo init produces the new router iron-rule, the `workflow.md` section, and the `integrate-gate` stub; and a **refresh** on an existing repo diffs reality against the docs and *proposes* the concurrency block + identity assertion where they're missing, rather than blindly rewriting.

### Phase 4 — Cross-repo contract gate (optional)

Add the drf-spectacular schema snapshot + oasdiff breaking-change diff to the backend and iOS gates.

**Acceptance:** a deliberately breaking serializer change **fails the local gate in the backend repo** — before merge, with no device and no staging involved.

## 8. Out of scope / do-nots

- **No required-status-checks or branch-protection changes.** The instant-merge behavior that drives the merge bursts is the same behavior dependabot auto-merge relies on (it depends on the *absence* of required checks). Changing that is a separate decision with its own tradeoffs; this directive routes around it with local gates, it does not touch it.
- **No touching single-revision mode or the poller 409 handling.** The transient Telegram poller 409 burst during every revision handover is known-benign and self-settles in about a minute. Leave it.
- **No hosted merge queue yet.** Plan-gated and/or operationally heavy (§4). The local train is the answer until it demonstrably isn't.
- **Stamp files are never committed.** They live in `.git/` by design — machine-local proof, not repo content. If a stamp ever shows up in `git status`, that's a bug in the skill.
- **Do NOT set `cancel-in-progress: true` on `main`.** Killing `az containerapp update` or a migration-running boot mid-flight creates partial states this platform has no reconciler for. This is called out again here because it is the single most tempting wrong "simplification" of Layer 3.

## 9. Context for the implementing agent

- **This working tree predates the harness router.** The branch `fix/site-publishing-reliability` was cut before harness PR #1031 merged, so `docs/agents/` and the router `CLAUDE.md` exist on `origin/main` only, not in this checkout. Read the playbook from `origin/main` (`git show origin/main:docs/agents/workflow.md`) and **branch from `origin/main`**, not from this working tree, so you inherit the router.
- **The `/deploy` skill contradicts this repo.** As covered in §5, the global skill assumes a no-PR direct-push flow and never verifies the serving sha. Fixing both is in scope for Phase 0 — do not follow the skill's current workflow model when working in this repo.
- **Both this directive and the companion are currently untracked** (they were written on this pre-router branch). Commit them together with the Phase 0 PR so the reasoning and the change land as one reviewable unit.
- **Standard repo gates still apply** to every PR you open here: worktree for the branch, stage specific files by path (never `git add -A`/`.` — the global hook blocks it), never `--no-verify`, and because there are no required status checks, `gh pr merge --auto` merges instantly — watch CI yourself before merging, and after merging verify the deploy landed by checking the serving image sha (the very property Phase 0 establishes).
