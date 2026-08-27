# AUDIT — templates/openclaw/AGENTS.md (2026-08-27, codex read-only session 01a04322…, requested by MJ)

Rendered at origin/main 001462bf. Sections 1–6 below; first copy only (the log repeated the report).

## 1. SIZE MAP

Counts are Python characters, the unit enforced by `len()` and `BOOTSTRAP_MAX_CHARS`—not UTF-8 bytes. Rendering strips the template and replaces `{{PERSONA_PERSONALITY}}` ([personas.py:389](apps/orchestrator/personas.py:389), [personas.py:399](apps/orchestrator/personas.py:399)).

Neighbor lean render:

- Raw stripped template: 15,832
- Placeholder: 23
- Neighbor personality: 412
- Exact rendered lean size: **16,221**
- Lean ceiling: 16,300 ([test_document_ingestion_directive.py:184](apps/orchestrator/test_document_ingestion_directive.py:184))
- Current lean headroom: **79**

| Template section | Lines | Chars | % lean | % all-gates |
|---|---:|---:|---:|---:|
| Title, opening, persona | [1–7](templates/openclaw/AGENTS.md:1) | 632 | 3.90% | 2.64% |
| Who You Are | [8–11](templates/openclaw/AGENTS.md:8) | 1,078 | 6.65% | 4.50% |
| Growing Into Who You Are | [12–17](templates/openclaw/AGENTS.md:12) | 1,049 | 6.47% | 4.38% |
| Session Start | [18–52](templates/openclaw/AGENTS.md:18) | 5,255 | 32.40% | 21.95% |
| North Star | [53–75](templates/openclaw/AGENTS.md:53) | 1,212 | 7.47% | 5.06% |
| How to Be | [76–83](templates/openclaw/AGENTS.md:76) | 301 | 1.86% | 1.26% |
| What You Can Do | [84–105](templates/openclaw/AGENTS.md:84) | 3,022 | 18.63% | 12.62% |
| What You Can't Do | [106–112](templates/openclaw/AGENTS.md:106) | 216 | 1.33% | 0.90% |
| Rules | [113–131](templates/openclaw/AGENTS.md:113) | 1,080 | 6.66% | 4.51% |
| Reply Markers — Mandatory | [132–153](templates/openclaw/AGENTS.md:132) | 2,075 | 12.79% | 8.67% |
| Reference Docs | [154–159](templates/openclaw/AGENTS.md:154) | 301 | 1.86% | 1.26% |
| **Lean total** |  | **16,221** | **100%** | **67.76%** |

Conditional additions include their leading `\n\n`:

| All-gates addition | Source | Added chars | % all-gates |
|---|---|---:|---:|
| Portfolio publish | [personas.py:718](apps/orchestrator/personas.py:718) | 701 | 2.93% |
| Current location | [personas.py:738](apps/orchestrator/personas.py:738) | 389 | 1.62% |
| Neighborhood, propose-enabled branch | [personas.py:755](apps/orchestrator/personas.py:755) | 1,653 | 6.90% |
| Document keep/removal | [personas.py:805](apps/orchestrator/personas.py:805) | 996 | 4.16% |
| Email/calendar/Reddit provenance | [personas.py:833](apps/orchestrator/personas.py:833) | 640 | 2.67% |
| Sautai | [personas.py:854](apps/orchestrator/personas.py:854) | 281 | 1.17% |
| Tour guide, places-ready + situation | [personas.py:868](apps/orchestrator/personas.py:868) | 327 | 1.37% |
| Journal shaping | [personas.py:912](apps/orchestrator/personas.py:912) | 562 | 2.35% |
| Gravity observation mode | [personas.py:926](apps/orchestrator/personas.py:926) | 2,170 | 9.06% |
| **Conditional total** |  | **7,719** | **32.24%** |

Exact neighbor all-gates render: **23,940**, leaving **10** characters under the 23,950 CI ceiling and **60** under the configured 24,000 cap ([test_reminder_capability.py:44](apps/orchestrator/test_reminder_capability.py:44), [config_generator.py:39](apps/orchestrator/config_generator.py:39)).

Other composition costs:

- `agents_md` extra of length `n`: **n + 2** after `.strip()`.
- `quick_replies_md` extra: another **n + 2** independently ([personas.py:940](apps/orchestrator/personas.py:940)).
- All-gates + 1,500-character `agents_md`: **25,442**, not the stale 25,451 stated in [test_reminder_capability.py:209](apps/orchestrator/test_reminder_capability.py:209).
- MJ-shaped gates—finance, friends-propose, document keep—plus 1,500 extras: **22,542**. The 22,324 comment at [test_reminder_capability.py:48](apps/orchestrator/test_reminder_capability.py:48) is stale.
- Subagent index row: **+76** ([personas.py:540](apps/orchestrator/personas.py:540)).
- Lean persona variants: neighbor 16,221; coach 16,118; sage 16,101; spark 16,101.
- Corresponding all-gates sizes: 23,940; 23,837; 23,820; 23,820.

## 2. RULE INVENTORY

Classification: **A** load-bearing operational rule; **A\*** operational but no direct rendered-prompt test; **B** supporting explanation/style; **C** example; **D** duplicated in an on-demand file; **E** stale or contradicted.

| Paragraph/bullet(s) | Class | Evidence |
|---|---|---|
| Regular-person opening, internals invisible [3–4](templates/openclaw/AGENTS.md:3) | A | Exact text pinned at [test_fuel_guidance_consistency.py:65](apps/orchestrator/test_fuel_guidance_consistency.py:65). |
| Persona insertion [6](templates/openclaw/AGENTS.md:6) | B | Renderer substitution only. |
| Warmth/identity authority and “operational gates still apply” [10](templates/openclaw/AGENTS.md:10) | A* | Deliberately grown by `5d75cd5d`/`2edcddda`; no exact prompt test. |
| Managed/open SOUL regions [14](templates/openclaw/AGENTS.md:14) and sparse-writing/user-data boundary [16](templates/openclaw/AGENTS.md:16) | A* | Merge behavior is tested at [test_workspace_rules.py:240](apps/orchestrator/test_workspace_rules.py:240); prompt wording is not. |
| Bootstrap files already loaded [20](templates/openclaw/AGENTS.md:20) | A, D | Duplicated in [rules/memory.md:125](templates/openclaw/rules/memory.md:125). |
| Two session kinds [22](templates/openclaw/AGENTS.md:22) | B | Routing explanation. |
| Cron marker/preamble [24](templates/openclaw/AGENTS.md:24) | A | Exact marker comes from [config_generator.py:145](apps/orchestrator/config_generator.py:145). |
| USER preloaded-state contract [26](templates/openclaw/AGENTS.md:26) | A* | Dense operational paragraph; no direct phrase pin. |
| Cron end-state introduction [28](templates/openclaw/AGENTS.md:28) | B |
| Persist narrative [30](templates/openclaw/AGENTS.md:30), persist goal/task changes [31](templates/openclaw/AGENTS.md:31), silence when nothing happened [32](templates/openclaw/AGENTS.md:32) | A* | These rules exist only here and have no exact prompt test. |
| Conversational “reply directly/no eager reads” [34](templates/openclaw/AGENTS.md:34) | A* | Consistent with the context-trim direction; unpinned. |
| Reconcile-gate introduction [36](templates/openclaw/AGENTS.md:36) | B |
| Material-event classifier [38](templates/openclaw/AGENTS.md:38) | A, D | Pinned at [test_reminder_capability.py:64](apps/orchestrator/test_reminder_capability.py:64); tool reference repeats it at [tools-reference.md:72](templates/openclaw/docs/tools-reference.md:72). |
| Workout plans are writes [40](templates/openclaw/AGENTS.md:40) | A | Exact rendered-order pin at [test_fuel_guidance_consistency.py:57](apps/orchestrator/test_fuel_guidance_consistency.py:57). |
| Fuel search-before-write/rotation [42](templates/openclaw/AGENTS.md:42) | A, D | Exact three-surface pin at [test_fuel_guidance_consistency.py:18](apps/orchestrator/test_fuel_guidance_consistency.py:18); duplicated in [rules/fuel.md:202](templates/openclaw/rules/fuel.md:202) and [tools-reference.md:221](templates/openclaw/docs/tools-reference.md:221). |
| Yes → scan/write/report [44](templates/openclaw/AGENTS.md:44); no → no scan [45](templates/openclaw/AGENTS.md:45) | A, D | Incident behavior pinned at [test_reminder_capability.py:64](apps/orchestrator/test_reminder_capability.py:64). |
| Legacy-turn fallback [47](templates/openclaw/AGENTS.md:47) | A* | Only here, untested. |
| Journal search only when needed [49](templates/openclaw/AGENTS.md:49) | A, D | Memory search order at [rules/memory.md:95](templates/openclaw/rules/memory.md:95). |
| Journal-link marker [51](templates/openclaw/AGENTS.md:51) | A*, D, E | Full reference at [tools-reference.md:5](templates/openclaw/docs/tools-reference.md:5), but that reference says “last line” while AGENTS permits placement before quick replies. Router tests support the quick-reply exception; prompt wording is unpinned. |
| North Star definition/use [55–58](templates/openclaw/AGENTS.md:55) | B, D | Duplicated at [tools-reference.md:107](templates/openclaw/docs/tools-reference.md:107). |
| Rare/high-trust introduction [60](templates/openclaw/AGENTS.md:60) | B |
| Weekly/multi-pillar threshold [62–64](templates/openclaw/AGENTS.md:62), question-only proposal [65–67](templates/openclaw/AGENTS.md:65), explicit-confirmation gate [68–71](templates/openclaw/AGENTS.md:68), retire/evolving [72–74](templates/openclaw/AGENTS.md:72) | A*, D | Duplicated in tool reference lines 116–120; no rendered-AGENTS prompt test. |
| Five “How to Be” bullets [78–82](templates/openclaw/AGENTS.md:78) | B | Style guidance, substantially overlapping persona/SOUL. |
| General capability bullets [86–92](templates/openclaw/AGENTS.md:86), image/PDF/TTS [94–97](templates/openclaw/AGENTS.md:94) | A* | They inherit the search/try umbrella. Most are not individually prompt-tested. |
| Reminder capability and success-only claim [93](templates/openclaw/AGENTS.md:93) | A | Added after eval runs 72/79 denied the capability and run 33 fabricated success; pinned at [test_reminder_capability.py:78](apps/orchestrator/test_reminder_capability.py:78). |
| Tool-search umbrella [98](templates/openclaw/AGENTS.md:98) | A, E | Load-bearing for toolSearch, but toolSearch is version-gated at [config_generator.py:2206](apps/orchestrator/config_generator.py:2206); pre-2026.5.28 tenants do not receive it. |
| Attachment path/read/injection defense [100](templates/openclaw/AGENTS.md:100) | A, E | Security floor pinned at [test_document_ingestion_directive.py:207](apps/orchestrator/test_document_ingestion_directive.py:207). “Reads text-based PDFs” is stale: current platform configuration supports scanned/image PDFs ([config_generator.py:2632](apps/orchestrator/config_generator.py:2632)). |
| Ephemeral document explanation [102](templates/openclaw/AGENTS.md:102) | A, D | Duplicated at [rules/document-ingestion.md:3](templates/openclaw/rules/document-ingestion.md:3). |
| Answer/propose/save flow [104](templates/openclaw/AGENTS.md:104) | A, D | Exact floors pinned at [test_document_ingestion_directive.py:193](apps/orchestrator/test_document_ingestion_directive.py:193); duplicated in [rules/document-ingestion.md:7](templates/openclaw/rules/document-ingestion.md:7). |
| No coding/admin [108](templates/openclaw/AGENTS.md:108) | A | Policy disables elevated execution at [tool_policy.py:187](apps/orchestrator/tool_policy.py:187). |
| No email/social posting [109](templates/openclaw/AGENTS.md:109) | E | Email sending is absent, but Reddit post/reply are current manifest tools requiring approval ([nbhd-reddit manifest:1](runtime/openclaw/plugins/nbhd-reddit-tools/openclaw.plugin.json:1)). |
| No others’ data [110](templates/openclaw/AGENTS.md:110) | A* | Safety rule, unpinned here. |
| Don’t pretend [111](templates/openclaw/AGENTS.md:111) | B | General explanation; specific anti-fabrication rules are stronger. |
| “Rules loaded on demand” [115](templates/openclaw/AGENTS.md:115), ten rule-index rows [119–128](templates/openclaw/AGENTS.md:119), read-relevant instruction [130](templates/openclaw/AGENTS.md:130) | D, E | Upload/index existence is tested at [test_workspace_rules.py:21](apps/orchestrator/test_workspace_rules.py:21), but chat agents cannot filesystem-read them ([docs/agents/invariants.md:71](docs/agents/invariants.md:71)). |
| Reply-marker inline mandate [134](templates/openclaw/AGENTS.md:134) | A, D | Full duplicate at [rules/reply-markers.md:1](templates/openclaw/rules/reply-markers.md:1); history shows moving it out failed. |
| Chart trigger/prohibition [138](templates/openclaw/AGENTS.md:138) | A, D | Duplicate at [rules/reply-markers.md:14](templates/openclaw/rules/reply-markers.md:14). |
| Chart types [140](templates/openclaw/AGENTS.md:140) | B, D | Reference data duplicated at rules lines 20–24. |
| Chart example [142](templates/openclaw/AGENTS.md:142) | C, D | Same example at rules line 28. |
| Insight trigger/side effect [146](templates/openclaw/AGENTS.md:146) | A, D | Duplicate at [rules/reply-markers.md:41](templates/openclaw/rules/reply-markers.md:41). |
| Pillar routing/gravity gate [148](templates/openclaw/AGENTS.md:148) | A, D | Duplicate at rules lines 49–61. |
| Insight example [150](templates/openclaw/AGENTS.md:150) | C, D | Same example at rules line 71. |
| Marker channel behavior [152](templates/openclaw/AGENTS.md:152) | A, D | Duplicate at rules line 10. |
| Reference-doc introduction [156](templates/openclaw/AGENTS.md:156), tools/cron/error bullets [157–159](templates/openclaw/AGENTS.md:157) | D, E | Valid for cron contexts; impossible as a chat gate because filesystem `read` is unavailable. |

Conditional rule inventory:

| Gate | Classification | Pin/incident |
|---|---|---|
| Portfolio call-first, one-call-per-image, success-only reporting [personas.py:719–729](apps/orchestrator/personas.py:719) | A, D | Comment records that catalog availability alone did not cause a call ([personas.py:709](apps/orchestrator/personas.py:709)); duplicated at [tools-reference.md:261](templates/openclaw/docs/tools-reference.md:261). |
| Current-location capture and provenance [personas.py:739–745](apps/orchestrator/personas.py:739) | A, D | Exact/length behavior pinned at [test_situation_capture_directive.py:32](apps/orchestrator/test_situation_capture_directive.py:32); shorter duplicate at tools-reference line 144. |
| Neighborhood invisibility, proposal approval, absorb, sensitive-data exclusions, mission boundary, Circle isolation [personas.py:755–795](apps/orchestrator/personas.py:755) | A | Proposal/order pin [test_pr4.py:236](apps/friends/test_pr4.py:236); Circle isolation [test_pr7.py:282](apps/friends/test_pr7.py:282); branch split [test_pr9.py:245](apps/friends/test_pr9.py:245). |
| Document keep and whole-source forget [personas.py:806–819](apps/orchestrator/personas.py:806) | A | Tool gating/order pinned at [test_document_ingestion_directive.py:67](apps/orchestrator/test_document_ingestion_directive.py:67). |
| Email/calendar/Reddit propose-first and source ledger [personas.py:834–842](apps/orchestrator/personas.py:834) | A | Gate and source fields pinned at [test_email_provenance_directive.py:64](apps/orchestrator/test_email_provenance_directive.py:64). |
| Sautai search/call and no fabrication [personas.py:855–860](apps/orchestrator/personas.py:855) | A | Explicitly lean by design; pinned at [test_sautai_directive.py:61](apps/orchestrator/test_sautai_directive.py:61). |
| Tour-guide call-first/search-before-compose/location [personas.py:868–909](apps/orchestrator/personas.py:868) | A, E | Tool-response variants are pinned at [test_tour_guide_directive.py:89](apps/orchestrator/test_tour_guide_directive.py:89). The legacy “read docs” branch cannot work in chat. |
| Journal template read/update, approval, future-only, schedule pairing [personas.py:912–923](apps/orchestrator/personas.py:912) | A, E | Verbatim test at [test_journal_shaping_directive.py:72](apps/orchestrator/test_journal_shaping_directive.py:72), but its `read docs/journal-shaping.md` step is impossible in chat. Tool descriptions already carry most of the contract ([index.js:140](runtime/openclaw/plugins/nbhd-journal-shaping/index.js:140)). |
| Gravity pull-first, per-topic signals, anomaly selection, record/confirm/refute/noise rules [envelope.py:42–62](apps/insights/envelope.py:42) | A | Signals-response delivery was chosen after rules-file pointers proved unreliable ([envelope.py:11](apps/insights/envelope.py:11)); pinned at [test_personas_workspace_files.py:26](apps/orchestrator/test_personas_workspace_files.py:26). |

Tool-name check:

- Every explicit `nbhd_*` name in the base template and conditional gates exists in a current plugin manifest’s `contracts.tools`; wildcard families map to registered tool families.
- `pdf`, `image`, `tts`, and `tool_search` are built-ins, not plugin contracts. `pdf` is explicitly allowed for 2026.5.28+ ([tool_policy.py:114](apps/orchestrator/tool_policy.py:114)).
- The model sees a compact structured Tool Search interface rather than approximately 79 schemas/20–30K tokens each turn; deny policy applies before discovery ([config_generator.py:2206–2223](apps/orchestrator/config_generator.py:2206)).
- The on-demand tools reference itself is stale: `nbhd_reddit_search`, `nbhd_reddit_new`, and `nbhd_reddit_comments` at [tools-reference.md:166](templates/openclaw/docs/tools-reference.md:166) are absent from the current Reddit manifest.

## 3. HISTORY

Requested 40-commit window:

```text
001462bf 2026-08-27 fix(fuel): variety guard applies to four-week plans (rotate every 1–2 weeks) (#1534)
26aa21f2 2026-08-27 feat(fuel): deterministic catalog chain — catalog_ref tags at ingress, per-track variety guard, plugin rotation compiler, search gate (Phase 2c) (#1531)
2d16a3c3 2026-08-04 feat: tie down checked claims and material events
b1ddbf9f 2026-08-03 feat: teach chat the journal-link marker
61c925a3 2026-07-22 fix(fuel): honor "start today" — first_workout_date in create response, cadence-includes-today guidance, funded AGENTS.md write-rule
a7434a79 2026-07-14 fix(cron): tell the assistant it can set reminders, and stop spent one-shots squatting their name
87a129d4 2026-07-14 fix(assistant): review fixes — echo guard, marker-shape classifier, audit test
8698ad1f 2026-07-14 fix(assistant): correct channel identity — app is a first-class surface
cc1602aa 2026-07-12 refactor(orchestrator): de-dupe + compress attachment/document prose in base AGENTS.md
5521c578 2026-07-11 Merge pull request #1091 from performlikemj/feat/doc-keep-phase2-ledger
026ca573 2026-07-10 docs(agents): clarify attachment-marker path parsing after untrusted-data notice
6bec51b8 2026-07-10 feat(router): frame uploaded files as untrusted data across all channels (P0-1)
f092b78c 2026-07-09 feat: nbhd-document-keep OC plugin + AGENTS.md gate/rules for document-keeping
af05262e 2026-07-09 fix: address PDF-ingress review — honest scanned-PDF limit, throttle, write-gate test
40567b87 2026-07-09 feat: accept PDF uploads on chat ingress → tenant share + pdf tool
5119a095 2026-07-07 fix: tell the assistant its tools live behind toolSearch
80f139e9 2026-07-03 merge: apply North Star integration snippets to AGENTS.md + tools-reference
536947ed 2026-07-03 merge: apply insight-marker pillar blurb to AGENTS.md (integration snippet)
5d75cd5d 2026-07-03 feat(persona): warmth floor + growth directive in AGENTS.md
2edcddda 2026-07-01 fix(persona): name the operational gates the voice directive must not bypass
1e3220f3 2026-06-30 feat(persona): make AGENTS.md defer to the tenant's IDENTITY.md/SOUL.md
7eb0e158 2026-06-04 fix(reconcile): pin kind=project on project-status append + probe catches misfile
d08679d3 2026-06-03 feat(reconcile): surface project docs in nbhd_reconcile_scan
44a39fbf 2026-05-21 feat(reconcile): nbhd_reconcile_scan + AGENTS.md conversational gate
be823b39 2026-05-21 fix(agents-md): restore [[chart:]] + [[insight:]] markers to system-prompt level
f5bb0d3f 2026-05-21 Merge pull request #646 from performlikemj/feat/remove-workspace-chat-routing
5142e237 2026-05-20 refactor: remove workspace-based chat routing
b24f3444 2026-05-20 feat(insights): record insights via [[insight:...]] reply markers
333c4b9f 2026-05-20 feat(agents-md): promote [[chart:]] marker rule to system-prompt level
b125395f 2026-05-07 feat(orchestrator): extend USER.md envelope with Fuel/Finance/Journal pillars
3745bb1e 2026-05-07 feat(orchestrator): move envelope to workspace/USER.md with sentinel-merge
c56bc4fd 2026-05-03 feat(crons): pre-load goals/tasks/lessons into cron messages via context envelope
f4509a21 2026-05-03 feat(byo): pre-warm claude session + trim conversational system context
c8ba33aa 2026-04-24 fix: eliminate redundant LLM calls and model spray on every message
4b8c8e9c 2026-04-22 feat(fuel): onboarding profile + natural language logging + OpenClaw plugin
7f9898f4 2026-04-13 fix: route journal content to correct sections instead of append
7a1eb186 2026-04-13 feat: voice-to-journal pipeline + project check-in cron (#293)
b3a2bd0f 2026-04-08 feat: lean AGENTS.md migration — expand rules + add week-ahead
ac09be9b 2026-04-08 feat: workspace agent rules + wire up rules upload (Phase 4)
58139391 2026-03-30 refactor: split AGENTS.md into scoped rules (217 → 71 lines) (#172)
```

Largest net character growth, calculated from each commit’s parent/current blob:

| Commit | Net chars | `git show --stat` |
|---|---:|---|
| `02c0817c` PKM workflow | +3,984 | 86 insertions |
| `9896b791` memory integration | +2,864 | 99 insertions, 24 deletions |
| `3fefbda3` profile/timezone | +1,866 | 43 insertions |
| `d3511a0d` issue handling | +1,865 | 25 insertions |
| `ccee4483` week-ahead/BYOK | +1,759 | 34 insertions |
| `9461b123` lesson approval | +1,724 | 35 insertions |
| `333c4b9f` chart rule | +1,611 | 29 insertions |
| `be823b39` marker restoration | +1,536 | 20 insertions |
| `5d75cd5d` warmth/growth | +1,501 | 8 insertions, 2 deletions |
| `f092b78c`/merge `5521c578` document gate | +1,389 | 9 insertions |

Largest trims were `f6f484a2` −11,936, `58139391` −8,353, `73072f93` −5,995, and `cc1602aa` −677.

A rule was demonstrably removed and then re-added:

1. `333c4b9f` added the chart rule to always-loaded AGENTS.
2. `b24f3444` removed 1,506 characters and moved marker detail on demand.
3. `be823b39` immediately restored charts and insights to system-prompt level because the markers were mandatory.

That is the direct precedent against moving the mandatory marker cue out again. The newer Fuel gate repeats the same conclusion: the exact discovery instruction is intentionally present in AGENTS, `rules/fuel.md`, and tools-reference, with the always-loaded copy separately pinned ([test_fuel_guidance_consistency.py:52](apps/orchestrator/test_fuel_guidance_consistency.py:52)).

## 4. TRIM PLAN

### Tier I — wording/examples only; no rule removed

Character deltas below use the CI’s character-count unit.

1. Reply-marker introduction, **−81**.

Before:

> Two pieces of markup the platform processes on the way out — these must be used inline as part of writing your reply, not deferred to a tool call. Full reference: `rules/reply-markers.md`.

After:

> Use these markers inline in replies; the platform processes them. Full reference: `rules/reply-markers.md`.

2. Delete the duplicate chart example at [AGENTS.md:142](templates/openclaw/AGENTS.md:142), including its following blank line: **−97**. The identical example remains at [rules/reply-markers.md:28](templates/openclaw/rules/reply-markers.md:28).

3. Delete the duplicate insight example at [AGENTS.md:150](templates/openclaw/AGENTS.md:150), including its following blank line: **−198**. Equivalent examples remain at rules lines 69–79.

4. Correct and shorten the stale PDF failure sentence, **−62**.

Before:

> The tool reads text-based PDFs; if it errors, tell the user plainly and ask for a text-based PDF or a photo instead — do NOT pretend you read it.

After:

> If it errors, say so and ask for another PDF or photo — do NOT pretend you read it.

5. Compress the two document-retention paragraphs, retaining every tested floor, **−316**.

Before:

> **After reading an attached document, decide what's worth keeping — with the user, not for them.** The uploaded file is temporary — it clears out about a day after it arrives, and only what you deliberately save is kept.
>
> **Answer first**, then keep. **Never save on the same turn the document arrives.** Propose first — show the *actual text or values* you'd keep and name *where* each piece goes (a journal note, a task, a goal, a fuel or finance entry) — then wait. Save ONLY after they reply and agree, exactly what they approved. Never say something is saved unless the write tool returned success THIS turn, and don't promise to "remember the whole document" — you keep only what you saved to a real destination.

After:

> **After reading an attached document:** it clears out in about a day; only deliberately saved information persists. **Answer first. Never save on the same turn the document arrives.** Propose the exact text or values and each destination, then wait. After agreement, save exactly the approved items. Claim success only after the write tool succeeds THIS turn; never promise to remember unsaved content.

6. Compress only the Rules table’s descriptive cells, **−483**:

| Current exact cell | Replacement |
|---|---|
| `Scope` | `Load for` |
| `PKM bootstrapping, live capture, lesson triggers, proactive maintenance` | `Journal capture` |
| `Lesson creation, approval flow, constellation tools` | `Lessons` |
| `Two-layer memory system, search order, when to write` | `Memory` |
| `Timezone + location setup for new users` | `Onboarding` |
| `Cron delivery, check-in windows, automated routines` | `Messaging/crons` |
| `Weekly cron review pass, mid-week plan changes` | `Weekly review` |
| `Voice recording processing, project cross-referencing, follow-up questions` | `Voice journal` |
| `Fuel workout tracking, fitness onboarding, natural language logging` | `Fuel` |
| `Platform-processed markup in replies — [[chart:...]], [[insight:...]]` | `Reply markers` |
| `Saving information from an uploaded document — propose-then-save, verbatim-keep` | `Uploaded documents` |

Total Tier I saving: **1,237 characters**. Result:

- Lean: **14,984**
- All-gates: **22,703**
- Headroom to CI ceiling: **1,247**
- All-gates + 1,500 extras: **24,205**, still 205 over the hard cap.

### Tier II — move behind a pointer; high delivery risk

Technically movable candidates:

- North Star details → `docs/tools-reference.md` § North Star. Replace the 1,212-character section with:

  > Treat a North Star as rare and consent-first: propose only as a question after a pattern spans multiple pillars; call `nbhd_purpose_confirm(user_confirmed=true)` only after explicit agreement. Details: `docs/tools-reference.md` → North Star.

  Saving: **954**.

- Reply-marker catalog/detail → `rules/reply-markers.md`. Leave only mandatory trigger, syntax, pillar guard, and channel behavior. A 451-character minimal section would save **1,624**.
- Portfolio detail → `docs/tools-reference.md` § Site Publishing. A one-line pointer would save approximately **574**.

Discovery is nominally through the Rules/Reference Docs pointers at [AGENTS.md:113–130](templates/openclaw/AGENTS.md:113). In chat, however, those files are not actually readable: filesystem `read` is stripped ([docs/agents/invariants.md:71](docs/agents/invariants.md:71)). The Gravity team explicitly rejected a rules-file move because files were loaded at most once or not reloaded, and moved detail onto a mandatory tool response instead ([envelope.py:11–24](apps/insights/envelope.py:11)). Fuel and portfolio comments show that passive catalog/on-demand detail does not reliably cause the first tool call. Therefore Tier II should not ship until content is delivered by a deterministic tool response or chat gains a real document-read surface.

### Tier III — raise the cap/ceiling

Raising only `_ALL_GATES_CEILING` deletes the alarm and gains no runtime capacity. A defensible alternative is to raise `BOOTSTRAP_MAX_CHARS` and the CI ceiling together—for example 26,000/25,950—while retaining a fixed margin. Then the current all-gates + 1,500-extra shape, 25,442, fits with 558 characters of hard-cap margin.

The current cap protects deterministic delivery: OpenClaw silently truncates the tail, which caused the July 11 incident ([config_generator.py:2649–2666](apps/orchestrator/config_generator.py:2649), [personas.py:979–998](apps/orchestrator/personas.py:979)). The lower CI ceiling protects “fund every addition with a trim” discipline. Neither comment presents 24,000 as an upstream absolute; the total remains separately capped at 80,000.

Per-turn cost is not exactly derivable because character-to-token ratio and cache-hit rate vary. At a rough four characters/token, 1,000 additional characters are about 250 input tokens:

- DeepSeek V4 Pro, $0.435/M input tokens: about **$0.00010875 per uncached turn**.
- DeepSeek V4 Flash, $0.09/M: about **$0.0000225**.
- Gemma, $0.10/M: about **$0.000025**.
- Free-offer model: $0.
- BYO models: paid outside NBHD.

Rates are at [billing/constants.py:30–58](apps/billing/constants.py:30). Prompt caching reuses a byte-stable prefix, but USER.md churn can bust it ([config_generator.py:3108–3118](apps/orchestrator/config_generator.py:3108)). No repository evidence quantifies latency per 1,000 characters.

## 5. RISKS

- **The on-demand architecture is contradicted by current tool policy.** AGENTS says rules/docs are loadable, while the authoritative invariant says chat agents cannot read them. “Move it to rules” can silently remove the rule.
- **Version skew:** AGENTS unconditionally instructs `tool_search`, but `tools.toolSearch` exists only for OpenClaw ≥2026.5.28 ([config_generator.py:2214](apps/orchestrator/config_generator.py:2214)). Older tenants see a prompt naming a discovery mechanism their config does not enable.
- **Stale PDF guidance:** “text-based PDFs” contradicts the current vision/PDF configuration.
- **Over-broad capability denial:** “Can’t … post to social media directly” contradicts current, approval-gated Reddit post/reply tools.
- **Journal-link conflict:** AGENTS allows the marker immediately before final quick replies; tools-reference says it must always be the final line.
- **Stale measurements:** 16,003/16,002 lean, 22,324 MJ-shape, and 25,451 all-gates-plus-extras comments no longer match origin/main. The actual values are 16,221, 22,542, and 25,442.
- **Stale test expectation:** [apps/orchestrator/tests.py:824](apps/orchestrator/tests.py:824) expects “search the tool catalog,” while current Sautai prose says “search the catalog.”
- **Missing cited continuity file:** [config_generator.py:2219](apps/orchestrator/config_generator.py:2219) references `CONTINUITY_openclaw-528-toolsearch.md`, absent from origin/main. Only `CONTINUITY_journal_shaping.md` is present.
- **Outdated reference documentation:** [tenant-runtime-and-provisioning.md:109](docs/reference/tenant-runtime-and-provisioning.md:109) still states an 18,000-character cap.
- **Clearly size-pressured prose:** the 387-character current-location gate, 279-character Sautai gate, sub-700 tour gates, and Fuel search sentence are deliberately compressed and test-pinned. They should not be “clarified” casually.
- **Compression likely caused the PDF drift:** `26aa21f2` shortened its failure guidance while adding the Fuel gate; the surviving “text-based” assertion is now behind current capability.
- **Operational rules only here without direct prompt tests:** cron end-state persistence [28–32](templates/openclaw/AGENTS.md:28), legacy-turn fallback [47](templates/openclaw/AGENTS.md:47), journal-link slug/quick-reply placement [51](templates/openclaw/AGENTS.md:51), USER state/tool-result precedence [26](templates/openclaw/AGENTS.md:26), identity-vs-operational-boundary wording [10](templates/openclaw/AGENTS.md:10), and most capability bullets other than reminders.

## 6. RECOMMENDATION

Apply the Tier I diet first: it frees **1,237 characters** without removing any tested rule, taking all-gates from 23,940 to **22,703**. Then either cap combined tenant extras or deliberately raise the runtime cap and CI ceiling together; do not merely widen the CI assertion. Do not move or weaken the reconcile scan/write order, Fuel chat-only/search-before-write gates, reminder success-only clause, attachment prompt-injection defense, document propose-then-save floor, mandatory marker triggers, cron end-state persistence, or any conditional call-first/anti-confabulation gate. Before moving anything on demand, fix the chat delivery mechanism or put the detail on a mandatory tool response. No files were edited; the worktree remains clean.
596,990
