# Input handling & injection surface

Every place untrusted bytes enter the platform, and what they can reach once
inside. The headline risk is **prompt injection**: the `oc-*` per-tenant
runtime is LLM-driven, has its own filesystem (the mounted Azure File Share)
and a proactive-send tool, and untrusted inbound text from Telegram/LINE/iOS
flows directly into its context window. Everything else here — webhook
validation, SSRF, file-share writes, SQL, frontend XSS — is scoped in terms
of what it could hand to, or receive from, that same LLM.

Builds on [`../reference/messaging-and-channels.md`](../reference/messaging-and-channels.md)
(inbound/outbound plumbing, PII redaction seam), [`../reference/tenant-runtime-and-provisioning.md`](../reference/tenant-runtime-and-provisioning.md)
(tool policy, config write gates, action-gating, sanitize chokepoint) and
[`../agents/invariants.md`](../agents/invariants.md) (#2 sanitize chokepoint,
#3 inbound dedup). Citations are `path:line` against this repo's current
`main`. Two source documents predate the current fleet and are cited as
**drafts** where their proposals were never implemented — this doc verifies
current code, not intent.

## 1. Prompt injection: untrusted input → LLM context → tool access

### 1.1 The path

Every inbound message (Telegram poller, LINE webhook, iOS/web chat POST) is
redacted for PII and marker-injected, then handed to the `oc-*` container as
the `user` turn of an OpenClaw chat completion
(`../reference/messaging-and-channels.md` §"The shape of a turn"). There is
**no injection-payload filtering** anywhere in that path — PII redaction
(`apps/pii/redactor.py:928` `redact_user_message`) substitutes PERSON/EMAIL/etc.
entities with placeholders for *confidentiality*; it does nothing to strip
or neutralize adversarial instruction text ("ignore previous instructions…").
A crafted inbound message, a forwarded document's extracted text
(`apps/router/poller.py:1180`+, `[Document attached: <path>]` → the OpenClaw
`pdf` tool), or Gmail/Calendar content pulled in via a connected integration
(§1.5) can all carry injected instructions into the same context the
legitimate user's turn occupies.

### 1.2 What a subverted agent can do

**Filesystem.** The container's mounted workspace (`ws-<tenant>` File Share,
`/home/node/.openclaw`) holds `AGENTS.md`, `USER.md`, `SOUL.md`, `IDENTITY.md`,
inbound media (`workspace/media/inbound/`), and — for tenants with Google
Workspace connected — **`gws-credentials.json` in plaintext**
(`apps/integrations/services.py:637` `_write_gws_credentials_to_file_share`,
payload built at `:655-660`: `client_id`, `client_secret`, `refresh_token`
as an `authorized_user` credential). `personas.py`'s SOUL/IDENTITY merge
functions (`../reference/tenant-runtime-and-provisioning.md` "Workspace
bootstrap files") establish that the agent itself grows parts of its own
workspace files, which means it has *some* file read/write surface beyond
Django's server-side writes. Whether that surface is scoped away from
arbitrary paths like `gws-credentials.json` is an OpenClaw-internal detail
not fully verifiable from this repo — no path-scoping for the agent's own
file tools is visible in `apps/orchestrator/tool_policy.py` or
`config_generator.py`. **Treat the credential file as reachable by the agent.**

**Outbound messaging.** The `message` tool (OpenClaw's built-in, arbitrary
`target`) is denied fleet-wide (`tool_policy.py:77`, part of
`_DENIED_TOOLS_2026_4_15`). The only send path is the custom plugin tool
`nbhd_send_to_user` (`runtime/openclaw/plugins/nbhd-journal-tools/index.js:993`),
which takes **no target parameter** — it POSTs to the tenant's own
`/send-to-user/` (`apps/router/cron_delivery.py:95` `CronDeliveryView`),
which resolves the destination server-side via `resolve_user_channel(user)`
(`cron_delivery.py:48` — the tenant's own linked Telegram/LINE/app, never
an attacker-supplied address) and rate-limits to 20/hr (`cron_delivery.py:24,30`).
**This closes the "message anyone" vector the 2026-02-14 draft plan worried
about** (`docs/agent-tool-security-plan.md` §1) — confirmed fixed, not just
proposed.

**Google Workspace CLI (`gws`).** For a tenant with Google connected
(`Integration.status=ACTIVE`, `provider="google"`), `config_generator.py:2253-2286`
wires `GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE` plus three skill directories
into `skills.load.extraDirs`. **As currently wired, only two GWS skills load:
`gws-gmail-triage`** (`skills/gws-gmail-triage/SKILL.md` — `gws gmail +triage`,
read unread inbox) **and `gws-calendar-agenda`** (`skills/gws-calendar-agenda/SKILL.md`
— `gws calendar +agenda`, read upcoming events); the comment at
`config_generator.py:2268` says "GWS skills — read-only for now." Write/delete
skills that exist fully built in the repo — `skills/gws-gmail-send/SKILL.md`
(free-form `--to <EMAIL>`), `skills/gws-drive/`, `skills/gws-calendar/`,
`skills/gws-calendar-insert/`, `skills/gws-tasks/` — are **never referenced**
in `config_generator.py` (`grep` for each skill name returns nothing outside
the `gws_skill_names` list itself) and so are dormant fleet-wide today.

### 1.3 Mitigating controls

| Control | What it covers | Citation |
|---|---|---|
| `tools.deny` version-keyed list | `gateway`, `sessions_*`, `agents_list`, `message`, `browser`, `canvas`, `nodes`, `code_execution`, `music_generate`, `video_generate` | `apps/orchestrator/tool_policy.py:52-91` |
| `elevated.enabled: False` | Host-elevated execution off fleet-wide | `tool_policy.py:171-173` |
| `nbhd_send_to_user` target-less by design | No cross-tenant / arbitrary-recipient messaging via the platform channels | `runtime/openclaw/plugins/nbhd-journal-tools/index.js:993-1030`, `apps/router/cron_delivery.py:48-83` |
| GWS write/delete skills not wired | Gmail send/trash, Drive delete, Calendar delete/insert, Task delete are dormant | `config_generator.py:2269-2273` (only triage + agenda listed) |
| `nbhd-action-gate` skill pre-loaded whenever GWS is on | Instructs the model to request approval before *any* GWS write, even though none is currently reachable | `config_generator.py:2283-2286`, `skills/nbhd-action-gate/SKILL.md` |
| Action-gating backend (`apps/actions`) | Approve/deny flow with 5-min expiry, audit log, Starter tier hard-blocked | `../reference/tenant-runtime-and-provisioning.md` §"Action gating" |
| PII redaction on inbound | Third-party PERSON/contact entities never reach the model raw (limits what a subverted agent could relay even if it tried) | `apps/pii/redactor.py:928-967`; fail-open on error (messaging doc "Risks" #4) |

### 1.4 The gap: action-gating is a prompt convention, not a code boundary

`nbhd_request_action_approval` (`skills/nbhd-action-gate/SKILL.md`) and the
underlying script (`skills/nbhd-action-gate/scripts/request_approval.py`)
create a `PendingAction`, poll `GatePollView`, and print `{"status":
"approved", "message": "User approved this action. You may proceed."}`
(`request_approval.py:172-182`) back into the model's tool-result context.
**Nothing links that approval to the actual destructive tool call.** There
is no capability token, signed nonce, or session flag the approval produces
that a subsequent `gws gmail +send` invocation must present. The two are
independent tool calls; the only thing connecting them is the model's
willingness to call the gate tool first and honor its answer. The skill's
own doc acknowledges this is a prompt-level control, not a code one:
> "Never skip this step. Even if the user says 'just do it' — the
> confirmation is a security feature protecting against prompt injection."
> (`skills/nbhd-action-gate/SKILL.md`)

`skills/gws-shared/SKILL.md` §"Security Rules" is the same pattern for the
underlying `gws` CLI itself — "Always confirm with user before executing
write/delete commands" is prose, not code. There is no allow-list on
`gws gmail +send`'s `--to` recipient (`skills/gws-gmail-send/SKILL.md:22`),
so a live deployment of that skill would let a subverted agent email
arbitrary content to an attacker-controlled address with no code-level
stop, only the model's own compliance. **Today this is latent** (§1.2 — the
send/delete skills aren't wired), but enabling them is a one-line change to
`gws_skill_names` (`config_generator.py:2269`) with no corresponding
code-level gate to add alongside it — the gap ships the moment that list grows.

Separately, `apps/actions/views.py:52-53` reads `X-Internal-Key`/`X-Tenant-Id`
on the gate endpoints, and `request_approval.py:41-42` sends exactly those
non-canonical names — internally consistent with each other, but divergent
from the canonical `X-NBHD-Internal-Key`/`X-NBHD-Tenant-Id` every other
runtime callback uses (already flagged as `../reference/tenant-runtime-and-provisioning.md`
Risks [med] "Gate endpoints read non-canonical auth headers" — confirmed
here from the client side too).

### 1.5 Residual risk

- **Plaintext OAuth refresh token on the share.** `gws-credentials.json`
  (§1.2) is a standing credential — anyone who can read it can mint fresh
  Google access tokens for that user indefinitely, independent of any
  future tool-policy tightening. It is written via a **hand-rolled**
  `ShareFileClient.upload_file` call (`apps/integrations/services.py:684-701`)
  that does **not** go through `_put_share_file`/`sanitize_share_text`
  (`apps/orchestrator/azure_client.py:427-489`) — contradicting invariants.md
  #2's "no hand-rolled share upload anywhere else" and the tenant-runtime
  doc's identical claim. It's system-generated JSON (no control-byte
  injection risk from redaction bypass), so the *sanitize* omission is
  benign, but the write-path inconsistency itself is worth closing so the
  chokepoint claim stays true.
- **Injected content re-entering context via read-only GWS.** Even
  read-only `gws gmail +triage` pulls sender/subject text — attacker-
  controlled if the attacker can email the tenant — into the agent's
  context, same class of risk as any inbound channel. No stronger than the
  existing Telegram/LINE/PDF vectors, but widens the number of untrusted
  entry points per Google-connected tenant.
- **`pdf` tool (2026.5.28+, `tool_policy.py:114-126`) is a second document-
  injection vector** alongside images — extracted PDF text enters context
  the same way OCR'd/transcribed text already does; no new mitigation is
  needed but no new one exists either.
- **Fail-open PII redaction** (messaging doc Risks #4) means a redaction
  outage sends raw PII to the model provider silently — orthogonal to
  injection but compounds the blast radius of a successful one.
- **No web_fetch SSRF config in this repo** — see §3.

## 2. Webhook / API input validation

| Surface | Size cap | Type validation | Auth | Citation |
|---|---|---|---|---|
| Telegram poller (photo) | 5 MB, checked before download | Telegram-side (no re-sniff) | Bot token, control-plane owned | `apps/router/poller.py:541-558` |
| Telegram poller (document) | 10 MB, checked before download | filename/mime from Telegram, extracted text truncated to 10k chars | same | `poller.py:1229-1291` |
| LINE webhook | Signature-verified body; outbound capped 5000 chars | — | HMAC-SHA256, `hmac.compare_digest` | `apps/router/line_webhook.py:64-77`, `:499` |
| Stripe webhook | Django default body limits | Stripe SDK verifies structure | `stripe.Webhook.construct_event` w/ signing secret, `ValueError`/`SignatureVerificationError` → 400 | `apps/billing/views.py:127-142` |
| iOS/web chat (`ChatMessageView`) | **`_MAX_REQUEST_BODY_BYTES`** pre-body `Content-Length` gate (sized for the largest allowed attachment), `_MAX_CHARS=8000` text cap, `_CLIENT_MSG_ID_MAX=64` | magic-byte sniff (below) | JWT `IsAuthenticated` | `apps/router/chat_views.py:61-76, 490-502` |
| Inbound image (iOS/web) | 1.5 MB post-decode (`MAX_APP_IMAGE_BYTES`), base64-length pre-check before allocating | **magic bytes only** — jpg/png/gif/webp; client-declared mime ignored | via chat auth | `apps/router/inbound_media.py:36, 55-69, 124-137` |
| Inbound document (iOS/web) | 10 MB post-decode (`MAX_APP_DOCUMENT_BYTES`, matches OpenClaw `pdf` tool's own ceiling) | magic bytes only — `%PDF-` header; renamed archives/executables rejected | via chat auth | `inbound_media.py:43, 72-81` |
| Container→Django callbacks (progress, cron delivery, gate) | — | — | per-tenant `X-NBHD-Internal-Key` + tenant-scope check, `secrets.compare_digest`, every attempt audit-logged | `apps/integrations/internal_auth.py:123` (per `../reference/messaging-and-channels.md`) |

The image/document ingress path (#1071/#996) is a real security control, not
just format handling: it never trusts a client-declared content-type,
sniffs decoded bytes against a fixed magic-byte allow-list, and rejects
anything else outright (`inbound_media.py:164-167, 192-195`) — a renamed
executable or archive can never be stored with a `.jpg`/`.pdf` extension or
handed to the vision/`pdf` tool. Binary bytes never ride the `PendingMessage`
queue row (only a path marker does — `inbound_media.py:6-10`), bounding
queue-row bloat independent of the size caps above.

One gap: `_MAX_REQUEST_BODY_BYTES` (`chat_views.py:490-502`) trusts the
`Content-Length` header for its pre-body reject; the comment at
`chat_views.py:481` acknowledges an absent/chunked length falls back to
Django's own capped `.body` read rather than this guard — so the coarse
OOM defense is `Content-Length`-only, with Django's default as the backstop
for the chunked-encoding case. Not verified whether that backstop is sized
consistently with `_MAX_REQUEST_BODY_BYTES`.

Stripe, LINE, and the container→Django callbacks all verify a cryptographic
signature or per-tenant secret before touching the body — no unauthenticated
write surface found among the webhook/API handlers reviewed.

## 3. SSRF

**No `web_fetch`/SSRF configuration exists in this repo.** `grep` for
`ssrf`, `allowPrivateNetwork`, `web_fetch`, and `"fetch"` across
`apps/orchestrator/config_generator.py` returns nothing; `generate_tool_config`
(`tool_policy.py:165-180`) sets only `"web": {"search": {"enabled": True}}` —
no `fetch` block, no explicit `ssrf` policy. `docs/agent-tool-security-plan.md`
(2026-02-14, **status: Draft**) proposed exactly this — an explicit
`allowPrivateNetwork: False` config, an application-level `url_validator.py`
tool interceptor, and Azure NSG rules blocking IMDS (`169.254.169.254`),
Azure wireserver (`168.63.129.16`), and the CGNAT range — **none of it was
implemented**: `apps/orchestrator/tool_interceptors.py` and
`apps/orchestrator/url_validator.py` don't exist, and `infra/README.md` is a
Terraform TODO list with no NSG/VNet module checked in, so network-layer
mitigation can't be verified from this repo at all. Current protection rests
entirely on OpenClaw's own upstream default (`allowPrivateNetwork: false`
per the draft plan's read of `infra/net/ssrf.ts`, unverified here since
OpenClaw's source isn't in this repo) — a vendor default with no
defense-in-depth layer and no test in this codebase asserting it.

Django-side outbound calls reviewed are all to fixed, hardcoded hosts —
Google's OAuth userinfo endpoint (`apps/integrations/views.py:142-146`),
Google's own token/API endpoints (`apps/integrations/google_api.py:20`) —
not attacker-influenceable URLs. BYO model configuration
(`apps/byo_models/`) swaps API **keys** only; no code path lets a tenant
set a custom `base_url`/`api_base` for LLM routing (`grep` for
`base_url`/`api_base`/`custom_llm_provider` under `apps/byo_models/` and
`config_generator.py` returns nothing tenant-controlled), so BYO doesn't
add SSRF surface. The container→Django callback direction is internal-key
authenticated, not URL-driven by tenant input (§2).

## 4. File-share write safety

The sanitize chokepoint (invariant #2) is real for the paths that go
through it: `_put_share_file` (`apps/orchestrator/azure_client.py:427`)
runs every `text=` write through `sanitize_share_text` (`:413`, strips C0
control bytes except tab/CR/LF) before an atomic `upload_file`. Config,
`AGENTS.md`/`USER.md`/`SOUL.md`/`IDENTITY.md`, and inbound-media path
markers all route through it or its `upload_workspace_file`/
`upload_workspace_file_binary` wrappers.

Two confirmed bypasses:

1. **Binary writes (`data=`) skip `sanitize_share_text` by design**
   (`azure_client.py:489`, already flagged in
   `../reference/tenant-runtime-and-provisioning.md` Risks [high]). Correct
   today (stripping control bytes would corrupt a JPEG/PDF), but it means
   any future binary→text path (a downloaded doc later injected into a
   prompt) reopens the null-byte-injection class invariant #2 exists to
   prevent.
2. **`gws-credentials.json` bypasses `_put_share_file` entirely** — it's
   a separate, hand-rolled `ShareFileClient.upload_file` call
   (`apps/integrations/services.py:637-702`, §1.5). This is the first
   confirmed violation of "no hand-rolled share upload anywhere else"
   found in this audit; low incremental risk (JSON payload, not raw user
   text) but breaks the single-writer invariant the rest of the platform
   relies on for auditability.

## 5. SQL injection

Clean. The only raw-SQL sites in the app layer (excluding migrations,
which Django generates) are:

- `set_rls_context`/`reset_rls_context` (`apps/tenants/middleware.py:21-72`)
  — parameterized (`cursor.execute("SELECT " + ", ".join(selects), params)`,
  values passed as `params`, never interpolated into the SQL string).
- `disable_rls.py` (`apps/tenants/management/commands/disable_rls.py:54`)
  — f-string builds `ALTER TABLE "schema"."table"`, but `schema`/`table`
  come from a `pg_tables` system-catalog query (`:37-45`), not request
  input; it's an ops-only management command, not reachable from any view.
- `force_hibernate_stale.py`'s `.extra(where=…, params=[cutoff, cutoff])`
  (`apps/orchestrator/management/commands/force_hibernate_stale.py:70-77`)
  — parameterized, values are server-computed timestamps.

No `.raw()`, no f-string/`%`-interpolated SQL built from request or model
input, in any DRF view, serializer, or the router/orchestrator apps.
[by-design]

## 6. Classic web (frontend)

The frontend is a static export (no SSR) — the main residual class is
stored/reflected XSS in user-authored content rendered back to the user or
other viewers.

- **No `dangerouslySetInnerHTML` anywhere** in `frontend/` (`grep` across
  all `.tsx`/`.ts` returns zero matches).
- **Markdown rendering** (`frontend/components/markdown-renderer.tsx:3-5`)
  uses `react-markdown` with `remark-gfm`/`remark-breaks` only — no
  `rehype-raw`, so raw HTML embedded in markdown source renders as inert
  text, not DOM. The one custom component override (`li`, for task-list
  checkboxes, `:38-72`) operates on parsed AST nodes, not raw strings.
- **TipTap editor** (`frontend/components/journal/markdown-editor.tsx:5,215`)
  uses stock `StarterKit` with no raw-HTML extension; ProseMirror's
  schema-based parsing sanitizes pasted HTML to the allowed node set by
  construction.

No custom HTML-sanitization code exists because none of the rendering paths
accept raw HTML in the first place — the risk is closed by library choice,
not by a sanitizer that could regress. [by-design]

## Findings

| # | Finding | Severity | Status |
|---|---|---|---|
| 1 | Action-gating (`nbhd_request_action_approval`) has no code-level link to the tool call it gates — approval is a text string in model context, not a capability check. Currently low-blast-radius because GWS write/delete skills aren't wired (§1.2), but the gap ships unmitigated the moment they are. | [high] | [partially-mitigated] — mitigated today by non-deployment of the gated actions, not by the gate itself |
| 2 | `gws-credentials.json` (Google OAuth `client_id`/`client_secret`/`refresh_token`, plaintext) sits on the tenant's mounted file share, reachable by the LLM's own file surface if unscoped — no path-scoping evidence found in this repo. | [high] | [open] |
| 3 | No SSRF configuration (`ssrf`/`allowPrivateNetwork`) in `config_generator.py`; the 2026-02-14 draft plan's interceptor/validator/NSG proposals were never implemented (files don't exist; `infra/` has no NSG module). Protection rests solely on an unverified OpenClaw upstream default. | [high] | [open] |
| 4 | `gws-credentials.json` is written via a hand-rolled `ShareFileClient` call that bypasses `_put_share_file`/`sanitize_share_text`, violating the "single writer" invariant (#2) the rest of the platform relies on. | [med] | [open] |
| 5 | Binary share writes (`data=`) bypass the sanitize chokepoint by design — correct for current image/PDF use, but no guard prevents a future binary-derived-text path from reintroducing null-byte injection. | [med] | [by-design] (tracked, already flagged in tenant-runtime doc) |
| 6 | Gate endpoints (`apps/actions/views.py`) and the gate client script both use non-canonical `X-Internal-Key`/`X-Tenant-Id` headers instead of the platform's canonical `X-NBHD-Internal-Key`/`X-NBHD-Tenant-Id` — internally consistent, but a latent trap if anything is unified later without checking both sides. | [low] | [open] |
| 7 | `nbhd_send_to_user` has no `target` parameter and routes through `resolve_user_channel` server-side — the cross-tenant/arbitrary-recipient messaging risk the draft security plan flagged as Critical is closed. | — | [by-design] (verified fixed) |
| 8 | Telegram/LINE/Stripe/iOS webhook and chat ingress all verify a signature or per-tenant secret before touching the body; image/document ingress sniffs decoded magic bytes rather than trusting client-declared MIME, with size caps enforced both pre-decode and post-decode. | — | [by-design] |
| 9 | No raw/f-string SQL built from request or model input anywhere in the app layer; the few raw-SQL sites are parameterized or system-catalog-sourced. | — | [by-design] |
| 10 | No XSS surface in the frontend — no `dangerouslySetInnerHTML`, `react-markdown` without `rehype-raw`, TipTap `StarterKit` with no raw-HTML extension. | — | [by-design] |
