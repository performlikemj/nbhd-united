# BYO Claude OAuth — handoff research deliverables

Pre-flight research for the BYO Claude refactor described in `CONTINUITY_byo_subscription_models.md` (decision 3, Codex pattern). The implementing agent should read this directory in order before writing code.

## Read order

| # | Doc | Why first |
|---|---|---|
| 01 | [credentials-schema.md](./01-credentials-schema.md) | Validation contract for the upload endpoint. Without this the JSON validator will reject real `claude login` artifacts. |
| 02 | [symlink-rename-verification.md](./02-symlink-rename-verification.md) | Whether the `~/.claude/.credentials.json → file-share` symlink survives the binary's in-place refresh. **Unresolved — flagged for empirical verification in Phase 2.** |
| 03 | [redaction-filter-audit.md](./03-redaction-filter-audit.md) | The existing `RedactBYOPasteBody` filter handles the new JSON-blob shape — no code change needed. |
| 04 | [azure-preflight.md](./04-azure-preflight.md) | Production is in a clean baseline state; no stragglers from the legacy `setup-token` path. |
| 05 | [persona-render-pipeline.md](./05-persona-render-pipeline.md) | Exact wiring point in `apps/orchestrator/personas.py:render_workspace_files` for the conditional in-language notice. |
| 06 | [notice-draft.md](./06-notice-draft.md) | Three candidate phrasings of the persona instruction. User picks. |
| 07 | [billing-policy-flag.md](./07-billing-policy-flag.md) | **READ THIS FIRST.** OpenClaw upstream documents that Claude currently routes third-party app usage through extra-usage billing even with full OAuth credentials. May invalidate the BYO refactor's stated goal. |

## Source context (already done in earlier sessions)

- Codebase map: `apps/byo_models/` — model/views/services/tests; `apps/orchestrator/azure_client.py:922` `apply_byo_credentials_to_container`; `apps/orchestrator/personas.py:460` `render_workspace_files`; `runtime/openclaw/entrypoint.sh:80-154` credentials bootstrap (to replace); `runtime/openclaw/claude-with-token.sh` (to delete).
- OpenClaw 2026.5.7 + Claude Code 2.1.123 extracts at `/tmp/oc-pack/` and `/tmp/cc-pack/`. OpenClaw upstream docs at `/tmp/openclaw-audit/openclaw-core/docs/`.
- 0 BYO secrets exist in production KV today (verified `2026-05-11`). No back-compat for existing `cli_subscription` users.
- All 26 production tenant containers currently bind `ANTHROPIC_API_KEY → anthropic-key`. None have `CLAUDE_CODE_OAUTH_TOKEN`.

## Branch convention

This research is on `byo/claude-cli-oauth-research` (docs only). Implementing agent's code branch should be `byo/claude-cli-oauth-impl` (or similar) off `main` — they merge independently, no conflicts expected.
