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
The send serializer strips fallback fences and `[[panel:...]]` markers before
quick-reply parsing on every channel. An explicit `panels` key wins, including
an empty/null/invalid value; fallback blocks and markers are always stripped.
Non-gated tenants' panels are dropped with content-free logs at validation and
persistence boundaries.

Optional titles are guarded in placeholder space at rest, truncated at a token
boundary within 60 characters, and rehydrated before applying the 60-character
owner display limit. Truncation never stores a partial PII placeholder.

Normal iOS replies may end with one fenced `nbhd-panels` JSON list. As a recovery
fallback, whole-line `[[panel:<kind>|<key>=<value>|...]]` references are also
accepted, including several markers on a line, surrounding whitespace and an
optional `title=`. They use the same Pydantic vocabulary, per-candidate validation
and six-panel limit; unknown fields, kinds, ranges and nested brackets are not
accepted. Timer `duration_seconds` is decoded as an integer before validation.
The first valid fenced list (including an empty list) takes precedence over
bracket references. Inline markers are stripped while preserving prose, but
only whole marker lines supply references. Finalization strips all such blocks and attaches the first valid list to the last row of a
coalesced batch, atomically with the reply. A valid empty list also wins. An
all-invalid list allows a later valid block to win. Malformed and unclosed
blocks are removed and dropped, without logging their content. A block in the
middle is removed while surrounding prose remains. Telegram poller/queue and
LINE relays strip the blocks and bracket markers, as does conversation-history
capture. App partial text hides in-progress blocks without logging normal incomplete JSON as errors.
Trailing partial openers (including a single backtick, ` ```n ` and ` ```nbhd `)
are withheld; cumulative text releases them if they prove to be ordinary content.
Unclosed trailing `[[panel:` markers are removed from final text and withheld
from partial text; split bracket opener prefixes are also withheld. All stripping is unconditional, independent of channel or tenant gate.
Prompts require the `panels` tool argument or the chat fence, never textual markers.

To repair existing rows, run the following (dry-run by default), then add
`--apply` to write:

```sh
python manage.py repair_panel_markers --tenant <uuid> --since <ISO-timestamp>
```

The command reports counts only, scopes both `ProactiveOutbound.message_text`
and `AppChatMessage.reply_text` by tenant and inclusive creation timestamp,
strips markers, and fills empty panels only when the tenant's **tool** gate is
on. Existing panels are preserved; proactive parsed items are refreshed. Naive
timestamps use UTC. Repeating an applied repair makes no further changes.

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


## Speculative iOS chat shape

`POST /api/v1/chat/shape/` is authenticated and gated by
`CHAT_SHAPE_TENANT_IDS`. It chooses a live panel independently of chat delivery.
Send `client_msg_id` (UUID string), `text` (up to 2,000 characters), optional
`open_panel: {kind, label}` (label up to 4,000 characters), and optional
`recent_turns: [{role: "user" | "assistant", text}]` (up to four turns, each
up to 4,000 characters). Raw fields are validated, never truncated before
redaction. `open_panel.kind` uses the same supported values as `panel` below.

Response example (illustrative timing and probabilities):

```json
{
  "enabled": true,
  "decision": "open",
  "panel": "log_table",
  "metric": "body_weight",
  "range": "this_month",
  "day": null,
  "duration_seconds": null,
  "wants_change": 0.06,
  "follow_up": 0.03,
  "confidence": 0.72,
  "reason": "ok",
  "latency_ms": 420
}
```

- `decision`: `open`, `update`, or `none`. `panel` is null for `none`.
- `panel`: `sleep`, `schedule`, `training_week`, `workout`, `timer`,
  `log_table`, or `journal_table`. `CHAT_SHAPE_PANELS` defaults to all seven;
  its comma-separated override can disable individual kinds. `tasks` remains
  a message-panel kind only.
- `metric`: optional/nullable `body_weight` or `sleep`, valid only for
  `log_table`; otherwise null. Jev `body_weight` selects
  `log_table` + `metric: "body_weight"`; Jev `journal` selects `journal_table`.
  Jev `sleep` still selects the `sleep` panel. These values reuse `panels.py`.
- `range`: `today`, `yesterday`, `tomorrow`, `this_week`, `last_week`,
  `this_month`, `last_month`, or `unspecified`; `day` is an optional ISO date.
  Explicit message dates take precedence over Jev's range.
- `duration_seconds`: positive integer for timers only, otherwise null.
- `wants_change`, `follow_up`, `confidence`: probabilities from Jev. Family
  confidence can authorize a panel while `confidence` remains below 0.5.
- `reason`: `ok`, `disabled`, `no_panel`, `low_confidence`,
  `redaction_unconfirmed`, `unavailable`, or `panel_disabled`.

The fitness family is `{training_week, workout_detail, body_weight}`. When
confidence is below 0.5, a top surface in that family with summed family
probability at least 0.6 selects `training_week`, except a body-weight
probability of at least 0.6 selects `log_table/body_weight`. The existing
workout tie rule wins: the top two surfaces must be training/workout, within
0.15 probability, with range `today`; then select `workout`. Confident
individual surfaces retain their existing mapping. A different top surface
cannot borrow family confidence. Visual usefulness (at least 0.75) or
follow-up (at least 0.5) is still required; panel rollout gates still apply.
Follow-ups update the same kind or switch within training/workout; weight
and journal tables update their own kind.

Without `open_panel`, history is omitted from both redaction and Jev state.
With `open_panel`, the latest text, every supplied history field, and label
are passed to one checked ephemeral redaction call. Only after confirmation
are the latest two history turns and label shortened to 300 characters,
extending cuts to preserve whole placeholders. Short batches fit one detector
round trip; longer batches retain overlapping bounded detector windows to
avoid silent model truncation. No PII-map writes or receipts are created.
Any unconfirmed field prevents Jev egress.

Warmup, redaction, and Jev share one 4.0-second deadline. Timeout returns
`decision: "none", reason: "unavailable"`; chat delivery remains independent.
`chat_shape_timing` logs `redact_ms`, `jev_ms`, `reason`, and `texts` (number
of fields submitted for redaction; zero if skipped), without field contents.
