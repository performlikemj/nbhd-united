# Assistant message panels

`AppChatMessage.panels` and `ProactiveOutbound.panels` hold up to six live
references, never data snapshots. The owner message detail and since-feed
expose `panels`; absence is `[]` on detail and an omitted key on feed rows.
Panel-only assistant rows remain visible in the feed.

`panels.py` owns the Pydantic v2 models, vocabulary, validation, prompt text,
and generated plugin schema. Regenerate the schema after changing the models:

```sh
python -m apps.router.panels > runtime/openclaw/plugins/nbhd-journal-tools/panel-schema.js
```

The proactive `nbhd_send_to_user` body accepts an optional `panels` list beside
`message`. Bad panels are dropped independently with content-free logs; the
first six valid references survive. Telegram and LINE receive prose, while the
stored proactive row retains panels for subsequent iOS sync. Optional titles
are guarded in placeholder space at rest and rehydrated at owner read seams.

Normal iOS replies may end with one fenced `nbhd-panels` JSON list. Finalization
strips all such blocks and attaches the first valid list to the last row of a
coalesced batch, atomically with the reply. A valid empty list also wins. An
all-invalid list allows a later valid block to win. Malformed and unclosed
blocks are removed and dropped, without logging their content. A block in the
middle is removed while surrounding prose remains. Telegram poller/queue and
LINE relays strip the blocks, as does conversation-history capture. App partial
text hides in-progress blocks without logging normal incomplete JSON as errors.

The iOS per-turn instruction and both morning briefing prompts are gated by
`CHAT_SHAPE_TENANT_IDS` (comma-separated, case-insensitive IDs, no wildcard).
Outside the gate, existing prompts remain byte-identical. Morning cards require
fresh relevant tool evidence; the typed briefing adds the read-only
`nbhd_fuel_summary` tool only inside the gate.

Rollout requires Django migrations and deployment, a tenant runtime image
containing the updated journal plugin, then regeneration/reconciliation of
stored morning cron prompts. The chat instruction is generated per turn.
`chat_panels_enabled` is temporary on this base: deduplicate it with lane B1's
`chat_shape` gate after merging. No deployment or tenant config refresh is part
of this change.
