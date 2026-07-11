# Saving from an uploaded document

The uploaded file is ephemeral (deleted ~24h after arrival). The *information*
is what persists — routed to its correct home. Follow this whenever you save
anything that came from a `[Document attached: <path>]` turn.

## Propose, then save — never on the same turn the document arrived
- On the turn the document arrives, answer the question and PROPOSE. Do not save
  yet. Show the user the exact content you'd keep — the real lines/values, quoted —
  and the destination for each piece (journal note, reminder/task, goal, fuel or
  finance entry). Group related items; don't ask a separate question per line.
- If they want the whole document kept, propose saving the extracted text
  verbatim into a single dedicated note with `nbhd_document_put` (its own note,
  not appended into today's daily note).
- Save ONLY after the user replies and agrees. If they edit the proposal, save the
  edited version. Save through the normal typed tools (`nbhd_document_put`,
  `nbhd_task_create`, `nbhd_goal_create`, the reminder tools, `nbhd_fuel_*`,
  `nbhd_finance_*`).
