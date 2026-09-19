# S2 local Yuki test stack

Approved MJ 2026-09-19. Worktree feat/yuki-local-test-stack from 9dee7814.
No prior S2 files/commits survived at initial status check.

Fences: worktree, ~/openclaw-yuki-test, ~/codex-runs logs only. Never personal
OpenClaw state/10443, production, Azure, email, Stripe, APNs, postgres16,
loanarmy GPU jobs, or Ollama restart. Plists written here, never loaded by agent.

Plan: shared guarded gateway URL helper; isolated environment/Compose installer;
local mock share + stable mock crypto; same config apply path and runtime plugins;
MJ signup then synthetic tenant provisioning; metadata-only proof; gates/report.

Pending external inputs: harness persona v3 confirmed facts, sautai lane in-memory
secret hand-off, MJ signup, orchestrator plist loading. Do not invent these.
Read CLAUDE.md, README, docs/agents/*, BYOK guide, required source paths.
Actual generator is generate_openclaw_config (no build_openclaw_config symbol).
OpenClaw help checked with HOME/STATE_DIR/CONFIG_PATH isolated in test home.
Ollama tags confirms qwen3.8:27b-obliterated-q8 available. Docker daemon reachable;
Compose CLI absent initially. Use project nbhd-yuki-test and dedicated loopback
ports 55441 (Postgres), 56381 (Redis), 18080 (Django), 19443 (gateway).

Implementation now in deploy/local-test and guarded apps/orchestrator helpers.
Compose running dedicated nbhd-yuki-test project; migration complete; native OC
schema and sandboxed boot tests pass, no inference. 135 targeted tests passed;
expanded tests and Docker gate pending. Temporary Django HTTP smoke process was stopped and both service ports verified
free; no launchd jobs loaded. Do not act on stale PIDs from earlier logs. DB has no accounts
or tenants (MJ signup not received). Persona and sim handoff still missing.
Loanarmy process detected; do not run inference. Native OC hardcodes lifecycle
locks under /tmp; process-local Node loader shim now redirects into .state/tmp.
Docker gate initially stopped due nested .state snapshot; exclusion added.
REPORT-S2.md and deploy/local-test/README.md carry current contract/status.

Commits d866edfc + c6890085 preserve implementation. Final named-runner targeted
suite: 143 passed. Runtime num_ctx auto-injection explicitly disabled after
installed-source inspection. `.state/docker-gate.log` holds the first full Linux
gate (snapshot started before c6890085). It must be rerun against current tree
before pushing; caches in .state/docker-gate-cache avoid download repetition.
No push/draft PR yet: pre-push Docker gate still pending. All external acceptance
prerequisites remain pending. REPORT-S2 must get final gate/git status before
handback. Actual loanarmy startup refusal verified without starting gateway.
