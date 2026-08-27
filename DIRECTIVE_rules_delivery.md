# DIRECTIVE — Rules delivery: make the assistant actually receive the rules (and free prompt space)

Author: Fable 2026-08-27. Owner: MJ ("create that directive and we'll move into another session"). Executors: codex (read-only triage → implementation waves → read-only review). Inputs: `AUDIT_agents_md_2026-08-27.md` (size map, rule inventory, history, trim plan, risks) — every claim below is cited there with file:line; re-verify before editing.

## Why (verified)
- Chat sessions cannot read files: filesystem `read` is stripped by tool policy (`docs/agents/invariants.md:71`). Yet `templates/openclaw/AGENTS.md:113-130` tells the model "rules are loaded on demand — read the relevant one" and lists ten rule files (`templates/openclaw/rules/*.md`) plus `docs/tools-reference.md`. In chat those pointers deliver nothing. Only rules duplicated into AGENTS.md, tool descriptions, or tool responses reach a chat session. Cron/background sessions CAN read the files, so the on-demand design works there only.
- Precedents already in the repo agree: a passive tool description did not cause calls, an imperative always-loaded gate did (`apps/orchestrator/personas.py:709-724`); moving mandatory reply markers to a rules file failed within a day and was reverted (`b24f3444` → `be823b39`); Gravity delivers its rules inside the tool RESPONSE after rules-file pointers proved unreliable (`apps/orchestrator/envelope.py:11`); Phase 1e/2c: server 400s are obeyed, prose is not.
- Space: lean render 16,221 / 16,300 ceiling; all-gates 23,940 / 23,950 CI ceiling (24,000 runtime cap; OpenClaw truncates silently past it — July 11 incident, `config_generator.py:2649-2666`, `personas.py:979-998`). "Session Start" is 32 % of the lean render. Tier I diet frees 1,237 chars with no tested rule removed.
- Stale text the model currently reads: "reads text-based PDFs" (scanned PDFs are supported), "can't post to social media" (approval-gated Reddit post/reply tools exist), `docs/tools-reference.md:166` lists three `nbhd_reddit_*` tools that no longer exist, `apps/orchestrator/tests.py:824` expects old Sautai wording, size comments in `personas.py`/tests are stale, `config_generator.py:2219` cites a missing `CONTINUITY_openclaw-528-toolsearch.md`, `docs/tenant-runtime-and-provisioning.md:109` says 18,000 cap.

## Principles (decided)
P1 **A rule that must hold lives in code** (server validation → 400 with the fix; tool contract; policy). Prose is advice.
P2 **Prompt text is for rules that need judgment**, kept short, always-loaded, and pinned by a rendered-prompt test (`_LOAD_BEARING_PHRASES` pattern in `apps/orchestrator/test_fuel_guidance_consistency.py`).
P3 **Situation-specific guidance travels with the tool**: in the tool's description (read when discovered) and in the tool's response (`guidance`, menus, `unmatched_*`, 400 bodies). Never "read `rules/x.md`" for chat.
P4 **Rule files stay only for cron/background sessions** and are labelled as such; chat-facing prompts never point at files.
P5 **Size policy:** fund every addition with a trim; if headroom < 1,500 after R0, raise `BOOTSTRAP_MAX_CHARS` and the CI ceiling TOGETHER by a fixed margin (e.g. 26,000 / 25,950) — never the ceiling alone.

## Tasks
R0 **Diet + stale facts (one PR, first):** apply the audit's Tier I items verbatim (−1,237: marker intro −81, duplicate chart example −97, duplicate insight example −198, PDF sentence −62 with the correction, doc-retention compression −316, Rules-table cells −483); fix "can't post to social" to the approval-gated truth; delete the three stale `nbhd_reddit_*` rows from `tools-reference.md`; fix `tests.py:824`, stale size comments, the missing-CONTINUITY reference, the 18,000 doc. Update pins deliberately; all-gates render ≤ 22,703 asserted by test. Fleet persona refresh after merge (`gh workflow run ci-cd.yml --ref main -f rollout_byo_persona_refresh=true`).
R1 **Triage every rule (codex read-only):** for each bullet in the ten rule files + `tools-reference.md`, classify into exactly one: (a) ENFORCE-IN-CODE (name the validator/contract), (b) ALWAYS-LOADED (≤ N chars, with the test that will pin it), (c) WITH-TOOL (which tool description / which response field), (d) CRON-ONLY (stays in the file, file header says so), (e) DELETE (obsolete/duplicate). Output: one table per file with evidence, the char budget for (b), and the code surface for (a)/(c). Fable arbitrates.
R2 **Implement in waves**, one PR per rule file, highest-value first (fuel and reply-markers are mostly done; memory, document-ingestion, messaging/crons next): move (c) text into descriptions/responses, add (a) validators with 400 bodies that carry the fix, keep (b) minimal and pinned, label (d) files "cron-only", delete (e). Each moved rule gets a test proving delivery (rendered prompt, tool description snapshot, or response field). Rewrite `AGENTS.md:113-130` into a single truthful sentence for chat ("tools carry their own instructions; search for a tool by name") and keep the file index only in the cron preamble.
R3 **Evals:** for the top rules (reminder capability, workout write gate, document propose-then-save, marker mandates) confirm an eval case exists (`apps/orchestrator/evals` or the eval runs cited in the audit) and add one where missing — behaviour, not wording.
R4 **Size policy** per P5, applied only if R0 leaves < 1,500 headroom; document in `docs/tenant-runtime-and-provisioning.md`.

## Acceptance
1. No chat-facing prompt text tells the model to read a file; the ten rule files are either delivered via (a)/(b)/(c) or marked cron-only.
2. All-gates render ≤ 22,703 after R0; every load-bearing phrase has a rendered-prompt pin; stale facts gone.
3. Each wave ships with a delivery test; evals green; fleet persona refresh done after each prompt change.
4. Ledger `CONTINUITY_rules_delivery.md` records the per-rule triage table and wave status.

## Run order (next session)
(0) R0 via codex in a worktree (small PR, own review) — MJ has NOT yet said go; ask once, then run. (1) R1 codex read-only triage → Fable arbitration → append the table to the ledger. (2) R2 waves. (3) R3/R4.
