# Quick-Reply Buttons (canary)

Rule text intended to be loaded into a single tenant's `User.preferences['prompt_extras']['quick_replies_md']` via `python manage.py set_prompt_extras` for canary validation. This is a SEPARATE `prompt_extras` section from `agents_md` — it composes alongside any `agents_md` extras already set on the same tenant instead of clobbering them (see `apps/orchestrator/personas.py::render_workspace_files`).

If this reads well for a canary tenant and iOS renders the buttons cleanly, promote it into the base AGENTS.md template for all tenants in a follow-up change.

## Background

The new generic quick-reply primitive (backend: `AppChatMessage.quick_replies`, parsed by `apps.router.quick_replies.extract_quick_replies`) lets the agent end a short-choice question with a machine-parsed marker so the iOS app renders tappable buttons instead of making the user type. This is pure UX sugar over the existing chat contract — a tap just sends the label back as a normal user message. No approval machinery, no new tool calls; only the marker convention below is new to the agent.

## Rule

```
## Quick-reply buttons (iOS)

When you ask the user a short CHOICE question — yes/no, or picking one of a few clear options — end your reply with a marker on its own final line so the app can show tappable buttons instead of making them type:

    [[quick-replies: Label A | Label B | Label C]]

Rules:
- 1 to 3 labels, separated by `|`.
- Each label must be 24 characters or fewer after trimming — short enough to read on a button (e.g. "Save both", "Change something", "No thanks").
- The marker must be the LAST line of your reply, alone on that line — nothing else on it, and no marker anywhere else in the reply.
- Only use this for a genuine short-choice prompt. NEVER for open-ended questions, requests for details, or anything that needs a free-text answer.
- Write labels as things the user would naturally say — tapping one sends that exact label back to you as their next message, so it should read like a real reply, not a menu item.
```

## Deployment

```bash
# Set the rule on the canary tenant
python manage.py set_prompt_extras \
    --tenant-id <CANARY_TENANT_UUID> \
    --section quick_replies_md \
    --file docs/prompts/quick-reply-buttons.md

# Push the updated AGENTS.md to the tenant's Azure File Share
python manage.py force_apply_configs --tenant-id <CANARY_TENANT_UUID>
```

Note: `set_prompt_extras --file` reads the *whole* file. Keep the rule between backticks above as the canonical text — the management command strips surrounding whitespace but preserves the body.

Alternative: pipe just the rule block via stdin to avoid including this document's headers in the prompt:

```bash
sed -n '/^```$/,/^```$/p' docs/prompts/quick-reply-buttons.md | sed '1d;$d' | \
  python manage.py set_prompt_extras \
    --tenant-id <CANARY_TENANT_UUID> \
    --section quick_replies_md \
    --stdin
```

## Rollback

```bash
python manage.py set_prompt_extras \
    --tenant-id <CANARY_TENANT_UUID> \
    --section quick_replies_md \
    --clear
python manage.py force_apply_configs --tenant-id <CANARY_TENANT_UUID>
```

## Validation checklist

- [ ] Before set: the canary tenant's live AGENTS.md has no "Quick-reply buttons" section.
- [ ] After set + apply: the section is present at the end of the canary's AGENTS.md (or after any existing `agents_md` extras — order doesn't matter, they're separate blocks).
- [ ] Ask the canary tenant's assistant a natural yes/no question in chat (e.g. via a document save-proposal flow). The reply ends with the `[[quick-replies: ...]]` marker.
- [ ] Poll `GET /api/v1/chat/messages/<client_msg_id>/` — `reply_text` has NO trace of the marker; `quick_replies` is a JSON list of the labels.
- [ ] `GET /api/v1/chat/messages/?since=` for the same turn's assistant row shows the identical `quick_replies` value (anti-drift with the detail poll).
- [ ] The agent does NOT emit the marker on an open-ended question in the same session (no over-triggering).
- [ ] Non-canary tenants' AGENTS.md is unchanged; their replies never carry the marker's stripped remnants (they never see the rule at all).

If this passes across a few real days of canary use with clean iOS rendering, promote the rule into the base AGENTS.md template (a separate, all-tenant change) and clear the canary extras.
