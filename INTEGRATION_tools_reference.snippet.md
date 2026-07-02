# INTEGRATION snippet — tools-reference.md (North Star tools catalog)

Owner of `templates/openclaw/docs/tools-reference.md` = the phase0 builder.
PR-4 does not edit that file directly. Integrator: please insert the subsection
below.

Why this matters: under `toolSearch`, a tool with no cued catalog entry is
effectively invisible to the model. These 5 tools need an entry with trigger
cues or the assistant will never reach for them.

## Exact placement

In `templates/openclaw/docs/tools-reference.md`, inside the
`## Journal Tools (\`nbhd-journal-tools\` plugin)` section, add a new
`### North Star (Purpose)` subsection **immediately after the `### Context & Search`
table** (the one whose last row is `nbhd_reconcile_scan`, ~line 43) and **before**
`### Lessons`.

## Subsection to insert verbatim

```markdown
### North Star (Purpose)

The user's long-horizon **direction** — the *why* above goals. **Consent-first:**
propose as a question; confirm ONLY after the user explicitly agrees. Trigger
cue: reach for these when the user talks about direction, meaning, "why am I
doing this," long-term / life goals, or a major life or career decision.

| Tool | Description |
|------|-------------|
| `nbhd_purpose_list` | List the user's North Stars (filter by status). Call before stating anything about the user's overall direction, and before proposing a new one. |
| `nbhd_purpose_propose` | Propose a North Star as `proposed` (a question, not a fact). Use **sparingly** — ~1/week — and only when a thread spans **2+ pillars**. Then ask the user to confirm. |
| `nbhd_purpose_confirm` | Confirm a proposal. **Consent gate:** requires `user_confirmed: true`, which you set ONLY after the user explicitly agrees in conversation. |
| `nbhd_purpose_update` | Refine statement/pillars, mark a confirmed one `evolving`, or `retire` one the user has moved past. Cannot promote a proposal to confirmed. |
| `nbhd_purpose_link_goal` | Link a goal to a North Star so the direction gathers the goals that serve it. |
```
