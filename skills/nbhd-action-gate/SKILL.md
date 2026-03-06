---
name: nbhd-action-gate
description: "Request user confirmation before executing irreversible actions (delete, trash, send email)."
tools:
  - name: nbhd_request_action_approval
    description: |
      Request user approval before performing an irreversible action.
      ALWAYS use this tool BEFORE executing any of these operations:
      - Deleting or trashing emails (gmail trash, gmail delete)
      - Sending emails on behalf of the user (gmail send)
      - Deleting calendar events
      - Deleting Drive files
      - Deleting tasks

      The tool sends a confirmation prompt to the user and waits for their
      response (up to 5 minutes). Returns the approval status.

      DO NOT proceed with the action unless status is "approved".
      If status is "denied" or "expired", inform the user and do not retry.
      If status is "blocked", explain the tier restriction to the user.
    parameters:
      action_type:
        type: string
        description: "Type of action. One of: gmail_trash, gmail_delete, gmail_send, calendar_delete, drive_delete, task_delete"
        required: true
      display_summary:
        type: string
        description: "Human-readable description of what you want to do, e.g. 'Trash email: Re: Invoice #4521 from billing@acme.com'"
        required: true
      payload:
        type: object
        description: "Structured data about the action (message_id, event_id, file_id, etc.)"
        required: false
    command: "python3 /opt/nbhd/skills/nbhd-action-gate/scripts/request_approval.py"
---

# Action Gate

**CRITICAL: You MUST use this tool before any irreversible action.**

When the user asks you to delete an email, trash a message, send an email,
delete a calendar event, delete a Drive file, or delete a task — call
`nbhd_request_action_approval` FIRST.

## Flow

1. User asks: "Delete that email from John"
2. You call `nbhd_request_action_approval` with details
3. User gets a confirmation prompt on their phone (Telegram/LINE)
4. They tap Approve or Deny
5. Tool returns the result
6. If approved → execute the action
7. If denied/expired → tell the user, do NOT retry

## Important

- **Never skip this step.** Even if the user says "just do it" — the confirmation
  is a security feature protecting against prompt injection.
- If the tool returns `"blocked"`, explain that destructive actions aren't available
  on the Starter plan and suggest upgrading.
- The user has 5 minutes to respond. If they don't, the action expires.
