# Directive — Remove Workspace Chat Routing

**Status:** Decided 2026-05-20. Implementation owner: MJ.
**Scope:** Backend + frontend. Cron isolation preserved.

## TL;DR

Collapse user-facing chat to a single session per user. The "workspaces" primitive conflates two concerns (cron isolation + chat domain partitioning); the first is necessary, the second is invisible to users and causes recurring continuity bugs. Stop using workspaces for chat routing. Keep the schema dormant. Cron isolation stays via `sessionTarget: "isolated"`, on its own sessionKey scheme.

## Evidence

### 1. Today's continuity break (2026-05-20, ~13:45–13:48 UTC)

User asked about loan payoff timeline. Agent generated chart + asked follow-up: *"Want me to also pull up your debt vs savings trend chart?"* User replied "yes". Agent: *"What are we confirming?"*

Log Analytics confirms two different sessionKeys in the same conversation:

```
2026-05-20T13:45:23 | [agent/embedded] workspace bootstrap file USER.md is 14212 chars
  (limit 12000); truncating in injected context
  (sessionKey=agent:main:openai-user:ua7395c8ec8fcaeaadad141b8f0babcee)

2026-05-20T13:47:39 | [agent/embedded] workspace bootstrap file USER.md is 14212 chars
  (limit 12000); truncating in injected context
  (sessionKey=agent:main:openai-user:ua7395c8ec8fcaeaadad141b8f0babcee:ws:finances)
                                                                       ^^^^^^^^^^^^
```

The "yes" reply routed to `ws:finances` instead of staying in the workspace the chart+question was generated in. Different sessionKey = different conversation history = agent has no record of its own prior turn.

### 2. Chart pipeline itself was correct

Same window, separate finding:

```
2026-05-20T13:46:51 | apps.orchestrator.azure_client Uploaded binary
  workspace/charts/payoff_timeline_c6af28cb.png (40596 bytes)
  to file share ws-148ccf1c-ef13-47f8-a
```

PNG rendered, uploaded, delivered to LINE. The visible bug ("agent forgot the conversation") is *not* a chart bug. It's a workspace routing bug surfaced by a chart-flavored conversation.

### 3. Second recurrence of the same bug class in 6 weeks

- **2026-05-14**: canary locked on agent-created `_sync:Heartbeat Check-in` workspace for **9 days** (per `memory/project_workspace_routing_trap_2026_05_14.md`). Fix attempted in PR #575: "reclassify every message; drop 30-min lock-in".
- **2026-05-20** (today): PR #575's reclassification routes short follow-ups across workspaces, losing continuity.

PR #575 traded one failure mode (lock-in) for another (continuity loss). Both are real. There is no auto-classifier policy that avoids both because the user can't see workspace switches and can't correct them.

### 4. UX confirmation

User report: *"the switching of workspaces isn't very obvious to me in daily chat that it is happening."* A feature the user can't see making decisions on their behalf, with observable failure modes, is not delivering value.

## Architecture diagnosis

Workspaces currently solve two unrelated problems via one primitive:

| Concern | Need | Currently solved by | Should solve via |
|---|---|---|---|
| Cron / heartbeat session isolation | Hard isolation so background runs don't pollute chat context | `sessionTarget: "isolated"` + `:ws:<cronname>` suffix on sessionKey | `sessionTarget: "isolated"` alone, with a distinct sessionKey scheme like `cron:<job>:user:<id>` |
| Domain partitioning of chat (work/finance/personal) | (Allegedly) per-domain conversation threads | Auto-classifier routes each user message; `:ws:<domain>` suffix on sessionKey | **Remove.** Single chat sessionKey per user. |

Cron isolation is non-negotiable and works. Domain partitioning is invisible to the user, recurringly broken, and not asked for.

## Scope of removal

### Backend

| File / area | Action |
|---|---|
| `apps/router/services.py` (and any classifier under it) | **Remove** auto-classification step. Every inbound user message routes to one sessionKey per user. |
| `apps/orchestrator/config_generator.py` | Audit any code that builds sessionKey with `:ws:<name>` suffix for *user-facing chat*. Strip the suffix. Crons keep their own scheme. |
| `runtime/openclaw/plugins/nbhd-routing-context/index.js` | The `before_prompt_build` hook injects the workspace catalogue. Reduce to no-op (or remove the plugin entry from config_generator's plugin entries). The `before_agent_finalize` degenerate-output guard is unrelated — keep it. |
| `nbhd-journal-tools` (`runtime/openclaw/plugins/nbhd-journal-tools/index.js`) | Remove `nbhd_workspace_switch` from the agent's tool surface (or strengthen its description to "only call when user explicitly says 'switch to X' workspace by name"). Keep `nbhd_workspace_list` etc. for backwards compat if you want, but they become low-traffic. |
| `apps/journal/models.py` `Workspace` model | **Keep**. Don't drop the table. Existing rows survive as dormant primitives. Migration cost zero, revival path open. |
| `apps/journal/signals.py` | Audit for workspace-creation triggers from agent actions. Remove or gate. |
| Cron-fired session naming (`apps/orchestrator/config_generator.py` cron job builders) | **Verify and preserve** isolation. Replace `:ws:<cronname>` suffix in cron sessionKeys with `cron:<jobname>:user:<id>` (or similar). Functional behavior identical; nomenclature decoupled from workspace concept. |

### Frontend

| Area | Action |
|---|---|
| Workspace switcher in chat header | Remove. There's nothing to switch between. |
| "Current workspace" chip indicator | Remove. |
| Workspaces tab in nav | **Keep** if useful as a content-organization view (group goals by pillar / topic), OR remove. Your call. It doesn't affect chat. |
| Workspace-list / workspace-switch API consumers | Mark deprecated. Don't break existing callers; just stop showing the UI. |

### Tests

Existing workspace tests will assert behavior that no longer applies. Plan:

- `apps/journal/test_workspace_views.py` — update assertions for the new "no auto-routing" reality
- `apps/orchestrator/test_workspace_rules.py` — similar
- Add a test: two consecutive messages on different topics → same sessionKey → agent has cross-topic history available
- Add a test: cron-fired session uses isolated sessionKey scheme, doesn't appear in chat session history

## What to preserve

- **Cron `sessionTarget: "isolated"`** — keep. This is the actual isolation mechanism.
- **Cron-fired session uniqueness per job** — keep. Each cron job (morning-briefing, heartbeat, weekly-review) gets its own session. Just rename the sessionKey scheme so it doesn't share infra with chat workspaces.
- **Per-tenant `Workspace` table** — keep. Dormant.
- **Workspace tools in OpenClaw** — keep registered. Remove from agent's prompted guidance.
- **Frontend Workspace tab as content organizer** — your call. Doesn't affect routing.

## Migration considerations

1. **Existing per-workspace chat history is orphaned.** Sessions in `:ws:finances`, `:ws:work`, etc. become historical-only. They aren't deleted, but new messages from those users go to the flat `agent:main:user:<id>` session. Mitigation: one-time agent-driven summarization of orphaned workspace history into the user's main session memory (post-cutover, run as a mgmt command). Optional — many tenants probably only have one active workspace anyway.

2. **The canary's history lives in multiple workspaces.** Audit `journal_sessions` for canary tenant (`148ccf1c-ef13-47f8-ada1-a98fa90e14a0`) to see what's there before cutover.

3. **Cron job schema changes.** When you rename cron sessionKeys from `:ws:_sync:morning-briefing` to `cron:morning-briefing:user:<id>` (or whatever), existing crons may need to be removed + re-added (per memory `project_openclaw_cron_lifecycle.md`, you can't reset `lastRunAtMs` via `cron.update` — use `cron.remove` + `cron.add`). One-shot reconciler run after deploy.

4. **AGENTS.md updates.** Strip any workspace-related guidance from the system prompt. The cron end-state rules still apply (write to daily note, persist goal/task changes, etc.) — those are session-shape rules, not workspace-specific.

## Acceptance criteria

- [ ] Sending two consecutive messages on visibly different topics produces the same sessionKey in canary logs
- [ ] The agent demonstrates cross-topic memory in one conversation (e.g. ask about fitness, then ask about a goal you set earlier in finance — agent recalls)
- [ ] Cron-fired sessions still produce isolated sessionKeys (logs grep for cron-prefixed keys)
- [ ] Heartbeat / morning briefing still runs without polluting chat context
- [ ] No "workspace switched to X" cues in any chat reply
- [ ] Frontend chat no longer shows workspace UI elements
- [ ] No regression in cron delivery (morning briefing arrives, heartbeats fire)
- [ ] Stretch: canary uses one continuous session for a full week without routing-related continuity bugs

## Risks / unknowns

1. **Hidden cron dependencies on `:ws:` prefix.** Some cron reconciler / dedup logic may key off the `:ws:` substring. Grep for `:ws:` literal usage; refactor if found.
2. **The `_sync:` prefix workspaces.** Reserved-prefix guard from PR #575 prevents agent-created `_sync:` workspaces but the prefix itself may be load-bearing somewhere. Audit before removing.
3. **OpenClaw plugin assumptions.** `active-memory` (currently disabled), `memory-core`, `dreaming` may have implicit per-workspace state. Verify they work with a single chat session.
4. **PendingExtraction approval flows.** Those land in Document rows — workspace-agnostic, should be fine, but verify.
5. **User-facing "workspace" mentions in any prompt template, doc, or agent voice.** Find/replace pass on `templates/openclaw/` and `apps/orchestrator/personas.py`.

## Estimated work

1–2 days of focused work. The largest chunk is the cron sessionKey rename (touches reconciler) and the test suite update. The chat-side change is small — strip the classifier, flatten the sessionKey builder.

## Related history

- `memory/project_workspace_routing.md` — original Context Workspaces vision
- `memory/project_workspace_routing_trap_2026_05_14.md` — first trap incident
- `CONTINUITY_workspace-routing-fix.md` — fix plan that led to PR #575
- PR #575 (merged 2026-05-19) — reclassify-every-message attempt

## Reference log lines (raw)

For posterity, the exact lines that motivated this decision:

```
2026-05-20T13:45:23 [agent/embedded] workspace bootstrap file USER.md is 14212 chars (limit 12000); truncating in injected context (sessionKey=agent:main:openai-user:ua7395c8ec8fcaeaadad141b8f0babcee)

2026-05-20T13:47:39 [agent/embedded] workspace bootstrap file USER.md is 14212 chars (limit 12000); truncating in injected context (sessionKey=agent:main:openai-user:ua7395c8ec8fcaeaadad141b8f0babcee:ws:finances)
```

KQL to reproduce against the Log Analytics workspace (`035a49db-1da5-452d-8b32-b074d7a5d606`):

```kql
ContainerAppConsoleLogs_CL
| where ContainerAppName_s == 'oc-148ccf1c-ef13-47f8-a'
| where TimeGenerated between (datetime(2026-05-20T13:45:00Z) .. datetime(2026-05-20T13:50:00Z))
| where Log_s contains 'sessionKey='
| project TimeGenerated, Log_s
| order by TimeGenerated asc
```
