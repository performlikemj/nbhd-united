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

The independent rollout gates are owned by `chat_gates.py`. Both settings are
comma-separated, case-insensitive tenant UUID allowlists, default empty, with
no wildcard support:

- `CHAT_SHAPE_TENANT_IDS`: shape endpoint, iOS per-turn fenced-block instruction,
  and persistence of fenced panels from ordinary chat replies. These are all
  Django-side and do not depend on the tenant image. Fences are stripped from
  transport text even outside the gate.
- `CHAT_PANELS_TOOL_TENANT_IDS`: journal-tools `panelsEnabled: true` config,
  both morning briefing prompts (including the typed briefing's read-only
  `nbhd_fuel_summary` tool), send-to-user serializer acceptance of explicit or
  fallback panels, and proactive panel persistence. It does not require the
  shape gate. Morning cards require fresh relevant tool evidence.

A shape-only tenant receives no `panelsEnabled` config key, no morning panel
instructions, and cannot attach panels via send-to-user. Outside the tool gate,
the original tool description/schema, generated config and morning prompts
remain byte-identical. The plugin requires a strict boolean true.

Rollout: deploy Django with its migrations; the shape gate can be enabled
independently. Set `CHAT_PANELS_TOOL_TENANT_IDS` **only AFTER verifying the
running tenant image includes PR #1638's journal plugin AND manifest schema**
(`panelsEnabled` in `openclaw.plugin.json`). Then refresh config and
regenerate/reconcile stored morning cron prompts. The chat instruction is
generated per turn. Before rolling back to an older image, remove the tenant
from the tool gate and refresh its config and stored prompts first.

There is no reliable local image-support check: `openclaw_version` identifies
the upstream binary, not the bundled plugin schema; images such as
`2026.9.4-cronfix` predate that schema despite sharing its binary version.
`container_image_tag` is not proof of the running image either:
`canary_tenant_image` deliberately does not update it. No verified plugin-schema
image registry exists. Consequently the tool allowlist is the explicit
operator attestation of manifest readiness, following the existing
manifest-ready gate pattern; never populate it based solely on a version or
DB image tag. Older manifests reject unknown config keys at load and can take
the assistant down. No deployment or tenant config refresh is part of this
change.
