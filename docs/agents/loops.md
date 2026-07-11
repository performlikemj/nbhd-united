# Running loops safely here

A **loop** is an agent repeating cycles of work until a stop condition is met. Four kinds, by what human judgment you hand off:

| Loop | You hand off | Primitive | Use when |
|---|---|---|---|
| Turn-based | the check | a prompt | exploring/deciding; short one-offs |
| Goal-based | the stop condition | `/goal` | you can state what "done" verifiably looks like |
| Time-based | the trigger | `/loop`, `/schedule` | recurring work, or watching an external system (a PR, a queue) |
| Proactive | the prompt | `/schedule` + `/goal` + workflows + auto mode | recurring, well-defined streams (triage, migrations, upgrades) |

The harness is the **precondition** for the last three: you cannot safely hand off the stop condition or the prompt until the system around the loop can catch a confident mistake. `invariants.md` is the list of hard stops every loop must honor; this doc is how loops don't hurt you.

## 1. Stop conditions are safety gates, not just quality gates

In an autonomous or auto-mode loop, a wrong action *executes* — there's no human between the decision and the effect. A quality-focused review (`/code-review` for style/bugs) sails right past a *scope* error. Real near-miss: a "docs harness" branch was silently stacked on unpushed feature commits carrying 6 DB migrations — merging it would have run schema changes against a paused prod DB. What caught it was a skeptical "is this the scope I was authorized for?" check, not a code review.

**Comply:** a loop must treat these as non-negotiable halts (raise to a human, don't route around): schema migrations against prod; sending anything to an external service before a PII/secrets audit; deleting Azure resources; anything in `invariants.md`. Encode the halt as the loop's stop condition, not as a hope.

## 2. Key the stop condition off the real end-state, not a proxy

Every "done" signal can lie. A MERGED badge doesn't mean `main` advanced (stacked-PR phantom merge). A green check / exit code can be stolen by a pipe (`gh ... --watch | tail`). "Tests pass" ≠ "the user-facing flow works." **Verify the artifact**: `git ls-tree origin/main <file>` that the change is really on main; hit the endpoint; load the DB row. The evaluator behind `/goal` is only as good as the observability you give it — point it at the end-state, not a signal that stands in for it. (See `workflow.md` §merge verification, `debugging.md` §post-deploy.)

## 3. Verify by DRIVING the change, never by assuming — non-negotiable for anything user-facing

A successful edit is not verification. Drive the real thing and look:

- **Web (frontend/):** Playwright — load the edited page, interact with the control, screenshot before/after, confirm zero new console errors. (No FE test runner exists here; driving the page IS the test.)
- **iOS:** build to the Simulator and drive it (`xcrun simctl` boot/install/launch, `io <udid> screenshot`); never report an iOS change done from a green build alone — a build that compiles can still render wrong. (nbhd-ios has its own `docs/agents/loops.md`.)
- **Backend:** hit the actual endpoint or run the flow end-to-end; a passing unit test doesn't prove the user-facing behavior. After a deploy, verify the symptom via logs/probes (`debugging.md`), never from CI green.

Encode these as a **verify skill** so the loop self-checks each iteration. The more quantitative the check (screenshot shows the expected state, response body has the field, query count didn't regress), the more the loop can trust its own "done."

## 4. Make the loop observe itself

When a proactive loop fans out sub-agents, each must report its result through a **structured channel** (SendMessage / structured output), and the orchestrator must verify each agent's *actual output* — the file changed, the screenshot taken, the row written — not merely that the agent "finished." Real failure: sub-agents wrote final reports as plain text that never reached the orchestrator; the loop looked complete while the hand-back was silently broken. A silent no-op is indistinguishable from success unless you check the effect.

## 5. Pilot the unit before you fan out

Loops amplify a flaw N times. Prove one instance end-to-end, then scale — we built the nbhd-united harness exemplar first, then let `/harness-init` replicate it to three repos. Token gauging is the lesser reason; **correctness amplification** is the real one. Dynamic workflows can spawn hundreds of agents — gauge on a slice first.

## 6. When a result misses the bar, fix the SYSTEM, not the instance

Don't just fix the one bad output — encode the miss as a new invariant, hook, or verify-skill step so the next iteration *can't* repeat it. That flywheel is the whole point of a harness: every caught mistake makes the loop safer for all future runs.

## Managing token spend

Match the primitive to the job (a small edit doesn't need a workflow) and the **model to the work**: fast/cheap models for mechanical changes, the capable model for judgment calls. Prefer a deterministic script over re-reasoning the same steps. Match a schedule's interval to how often the watched thing actually changes. Inspect with `/usage` (by skill/subagent/MCP), `/goal` with no args (turns + tokens so far), and `/workflows` (per-agent spend; stop an agent anytime).
