# Task Completion Rule (canary)

Rule text intended to be loaded into a single tenant's `User.preferences['prompt_extras']['agents_md']` via `python manage.py set_prompt_extras` for canary validation.

If the rule resolves the stale-task-surfacing bug without regressions, promote it into the base AGENTS.md template for all tenants in a follow-up change.

## Background

The morning briefing surfaces open tasks by reading the tenant's tasks document (`nbhd_document_get(kind='tasks', slug='tasks')`) and counting days since each `- [ ]` line was added. When a user verbally reports completing a task ("messaged Patrick", "done with the CV", "finished the deck"), the agent acknowledges in chat but does **not** update the tasks document. The checkbox stays `- [ ]`, the counter keeps ticking, and the same "X days overdue" item reappears in every subsequent briefing.

The prompt rule below closes the write-back gap.

## Rule

```
## Task completion discipline

When the user reports that they have completed, dropped, or deferred a task that appears in their `tasks` document — phrases like "messaged Patrick", "done with X", "finished Y", "took care of Z", "not doing that", "drop it" — you MUST update the tasks document in the SAME TURN as your acknowledgment. Do not defer. Do not assume another system will handle it.

Procedure:

1. Call `nbhd_document_get(kind='tasks', slug='tasks')` to fetch the current markdown.
2. Find the matching task line by case-insensitive substring match on the task text. If zero lines match, acknowledge the user naturally and do not force a write. If two or more lines match ambiguously, ask the user which one before writing.
3. For completion ("done", "finished", "messaged them"): toggle `- [ ]` to `- [x]` on the matching line. Leave all other lines and content untouched.
4. For drop/defer ("not doing that", "drop it"): delete the entire task line. Do not leave it checked — the user has chosen to remove it from consideration.
5. Call `nbhd_document_put(kind='tasks', slug='tasks', markdown=<updated content>)` with the full updated document. Preserve every other line byte-for-byte.
6. In your reply, briefly confirm in-line: "✓ closed Patrick check-in" or "✓ dropped gym plan." One short phrase. Don't ceremonialise it; the user already moved on.

Why this matters: the morning briefing's "overdue" counter reads directly from the checkbox state. Until you flip it, every briefing re-raises the same closed item. The user has told you — you're the system of record.
```

## Deployment

```bash
# Set the rule on the canary tenant
python manage.py set_prompt_extras \
    --tenant-id <CANARY_TENANT_UUID> \
    --section agents_md \
    --file docs/prompts/task-completion-rule.md

# Push the updated AGENTS.md to the tenant's Azure File Share
python manage.py force_apply_configs --tenant-id <CANARY_TENANT_UUID>
```

Note: `set_prompt_extras --file` reads the *whole* file. Keep the rule between backticks above as the canonical text — the management command strips surrounding whitespace but preserves the body.

Alternative: pipe just the rule block via stdin to avoid including this document's headers in the prompt:

```bash
sed -n '/^```$/,/^```$/p' docs/prompts/task-completion-rule.md | sed '1d;$d' | \
  python manage.py set_prompt_extras \
    --tenant-id <CANARY_TENANT_UUID> \
    --section agents_md \
    --stdin
```

## Rollback

```bash
python manage.py set_prompt_extras \
    --tenant-id <CANARY_TENANT_UUID> \
    --section agents_md \
    --clear
python manage.py force_apply_configs --tenant-id <CANARY_TENANT_UUID>
```

## Validation checklist

- [ ] Before set: `grep "prompt_extras" $(az storage file download ...)` returns nothing in the canary's AGENTS.md.
- [ ] After set + apply: the new rule block is present at the end of the canary's AGENTS.md.
- [ ] Canary user reports a task completion ("messaged X"). Agent responds with "✓ closed X …" AND the tasks document shows `- [x] X` on next `nbhd_document_get`.
- [ ] Next morning's briefing does NOT list X as overdue.
- [ ] A second canary test: user says "drop Y". Agent removes the `Y` line entirely. Briefing does not list Y.
- [ ] Non-canary tenants' AGENTS.md is unchanged.

If all five pass across 3–5 real days, promote the rule into the base AGENTS.md template (a separate, all-tenant change) and clear the canary extras.
