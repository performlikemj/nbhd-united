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
stored proactive row retains panels for subsequent iOS sync. Panel-only sends
persist through the app-feed branch without text transport, including for
Telegram/LINE-linked tenants without app devices. Eval sends stay isolated.
The send serializer strips fallback fences before quick-reply parsing on every
channel. An explicit `panels` key wins, including an empty/null/invalid value;
fallback blocks are always stripped. Non-gated tenants' panels are dropped with
content-free logs at validation and persistence boundaries.

Optional titles are guarded in placeholder space at rest, truncated at a token
boundary within 60 characters, and rehydrated before applying the 60-character
owner display limit. Truncation never stores a partial PII placeholder.

Normal iOS replies may end with one fenced `nbhd-panels` JSON list. Finalization
strips all such blocks and attaches the first valid list to the last row of a
coalesced batch, atomically with the reply. A valid empty list also wins. An
all-invalid list allows a later valid block to win. Malformed and unclosed
blocks are removed and dropped, without logging their content. A block in the
middle is removed while surrounding prose remains. Telegram poller/queue and
LINE relays strip the blocks, as does conversation-history capture. App partial
text hides in-progress blocks without logging normal incomplete JSON as errors.
Trailing partial openers (including a single backtick, ` ```n ` and ` ```nbhd `)
are withheld; cumulative text releases them if they prove to be ordinary content.

The tool surface, panel acceptance, iOS per-turn instruction, and both morning briefing prompts are gated by
`CHAT_SHAPE_TENANT_IDS` (comma-separated, case-insensitive IDs, no wildcard).
Config generation emits journal-tools `panelsEnabled: true` only inside that
gate; the plugin requires a strict boolean true. Outside it, the original tool
description/schema and generated config remain byte-identical. Morning cards require
fresh relevant tool evidence; the typed briefing adds the read-only
`nbhd_fuel_summary` tool only inside the gate.

Rollout requires Django migrations and deployment, a tenant runtime image
containing the updated journal plugin AND manifest, then allowlist activation
and config refresh plus regeneration/reconciliation of stored morning cron
prompts. This review adds a new manifest config key: do not activate the gate or
emit `panelsEnabled` for an image whose manifest has not been verified. The chat instruction is generated per turn.
`chat_panels_enabled` is temporary on this base: deduplicate it with lane B1's
`chat_shape` gate after merging. No deployment or tenant config refresh is part
of this change.
