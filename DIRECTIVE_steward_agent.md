# DIRECTIVE: The Steward — PM + watchtower agent for MJ's portfolio

**Written:** 2026-07-30 by Fable (session ab4cff5c) as a handoff to the next agent session.
**Status:** design ADOPTED, nothing built yet. Branch `feat/watchtower` (nbhd-united) is empty of implementation.
**Read this whole file before acting.** It is self-contained: you do not have the originating conversation.

---

## 1. WHY THIS EXISTS — the two incidents that define the requirements

Both happened in the week of 2026-07-23→30. Every design decision below traces to one of them.

**Incident A — the release that was never submitted.** iOS 2.1.5 was merged to main on 07-27 and everyone (including the session ledgers and later sessions' memory) believed it shipped. It hadn't: **no Xcode Cloud build was ever triggered** (builds do NOT auto-fire on main merges) and no App Store Connect version was ever created. Meanwhile 2.1.4 — which carried a foreground-freeze regression — went live to 100% of users. Diagnosis that matters: the 2.1.5 plan *succeeded*. Its stated objective literally ended at "leave a fully verified local integration branch with no push or PR," all 8 tasks marked complete. **Shipping was nobody's object.** No entity existed whose state was "release 2.1.5, phase=verified_local, next expected=submission." (Since fixed: a consolidated 2.1.5/build 40 was built, submitted, approved, and is now live on phased rollout.)

**Incident B — the personal agent dead for 7 days.** MJ's personal OpenClaw gateway died 2026-07-23 17:48 JST and nobody noticed until 07-30. Cause: the 07-23 OpenClaw self-update to `2026.7.1-2` requires Node ≥22.22.3; systemd still launched `/usr/bin/node` 22.22.2. It failed 5 times and stayed down, **with no journal entries** (crash preceded logging init). **Worse: that gateway is the delivery endpoint for NBHD's health alerts** (`_send_alert_to_personal_openclaw`, `apps/cron/views.py` ~line 1507) — so the component that died is the delivery channel for alerts about things dying. Log Analytics shows 9 alert attempts returning 502 between 07-23 and 07-29. Silent for a week.

**The requirement both imply:** *nothing tracked may occupy a state where silence is indistinguishable from fine.*

---

## 2. MJ'S VISION (his words — do not drift from these)

- Primary role: **"the project manager that is competent continues to be the focus and as a competent PM, they need the ability to track what is going on."** Guides him through issues, keeps track of his work, across 4 products.
- Secondary: **watchtower** — platform health + breaking-change tracking.
- Tertiary: consume **security tooling** output to help keep his machines safe.
- **"the thing with llms is not to trust them blindly. my intention is to use TOOLS ... the correct tools, whatever they are, to help gather data and organize them in a STRUCTURED way."**
- **"we can use a form of input validation to ensure we don't take text written to logs as commands, or use the SANDBOX for the log analysis which openclaw has the ability to create."**
- Hard constraint: **no access to secrets or user data.**
- Portfolio: nbhd (`nbhd-ios` + `nbhd-united`), sautai, The Academy Watch (`loanarmy`), YardTalk.

---

## 3. VERIFIED HOST FACTS — do not re-recon, this was done 07-30

**Personal OpenClaw** runs on a **Hostinger Ubuntu 24.04 VPS**, reachable as **`ssh openclaw`** (works non-interactively from MJ's Mac; user `openclaw`, home `/home/openclaw`, host `srv1315327`). NOT Azure, NOT Docker.

Traffic path: `agent.bywayofmj.com` → **Cloudflare Access** (service-token policy; needs a Service Auth policy or it 302s) → **cloudflared** system service (tunnel `openclaw`, id `c5db9602-809f-4ffb-8474-a399383ae9f0`, 4 edge connections, `/etc/cloudflared/config.yml`) → **`openclaw-gateway.service`** — a **USER-level systemd unit** (always `systemctl --user`, never system) → `127.0.0.1:18789` **loopback-only** → OpenRouter models.

- Install: `~/.npm-global/lib/node_modules/openclaw` (global npm — an unpinned `npm i -g` is a supply-chain event). Version `2026.7.1-2`, channel `stable`. Self-updates directly; no CI, no deploy repo.
- Config: `~/.openclaw/openclaw.json`. Tree: `agents/` (claude, claude-code, jhaughton, main) · `cron/` · `memory/` · `flows/` · `hooks/` · `extensions/` (empty) · `exec-approvals.json` · `delivery-queue/` · `workspace/` (contains working copies of nbhd-united, sautai, loanarmy, openclaw-fork + an Obsidian vault).
- Models: primary `openrouter/deepseek/deepseek-v4-pro`, fallback `openrouter/moonshotai/kimi-k2.6`.
- **Channels: Telegram ONLY** (`@ByWayOfBot`, accounts `default` + `jhaughton`; verified send 07-30 10:19). **LINE is NOT configured** — earlier memory claiming alerts relay to LINE was aspiration, not fact. Discord configured but disabled.
- Cron engine enabled, 22 jobs / 19 enabled (Morning Briefing, Weekly Progress Report, Evening Check-in, Dependency Guardian, Cron Health Monitor, …), heartbeat every 3h. `HEARTBEAT.md` already watches gateway errors / cron consecutive-failures / "wall" freshness — a substrate to extend, but it watches OpenClaw itself, not the platform.
- Capabilities present: HTTP fetch, web search, Brave, browser tooling, 13 loaded plugins.
- Secrets live at `~/.openclaw/.env`, `gateway.systemd.env`, `secrets/`, `credentials/`, `~/.cloudflared/*.json`. **Never print values.**

**The Node fix already applied (07-30):** user-local Node **v22.23.1** at `~/.local/nodejs/bin/node` + systemd **user drop-in** `~/.config/systemd/user/openclaw-gateway.service.d/10-node-path.conf` overriding `ExecStart` and `PATH`. Gateway verified: active, `[gateway] ready`, local HTTP 200, `/health` + `/ready` 200, `/v1/models` 401 without bearer (correct), external 302 (correct — CF Access).

**Hardening posture checked against known OpenClaw CVEs / the ClawHavoc malicious-skill campaign:** loopback-only bind ✅ · no ClawHub skills ✅ · no extensions ✅ · **GAP: `plugins.allow` is ABSENT** (startup warns non-bundled plugins "may auto-load: acpx") — one-line fix, do it.

---

## 4. LIVE PROBLEMS ON THE HOST (triage these; they are not hypothetical)

1. **⚠️ CRON IS BROKEN after the 07-23 upgrade.** On the 07-30 restart it loaded 22 jobs, found **17 missed runs**, and ≥9 catch-up jobs failed with `TypeError: Cannot read properties of undefined (reading 'find')` (affected: Cron Health Monitor, Morning Briefing, Morning Standup, Daily Vault Sync, Usage Snapshot, Calendly Booking Check, Social Draft Generation, Job Search Scan, Dependabot Digest). The cron store also migrated to SQLite during that startup ("Imported 74 legacy cron run logs"). **MJ's entire scheduled assistant life is degraded.** I could not reproduce the TypeError in the post-restart log window — **watch the next scheduled fire** before concluding cause. Suspicion: schema mismatch between legacy `jobs.json` and the new SQLite store, or a missing field on migrated job rows. This is *also* why the Steward heartbeat must NOT live in OpenClaw cron.
2. **cloudflared is ~6 months stale** (`2026.1.2`, recommends `2026.7.3`), `--no-autoupdate`, root-owned.
3. **Kernel reboot required + 28 pending package updates.** System Node still 22.22.2 (apt candidate 22.23.1 available).
4. **Runtime logs land in `/tmp/openclaw`** — ephemeral; outage-era evidence can vanish on cleanup/restart. Fix before you need forensics.
5. **`openclaw` user has no journal access to system units** (needs sudo).

---

## 5. THE ADOPTED DESIGN

### 5.1 The stall primitive — `Expectation`

Every tracked item in a live state carries an **armed Expectation**: a concrete next event + an evidence source that can confirm it + `due_at`/`interval` + grace.

> **Stalled ≙ an armed Expectation past `due_at + grace` with no matching evidence event.**

Enforced as a DB invariant: `status=active` ⇒ ≥1 armed Expectation; `status=parked` ⇒ a revisit Expectation exists. One table serves heartbeats, deadlines, recurrences, and blocked-on-MJ nags. **Activity-based staleness is WRONG** — it would have missed 2.1.5 entirely (a train can be active and stalled simultaneously) and it makes parked work "stall" forever.

### 5.2 Schema — 5 tables + edges. Resist growth; this is not Jira.

- **TrackedItem** — `product` · `kind` (work|release|blocked_on_mj|recurring|infra_watch) · `title` · `context` (bounded prose, as an *attribute* of a queryable row) · `status` (proposed→active→blocked→parked→done→abandoned) + `blocked_on` · `refs` (typed pointers: repo+branch, PR#, CONTINUITY path, ASC id — point at artifacts, never duplicate them) · `provenance` (mj | collector | agent_proposed — agent items are visibly second-class until MJ confirms).
- **Expectation** — `subject_item` (nullable: pure infra heartbeats need no item) · `kind` (heartbeat|deadline|recurrence) · spec (`interval_s+grace_s` | `due_at+grace_s` | `cron_expr+grace_s`) · `evidence_source` (ENUM naming a collector, never free text: github_pr_merged, asc_version_state, gateway_heartbeat, ci_run, mj_ack…) · `state` (armed|satisfied|missed|retired) · `last_satisfied_at` · `miss_count` · `on_miss` (urgent|digest) · `owner`.
- **EvidenceEvent** — append-only facts. `source` · `subject` · `occurred_at` · `payload` (typed, size-capped) · `fingerprint` (dedup key — `PlatformIssueLog`'s biggest gap is having none) · **`trust` (authenticated_api | host_log | untrusted_text — the quarantine keys on this)** · `provenance`. **ONLY collectors and MJ may satisfy Expectations. The agent CANNOT.** ("Agent is never ground truth" as a foreign-key rule.)
- **ReleaseTrain** — its own table because releases are the highest-value dropped baton. `phase`: planned→integrating→verified_local→pushed→ci_green→tagged→submitted→in_review→released (+rolled_back). **Each phase entry auto-arms the next phase's Expectation** with a per-product SLA. This is the machine that would have held 2.1.5.
- **Decision** — append-only `decided_at` · `decision` · `rationale` · `alternatives_rejected` · `refs` · `supersedes` (self-FK). Reversals are new rows.
- **DependencyEdge** — `from_item` · `to_item` · `kind` (blocks|release_order|shared_contract). Powers stall *propagation* (A stalls, A blocks B ⇒ B's digest line names A).

### 5.3 Storage: Django `apps/steward` — NOT OpenClaw memory

Three reasons, in weight order: (1) **the stall detector must run off the box being watched** — if the ledger lived in OpenClaw's memory tree, the detector for "the VPS died" would live on the VPS, which is exactly the architecture that just failed for 7 days; (2) **the write boundary is only enforceable at a validating, authenticating API** — a filesystem tree can't refuse a write, validate schema, or distinguish provenance; (3) durability/backups/admin, and other agents can read it. OpenClaw memory = **disposable cache**; truth flows Django → memory, never back.

**Do NOT extend `PlatformIssueLog`** (`apps/platform_logs/models.py`): it's tenant-scoped with `on_delete=CASCADE`; PM state is portfolio-scoped and must not vanish with a tenant. **Inherit its contract** ("detail: no user PII") and severity vocabulary only.

### 5.4 The write boundary — precise version

> **The agent may write only to sinks where NO code path executes as a consequence of the write without MJ in between.**

"Never MJ's machines" is too loose. GitHub labels trigger Actions. Issue comments land in *future agent sessions'* context — a self-injection loop. So: **GitHub stays READ-ONLY** (MJ's team reads Telegram, not GitHub; label-writing adds risk and buys nothing he'd notice). Its writable sinks are: the `apps/steward` tables via a narrow typed API (new agent-principal key class derived from `apps/integrations/internal_auth.py` — portfolio-scoped, steward endpoints only, NOT a tenant key), its own memory cache, and messages to MJ.

**The back-door rule (name it in code review):** rows with `provenance=agent` may influence **display only** — digest content and ordering. Anything that changes system behavior (auto-closing a cron, muting a monitor) requires MJ-provenance confirmation. Otherwise the agent programs the machine by writing bookkeeping.

**Approval channel:** approvals must travel a channel the agent cannot write to; the approval object (what exactly was approved + hash) must be constructed by CODE, not by the agent's restatement. Verify whether `~/.openclaw/exec-approvals.json` checks MJ's sender identity deterministically before relying on it.

### 5.5 Quarantine for untrusted text

Privileged context (tools, memory, messaging) **never sees raw untrusted bytes.** Pattern: deterministic shippers pre-parse to structured events (Laurel/journald JSON) → **quarantined pass**: fresh, tool-less, network-less, memory-less LLM invocation → rigid-schema JSON out → **deterministic validator** → privileged context sees only validated typed verdicts.

**The validator is the boundary; sandbox walls are defense-in-depth.**

Anti-suppression (the *measured* threat — persona-hijacks suppressed 68% of genuine alerts in published research; injection succeeds ~96% on naive summarization; best in-band filtering only gets 26.6%→11.8%, so architecture over filtering):
1. **The LLM never decides whether an alert fires.** Deterministic rules select alert-worthy events *before* the LLM exists in the path; the LLM only enriches/clusters/annotates. A hijacked pass can emit garbage; it cannot un-fire an alert.
2. **Accounting invariant:** N event IDs in ⇒ exactly N dispositions out; the validator counts. "Quietly omit the SSH events" fails mechanically.
3. **Substring rule:** any quoted material must be a literal, length-capped (~200 char) substring of the input, checked mechanically. Prevents laundering invented text as evidence.
4. **Canary events** with known-required dispositions injected into each batch; a miss ⇒ discard verdict + raise a deterministic alert the attacker never sees.
5. **Fresh context per batch is non-negotiable** — a poisoned batch must not condition the next.

### 5.6 Tools gather, LLM judges — the exact line

**Code, no exceptions:** all collection · thresholds · the stall sweep (a timestamp comparison — no judgment in "expected Tuesday, absent Thursday") · dedup/fingerprinting · cooldowns and message budgets · correlation by key · schema validation · canary/accounting checks · release-phase transitions · **rendering the facts section of every digest**. Note: the entire origin incident is solved in this column with zero LLM.

**LLM, because a rule genuinely can't:** (1) unstructured→structured at ingestion (novel error text → typed vocabulary; extracting items/decisions from the ~60 `CONTINUITY_*.md` files; turning a session transcript into proposed ledger deltas) — all landing as `agent_proposed`; (2) structured→narrative at presentation (prioritization, "do the cert first because…" — this is the *competent* in competent PM); (3) relevance judgment for breaking changes (does this Node/dep change affect this stack, including transitively — the exact class that killed the gateway); (4) anomaly **narration**, never detection.

**Never LLM:** whether anything fires, is late, or is severe within known categories; whether an Expectation is satisfied.

### 5.7 Watch-the-watcher — 3 layers, then STOP

- **L0 (exists):** QStash fires Django every 5 min — different failure domain, survived the dead week. Register the sweep via the existing self-healing `apps/cron/system_cron_registry.py` (that reconcile pattern came from incident 2026-07-09b; reuse verbatim so the sweep can't silently drift).
- **L1 (the fix):** **urgent alerts go Django → Telegram Bot API DIRECT**, with Mailgun email fallback. Remove the agent from the urgent path entirely; keep the agent-routed path for digest-class enrichment only. *The agent's death must produce a message no agent handles.*
- **L2:** one dumb external dead-man (healthchecks.io-style) pinged by Django's sweep; it emails MJ if pings stop. Azure alerts + QStash DLQ back it up.
- **Do not add a third custom watcher** — that's maintenance surface pretending to be safety.

Agent heartbeat: an OpenClaw cron POSTs a signed constant payload to a steward endpoint every 15–30 min. **The body is boring by design** — it proves liveness, not health. Health claims from the agent are `provenance=agent` and satisfy nothing. (Given §4.1, verify OpenClaw cron actually fires reliably, or use a plain systemd timer + curl on the VPS instead — a timer is more reliable than the broken cron engine and needs no agent.)

### 5.8 Digest shape and message budget

- **One daily digest, two parts:** (a) **code-rendered FACTS** the LLM cannot edit — missed expectations, blocked-on-MJ items with ages, release-train phases, unconfirmed agent proposals; (b) LLM narrative/prioritization **below** it. This makes digest-level suppression structurally impossible.
- **Urgent interrupts only for:** agent/gateway heartbeat miss, production tenant health, security-critical, L2 events.
- Dedup + cooldown per fingerprint (reuse the delivered/timeout/transient/undeliverable classification and differentiated cooldowns already proven in `run_health_check`).
- Urgents carry an ack; unacked after N hours ⇒ one resend, then email. MJ's ack is an MJ-provenance EvidenceEvent.
- **Blocked-on-MJ nag decay: 2, 5, 10 days — never daily.** A skimmed digest is a dead digest (SOC research: 50–80% false-positive rates make humans stop trusting alerts entirely).

### 5.9 Autonomy ceiling

**Observe → recommend → propose-with-confirm → (never) act.** Fable's position, endorsed by research: **permanent ceiling at propose-with-confirm for anything touching machines.** Ratchet **BREADTH** (more projects, more signals, better correlation), never **POWER**. Blocking/remediation stays with non-LLM tooling (fail2ban/CrowdSec/WAF) — NIST SP 800-94: automating prevention converts false positives into outages. **MJ has not yet explicitly ratified this ceiling — get his confirmation.**

### 5.10 Security tooling verdict (MJ asked about Snort — answer: no)

**Skip Snort.** Inbound TLS terminates at Cloudflare; the packet stream reaching the host is inside cloudflared's own encrypted connection, so a packet NIDS sees ciphertext. If a NIDS is ever wanted it's **Suricata** (better EVE JSON, active project) and only for **egress/DNS/JA3 beaconing + the plaintext loopback hop**. Also skip: Zeek, Wazuh full stack (4CPU/8GB = the whole VPS), OSSEC (dormant), host-side WAF (Cloudflare edge does it).

**Recommended stack, value-per-effort order:** SSH key-only + no root (ideally SSH *behind* the tunnel ⇒ zero open inbound ports) · verified unattended-upgrades · UFW default-deny + loopback-bound apps · **Ubuntu Pro free tier (≤5 machines) = Livepatch rebootless kernel CVE patching + 10yr ESM** (directly retires the reboot treadmill) · CrowdSec (JSON API `cscli alerts list -o json`; fail2ban is slow-maintenance/unstructured — but keeping fail2ban is defensible; **never run both banning SSH**) · **auditd + Laurel** (JSON Lines built for exactly this consumption) · **AIDE** daily FIM · journald `-o json` + **Forward Secure Sealing** · **gateway access log as JSON** (this is the authoritative inbound record: Cloudflare Free gives no Security Events analytics, and Logpush is Enterprise) · ship off-host via **Azure Arc + AMA → the existing Log Analytics workspace** (Arc onboarding free; 5GB/mo free tier covers a tuned host ≈ $0; same KQL surface). **Sentry is NOT a security log store.**

**TUNING GATE — non-negotiable:** run everything observe-only 2–4 weeks, suppress top benign patterns weekly, reach **<20 actionable events/day BEFORE the agent consumes anything.** Untuned reality: SSH brute force = hundreds–thousands/day of background radiation; untuned Suricata+ET Open = same; raw auditd execve on an agent host = tens of thousands/day. The agent sees **summaries and anomalies** (new source succeeding, success-after-failures, odd-hour logins from new ASNs), never raw attempts.

---

## 6. PHASING — build in this order

1. **Heartbeat + direct alerting. Days, ZERO LLM.** `apps/steward` with just `Expectation` + `EvidenceEvent`; QStash-fired sweep registered via `system_cron_registry`; **Django→Telegram direct send**; L2 external pinger. Seed three expectations: gateway heartbeat, current release-train deadline (manual row), CI-green-on-main. **This alone would have caught both origin incidents.** Smallest slice with value MJ notices within days.
2. **PM ledger v0 (the actual ask).** TrackedItem, ReleaseTrain, Decision, DependencyEdge + GitHub and ASC collectors + the two-part daily digest (facts first; narrative can lag) + a one-time LLM extraction pass over the ~60 `CONTINUITY_*.md` files, **every item MJ-confirmed** before it counts + a session-end proposal hook alongside the existing `yardtalk-push` flow.
3. **VPS hardening + telemetry tuning — parallel, independent workstream** (§5.10, incl. the `plugins.allow` fix, egress allowlist, OpenClaw version pin, journald sealing, moving logs off `/tmp`).
4. **Agent narrative + proposals.** Digest composition, "what's the state of X" on demand, ledger-delta proposals. Ceiling per §5.9.
5. **Quarantine broker, then security telemetry in SHADOW MODE** for weeks — verdicts logged, not surfaced; measure canary pass-rate before MJ ever sees an LLM-annotated security line.
6. **Breaking-change tracking last** — highest LLM-judgment content, lowest urgency, and it benefits from the inventory the collectors accumulate by then.

---

## 7. EMPIRICAL FINDINGS (codex audit, 2026-07-30 — settled; do not re-audit)

**Headline:** buildable — but **OpenClaw is the REPORTING layer only. It is not the trusted collector, not the stall detector, not the remediation engine, and not the dead-man monitor.**

### 7.1 The sandbox answer (MJ was partly right)

OpenClaw **does** have native **Docker-based tool isolation** — but it is **`mode: off` on this host today**, both agents resolve unsandboxed, `openclaw sandbox explain` reports `sessionIsSandboxed: false`, and no sandbox image is installed. `exec-approvals.json` is a command-approval policy, **not** a sandbox.

Config surface: `mode` (off | non-main | all) · `scope` (agent | session | shared — shared weakens separation) · backends Docker/SSH/OpenShell · Docker `network:"none"`, read-only root, `capDrop: ALL` · workspace access none/ro/rw · relocatable tools (exec, read, write, edit, apply_patch, process, optionally browser) · layered allow/deny (global → provider → agent → sandbox → subagent; **earlier denials cannot be re-granted**) · elevated tools can bypass the sandbox — **keep disabled**.

Suggested profile for analysis work: `mode: all`, `scope: session`, `backend: docker`, `network: none`, `readOnlyRoot: true`, `capDrop: [ALL]`, a single read-only bind of an inbox dir, expose only `read` (+ `write` scoped to its own sandbox workspace). **Deny at minimum:** `exec, process, message, gateway, cron, browser, web_fetch, web_search, sessions, nodes, apply_patch, edit`.

**Two corrections to the quarantine design in §5.5:**
1. **A literally tool-less analysis subagent is NOT possible** — an effective allowlist of zero tools fails before the model call. A narrow read-only profile is the achievable equivalent.
2. **Plugins execute unsandboxed inside the gateway process**, and OpenClaw's own docs decline to call the Docker sandbox a perfect security boundary.

**⇒ Consequence (important):** native isolation is adequate for **already-sanitized, schema-validated observations**, and must **not** be trusted to ingest raw production logs. **Raw-log reduction belongs in deterministic Django code or a separate disposable parser process; only the typed summary reaches a model.** This *strengthens* §5.5's core claim — the validator is the boundary, and the sandbox is defense-in-depth.

### 7.2 Cron (explains §4.1 and kills one design option)

Cron state **migrated from `cron/jobs.json` into `~/.openclaw/state/openclaw.sqlite`** (the old JSON is gone; backups remain). Cron *can*: persist across restarts, run at/interval/cron-expr/on-exit schedules, invoke an agent turn with a per-job model **and tool restriction**, inject a system event, run a host shell command **with no model**, deliver by announcement/webhook/none, record run+delivery status, retry with escalating backoff.

Limits that matter: **the gateway must be alive** · overdue isolated agent turns are *rescheduled*, not replayed as catch-up · **OpenClaw cron therefore cannot detect that the OpenClaw gateway was dead** · command-cron is privileged host automation **not** governed by agent `exec` policy · at audit time most active jobs had **no job-level tool pin** and only a minority had failure alerts.

**⇒ The dead-man MUST be external to OpenClaw** (confirms §5.7). Also: a **plain systemd timer + curl** is the more reliable heartbeat emitter than the cron engine, and needs no agent at all.

### 7.3 Agents, tools, plugins, memory, messaging

- **Only TWO configured agents exist: `main` and `jhaughton`.** The `claude` / `claude-code` dirs are historical session metadata, not agents. An agent can have its own workspace, model, tool policy, sandbox policy, heartbeat, skills, memory index — but **separation is application-level, not an OS boundary**, and auth profiles may **fall back to main's** when an agent-local profile is absent. A new `pm-watchtower` agent must be configured explicitly and must **not** inherit main's surface.
- **`main` currently resolves to ~41 tools** including host exec, file write/patch, gateway+cron admin, messaging, browser, web fetch/search, session/subagent control, MCP. OpenClaw is also an **MCP client** (an HTTP MCP server is configured). **Deny MCP to the PM agent** — Django exposes one typed snapshot, not a cloud-tool surface.
- **`plugins.allow` absent is materially dangerous** because **native plugins run in the gateway process, outside any agent sandbox** — a malicious plugin ≡ arbitrary gateway-process code. Semantics: `allow` = exclusive allowlist · `deny` wins over allow · `entries.<id>.enabled:false` disables · `load.paths` adds locally discovered code · `entries` configures but does **not** establish trusted provenance. **Freeze the reviewed current set first, then reduce** (codex drafted the exact `config set plugins.allow [...]` + `config validate` + `systemctl --user restart` sequence; ~18 observed IDs incl. acpx, active-memory, browser, canvas, device-pair, file-transfer, google, memory-core, ollama, openrouter, phone-control, talk-voice, telegram, browser-site-memory, brave, exa, perplexity, tavily — no sudo needed).
- **Memory is agent-scoped:** `<workspace>/MEMORY.md` + `memory/YYYY-MM-DD.md` + `~/.openclaw/agents/<agent>/agent/openclaw-agent.sqlite`. Main currently holds **225 files / ~1.5 MB / 1,105 indexed chunks**, and **main's overall session state is ~1.8 GB**. No demonstrated automatic retention for Markdown memory; embedding cache effectively unbounded unless configured. Confirms memory ≠ system of record.
- **Messaging:** Telegram configured, **LINE absent**. **If an agent has the `message` tool, the MODEL chooses channel/account/recipient** — `allowFrom` restricts inbound senders only, it does **not** pin outbound recipients. Cron *can* pin `delivery.channel` / `delivery.accountId` / `delivery.to`, but current jobs embed delivery instructions **in prose, which is not enforcement**. **⇒ Deny `message` to `pm-watchtower`; pin delivery via cron config or a deterministic sender service; never let a generated tool call choose the recipient; keep destination + channel credential outside the sandbox.**
- **Version pinning:** `2026.7.1-2` (CLI commit `0790d9f`), requires Node ≥22.22.3. **There is no persistent desired-version pin** — an unqualified `npm i -g openclaw` or `openclaw update` follows the dist tag and can re-break the Node floor. **The base unit still points at `/usr/bin/node`, so losing the drop-in recreates the 7-day outage.** Fix: install an explicit version, keep the desired version in source control, and add an **`ExecStartPre` guard asserting both Node and OpenClaw versions**.

### 7.4 Data sources — auth boundary and per-product truth

**GitHub:** MJ's Mac has authenticated `gh`. **The VPS has NO usable GitHub API auth** (`gh` not installed, no `GH_TOKEN`/`GITHUB_TOKEN`; SSH keys allow git transport only). **⇒ Django should use a GitHub App installed on just the four repos, read-only: Metadata, Contents, Pull requests, Actions, Checks/statuses.** A fine-grained read-only PAT is cheaper to start, worse for lifecycle/attribution. **No GitHub credential reaches OpenClaw.**

Per product (point-in-time 2026-07-30): **nbhd-ios** private, ~4 open PRs, no GitHub workflow / no release-tag truth, Xcode Cloud execution not visible in-repo, ASC needs a Django-side key · **nbhd-united** ~9 open PRs, CI/CD + Dependabot, **already has the strongest deploy check: CI waits for the exact serving SHA via `config/health.py`** · **sautai** private, 15 open Dependabot PRs, `/healthz/` returns only `ok` — **cannot prove which SHA is serving** · **loanarmy (Academy Watch)** public repo, ~9 open PRs, 5 workflows, `/api/health` has a static API version not a build SHA, several PRs with failing/missing checks · **yardtalk** private, 3 GitHub releases (latest `v1.2.0`), no tracked CI workflow, installed Mac app version not remotely queryable.
**Prerequisite for authoritative deploy checks:** add build identity (SHA) to sautai's and loanarmy's health responses.

**Continuity ledgers as PM substrate — NO.** Counts: nbhd-ios ~44 `CONTINUITY*.md` + 6 `docs/agents/`; nbhd-united ~26 + 7; sautai ~12 + 6; loanarmy ~16 + 7; yardtalk none. Newer ones have regular headings (Goal / Constraints / Decisions / Done-Now-Next / Working Set) so they're **mechanically segmentable** — but state is prose, there are **no stable IDs**, freshness isn't enforced, relationships aren't structured, worktree copies duplicate, formats vary, and **"done" in prose does not prove deployment or submission**. **⇒ Use as qualitative context only, tagged with repo path + commit SHA + mtime; never let prose override GitHub/Azure/ASC/check results.**

**Reusable nbhd-united components (verbatim from the audit):** `config/health.py` (serving build identity — reuse as-is) · `apps/evals/models.py` `EvalRun`/`EvalResult` (**strong reuse** — IDs/counts/durations/pass-fail with no user content; add a sanitized read projection) · `apps/evals/suites/slo_snapshot.py` (deterministic SLO + stale-run concepts) · `apps/platform_logs/models.py` (categories/severity/resolution — but **do not expose tenant/detail rows as-is**) · `apps/orchestrator/services.py` (health logic, **must aggregate** — current results contain tenant display names/container ids) · `apps/orchestrator/azure_client.py` (authenticated client; add read methods for revisions/images/provisioning/traffic) · `apps/orchestrator/config_security.py` (deterministic finding types) · `apps/cron/views.py:TASK_MAP` (executable task allowlist) · `register_system_crons.py:SYSTEM_CRONS` (desired schedule state) · `apps/cron/system_cron_registry.py` (live-schedule listing + convergence). **`apps/platform_logs/views.py` is NOT reusable as a read API** (it's a write endpoint). **`CronJob` is desired-state, NOT an execution-success ledger.**
**⇒ The missing backend primitive: a durable record of generic scheduled-task EXECUTION** (expected run, start, finish, result, error class). **QStash delivery success does not prove the application task completed.**

**ASC:** the Mac wrapper (`~/.claude/skills/asc/`) mints short-lived JWTs and works, but **is not integrated into Django**. Build a Django ASC collector with a **dedicated read-only ASC key in Key Vault**. Agent receives only: version id, state enum, build number/status, submission/review state, timestamps, phased-release state — **never** key metadata, tester data, customer data, or review communications. ⚠️ **APNs `.p8` ≠ ASC API key — not interchangeable.**

**QStash:** stays Django-side. The PM agent needs **no** Upstash MCP tool or credential.

### 7.5 Stall-signal catalogue (implementable now)

**For the two origin incidents:** *release* → version/release-branch push with no new ASC build after N hours · processed build not attached to a version · version stuck `PREPARE_FOR_SUBMISSION` N days · no review submission once a build is eligible · live version ≠ repo release intent. *Gateway* → external dead-man receives no heartbeat · `systemctl --user is-active` false · local gateway health command fails · **running Node below the package's declared floor** · no recent healthy-start record in the journal.

**Work-in-flight:** branch with commits not in default and no PR after 2–3 days · PR with no activity after N days · draft PR still draft after N days · PR dirty/conflicted/blocked/missing required checks · default-branch commit with no CI run after 10–15 min · expected workflow never ran for a path/ref · release tag with no GitHub release or expected artifact.
**Deployment:** deploy workflow green but no Azure revision created · revision failed/degraded/inactive/no-traffic · serving image ≠ intended commit · **serving `/health.build` ≠ merged SHA** (nbhd-united already does the strongest version of this) · desired image tag vs active revision disagree · backend deployed but frontend not · health endpoint unavailable or stale build age.
**App Store:** release-intent bump with no uploaded build in N hours · build stuck processing/invalid · processed build unattached · version past threshold in `PREPARE_FOR_SUBMISSION` · missing review submission.

### 7.6 Still to verify (not settled by this audit)

- **Telegram send-only from Django** alongside OpenClaw's channel ownership — does a second `sendMessage` producer conflict with the gateway's polling consumer? If yes, use a **second bot** for urgents.
- What the tenant health checks actually reported during the dead week (Log Analytics should show 7 days of `undeliverable`/`timeout` — confirms the inversion diagnosis and calibrates urgents).
- Whether any repo has meaningful public surface/external contributors (**loanarmy is public**) — sets the trust class of PR/issue bodies for §5.5.
- Root cause of the cron `TypeError` (§4.1) — watch a real scheduled fire.

---

## 8. OPEN DECISIONS FOR MJ — ask, don't assume

1. **Ratify the permanent propose-with-confirm ceiling** for anything touching machines? (Fable's recommendation; MJ hasn't explicitly agreed.)
2. **Telegram as the urgent channel, or wire LINE properly?** (Telegram is the only proven path today; MJ originally expected LINE.)
3. **Chase the broken OpenClaw cron now?** It degrades his assistant daily and is independent of this project.
4. Ubuntu Pro free tier — worth enabling for Livepatch? (Retires the reboot treadmill.)
5. CrowdSec migration, or keep fail2ban? (Both defensible; never both banning SSH.)

## 9. WHAT NEEDS MJ'S HANDS (sudo on that box REQUIRES a real TTY — chat-run commands CANNOT sudo, even with `ssh -t`)

```bash
# system Node to standard (the gateway now runs on its own user-local Node, so this is hygiene not emergency)
ssh -t openclaw "sudo apt-get install -y nodejs && node --version"
# pending updates + kernel reboot
ssh -t openclaw "sudo apt-get update && sudo apt-get upgrade -y && sudo reboot"
# cloudflared is ~6 months stale
ssh -t openclaw "sudo journalctl -u cloudflared -n 200 --no-pager"
```
Cloudflare Access **policy** inspection needs MJ's interactive Zero Trust login. Tunnel metadata does not: `ssh openclaw 'cloudflared tunnel list'`.

---

## 10. OPERATIONAL LANDMINES (each of these cost real time)

- **`systemctl --user`, never system** for the gateway. The unit lives at `~/.config/systemd/user/`.
- **sudo needs a TTY** — chat-run `ssh -t` still fails. Anything requiring root goes to MJ as a copy-paste command.
- **The gateway crashed with ZERO journal entries** — don't trust "no logs" as "no crash." Reproduce by running the ExecStart line manually with a timeout; that's how the Node floor was found.
- **codex CLI outside a git repo needs `--skip-git-repo-check`** or it exits 1 immediately.
- Watchdog monitors should **count completion sentinels** rather than pattern-match, and must not match strings that appear in source code being read (a "usage limit" UI string in the iOS repo tripped a false quota alarm).
- **Session-only state dies with the chat:** a 4-hourly 2.1.5 rollout-watch cron was armed in the originating session and **lapses when that session ends** — re-arm or drop deliberately (see §11).
- Never print secret values from that box; several env/credential paths are listed in §3 for existence-checking only.

---

## 11. ADJACENT LIVE STATE (so you don't trip over it)

- **iOS 2.1.5 (build 40) is LIVE on the App Store, phased rollout ACTIVE** (ASC version `a05489e9-7edc-437d-ae13-ea61459289be`; pause lever = its `appStoreVersionPhasedRelease`). A 4-hourly rollout-health sweep existed in the originating session and **is now lapsing** — decide whether to re-arm. Day-1 sweeps were all clean; the only recurring noise is a **pre-existing** `cron.remove` Gateway-500 in the ghost-job reconciler (`apps/cron/post_reconcile.py` `_sweep_ghost_jobs`) that predates the release and is queued for its own fix.
- **iOS PR #104 merged** (dead token-bearing WebView deleted + static privacy pin test). Rides the next train.
- **Parked with intent, pushed branches:** `nbhd-ios feat/privacy-batch` (workout-draft repository + friend-thread retirement lifecycle) and `nbhd-united feat/friends-retirement-cutoff` (universal friendship lock + retirement/acceptance cutoffs, migrations 0013+0014, certified sound). **Unparking blocker:** the draft staging path uses detached unstructured tasks with no FIFO guarantee — needs a redesign into one ordered pipeline into the repository actor. Backend ships only with the iOS side.
- **Backend meditation reliability is live and proven** (`#1314` two-hop split + graceful drain; `#1321` typed retry state machine + `reap_meditations` reaper, QStash schedule `scd_5nDBzAT6P4sw4zVdMrgw4C2EQqkj`, every 10 min).
- Queued backend work: element D (corrupt-archive bundle lifecycle), the client retry-UX wave (`retryable` polling + resume endpoint), Fuel `If-Match` concurrency.
- **Open product call for MJ:** chat photos don't survive reinstall/lost phone (backup-excluded by design, server keeps no bytes, in-app camera shots exist nowhere else) — a backend durable-preview lane if he wants it.

---

## 12. HOW TO PROCEED — first session checklist

1. Read this file, then MJ's memory dir (`~/.claude/projects/-Users-michaeljones-Projects-nbhd-united/memory/`) — start with `MEMORY.md` and `project_personal_oc_watchtower_2026_07_30.md`.
2. Pick up the codex empirical output (§7) or re-run that analysis.
3. Ask MJ the §8 decisions. Do not assume the autonomy ceiling.
4. Verify the gateway is still up (`ssh openclaw 'systemctl --user is-active openclaw-gateway'`) and check whether the cron TypeError recurred on a scheduled fire.
5. Build **phase 1 only** (§6.1) and get it in front of MJ. It is days of work, needs no LLM, and retires the failure class that motivated the whole program. Resist starting the ledger before the heartbeat exists.
6. Work in a worktree per MJ's git discipline (`feat/watchtower` exists on nbhd-united off main; stage specific files, never `git add -A`, never `--no-verify`, PRs only to `main`).
