# PII Self-Cleaning — Zero-Egress Hygiene + Owner Review

## Overview

The PII subsystem (see `docs/pii-redaction-security.md`) mints `[TYPE_N]`
placeholders into `Tenant.pii_entity_map` at detection time. A production audit
of the canary tenant found that **979 of 1,103 bindings were junk** — the
DeBERTa NER model, run over text it was never trained on (agent-authored
markdown, raw email bodies, financial jargon), minted placeholders for
structure fragments, invisible characters, and common words.

We used to clean this up with an **hourly cron that shipped PERSON/LOCATION span
text to Claude Haiku via OpenRouter** (`apps/pii/arbiter.py`) and asked the model
to judge each span. That cloud round-trip is being **retired**. It is replaced by
a two-part stack that keeps every PII value inside the platform boundary:

1. **Local deterministic hygiene** — stop minting junk at the source, validate
   what neural labels claim, and sweep the accumulated backlog with pure Python
   rules. No network call, no model judgment.
2. **On-device / owner review** — the ambiguous residue (real-looking names and
   places the deterministic layer can't rule on) is surfaced to the *owner's own
   authenticated clients* for a one-tap decision. See
   `docs/ios-chat-redaction-transparency-directive.md` §Phase 2.

> **Egress guarantee.** The retired arbiter was the *only* component that sent a
> PII value to a cloud model. After this change, **no PII value leaves the
> platform boundary except to the owner's own authenticated clients** (the same
> JWT-scoped surfaces where the owner already sees their real data). Detection,
> validation, and the junk sweep all run in-process in the Django container.

## The audit taxonomy

The 979 junk bindings fell into five classes. Each maps to a specific fix below.

| # | Class | Examples | Source path | Fix |
|---|-------|----------|-------------|-----|
| a | Markdown / structure fragments | `Quick Wins\n-`, `\|----\|----\|`, `### 08:05 — Neighbor`, `- **06:02**` | Agent-authored notes via **memory-sync** | P0 replace-only + P2 span hygiene |
| b | Newsletter senders + zero-width / invisible-char runs | sender display names, `​`/`﻿` runs from raw email bodies | **Tool** responses | P0 validated-only + P2 span hygiene |
| c | Unvalidated neural financial labels | `django` / `USER.md` minted `CREDIT_CARD`; temperature ranges minted `ACCOUNT` | Any path | P1 validators |
| d | Word-boundary fragments + placeholder self-redaction | `amaica`, `tingham Forest`, `ikeisha Mathison`; `CODE_1]ADDRESS`, `[CRYP` | Any path | P2 span hygiene + placeholder masking |
| e | Dates / times / bare numbers as identifiers | timestamps minted `ACCOUNT`/`PASSWORD`, bare integers minted `IP_ADDRESS` | Any path | P1 validators + P2 span hygiene |

The common thread: the DeBERTa model (`lakshyakh93/deberta_finetuned_pii`) was
trained on synthetic ai4privacy prose. Fed anything else — a markdown table, a
mailer's HTML-to-text dump, a workout log — it hallucinates spans and labels.
The fixes below narrow *where* it is allowed to mint and *validate what it
claims* before a placeholder is ever written.

## P0 — mint-gating by source

The single most important change: **not every text path is trusted to mint new
placeholders.** The audit's biggest junk sources (classes a and b) were paths
where the text is not the user speaking — it is the *agent* writing notes, or a
*tool* returning a third party's email. Minting off those paths is what produced
structural garbage and newsletter senders.

Every redaction entry point now declares a `source`, and the source decides
whether the NER/mint pass runs at all:

```
                    MINT GATING BY SOURCE
                    =====================

chat          User typed it. Trusted.        →  FULL MINT
              (redact_user_message, called       Step-1 replace known entities
              from chat_views / line_webhook     + NER detect + mint new [TYPE_N]
              / poller)

memory-sync   Agent wrote it into workspace   →  REPLACE-ONLY
              notes. Not the user speaking.       Step-1 replace known entities;
              (RedactionSession in                NEVER mint from NER. Nothing
              orchestrator/memory_sync.py)        new is PII the user disclosed —
                                                  it is the agent's own prose.

tool          A third party's data (Gmail     →  VALIDATED-ONLY
              body, calendar). Untrusted          Step-1 replace + NER detect,
              structure.                          but a new mint must clear the
              (redact_tool_response in            P1 validators before it is
              integrations/runtime_views.py)      written.
```

Rationale per source:

- **chat = full mint.** The user typed this. A newly-seen name or place here is
  a genuine disclosure the assistant must not receive in the clear. This is the
  one path that must mint freely. (It is also the path the owner can review and
  opt out of per-value — see the transparency directive.)
- **memory-sync = replace-only.** Workspace documents (daily notes, recaps,
  goals) are written *by the assistant*, which already reasons in placeholder
  space. Any real PII in them was already minted on the chat/tool path that fed
  them. Re-running NER over the agent's markdown only invents junk (class a). So
  memory-sync **substitutes known entities** (keeps `[PERSON_1]` consistent
  across documents) but **never mints a new placeholder**.
- **tool = validated-only.** Tool responses carry PII about *other people*
  (email correspondents, attendees) that genuinely must be redacted — so we
  cannot go replace-only here. But raw mailer bodies are the densest junk source
  (class b: invisible characters, sender chrome). So the tool path mints, but
  only spans that survive the P1 validators.

Implementation: thread a `source` argument through `redact_user_message`,
`RedactionSession`, and `redact_tool_response`, and gate the post-NER mint loop
on it. The Step-1 known-entity replacement (`_sub_outside_placeholders` over
`inverted_names_ci`) runs on all three — it never mints, it only reuses existing
bindings, so it is always safe.

## P1 — validators

Neural labels are *claims*, not proof. Presidio's `CREDIT_CARD`/`IBAN_CODE`
recognizers already validate (Luhn, country checksum); the DeBERTa financial and
identifier labels do not — which is how `django` became a `CREDIT_CARD` and a
temperature range became an `ACCOUNT` (class c), and timestamps became
`PASSWORD`/`ACCOUNT` (class e).

The validators live in `apps/pii/hygiene.py` and run **before a span is minted**,
keyed by the collapsed entity type:

- **`CREDIT_CARD`** — require a plausible digit run (Luhn where length allows);
  reject spans that are mostly letters (`django`, `USER.md`).
- **`ACCOUNT` / `IBAN_CODE`** — require sufficient digit/alnum content in the
  documented format; reject temperature ranges, prose, and bare words.
- **`IP_ADDRESS`** — require dotted-quad / valid v6 / MAC shape; reject bare
  integers and version strings.
- **`PASSWORD` / `PIN`** — already carries a `LABEL_SCORE_OVERRIDES["PIN"] = 0.7`
  guard in `config.py`; the validator additionally rejects date/time-shaped
  spans (class e) that clear the score bar.
- **`PHONE_NUMBER`** — require enough digits to be a real number, not a
  rep/weight count.

`PERSON` and `LOCATION` have **no deterministic validator** — that is exactly the
class the retired arbiter existed to judge, and the class that now flows to owner
review instead. The validators only rule on types where a rule *can* be
authoritative; they never guess at a name.

Validators are pure functions (`(span_text, entity_type) -> bool`) so they are
trivially unit-testable against the labeled cleanup set and carry no import cost
on the hot path.

## P2 — span hygiene + placeholder masking

Two more deterministic filters in `apps/pii/hygiene.py`, applied to every
candidate span regardless of source or type.

### Span hygiene

Rejects spans that are structurally incapable of being real PII:

- **Word-boundary fragments** (class d) — a span whose edges fall mid-word
  (`amaica` ⊂ Jamaica, `tingham Forest` ⊂ Nottingham Forest, `ikeisha Mathison`).
  The existing `_redact_user_message` Step-1 loop already anchors stored names on
  `\b`; span hygiene extends the same discipline to *fresh* NER spans, rejecting a
  candidate whose start or end is glued to an alphanumeric it did not include.
- **Markdown / structure fragments** (class a) — table rules (`|----|`), heading
  markers, list bullets, and the leading `- **HH:MM**` timestamp shape. These are
  authored structure, never PII.
- **Invisible / zero-width runs** (class b) — spans that are empty after
  stripping zero-width (`​`, `‌`, `‍`, `﻿`) and control
  characters. A "name" made of invisible characters is a mailer artifact.
- **Bare numbers / units** — folds in the existing `_is_numeric_or_unit_span`
  and `_is_degenerate_span` checks so all the "too featureless to be PII" logic
  lives in one place.

### Placeholder masking

The self-redaction bugs (`CODE_1]ADDRESS`, `[CRYP` — class d) happen when NER is
run over text that *already contains placeholders* and flags the placeholder's
own internal tokens (`CRYPTO_ADDRESS_16`) as a fresh entity — then the
replacement loop corrupts the token into nested garbage.

The redactor already has partial guards (`_hit_inside_placeholder` drops hits
overlapping a placeholder range; `_sub_outside_placeholders` substitutes only in
the gaps). P2 hardens this into a single **mask-before-detect** step: replace
every `_PLACEHOLDER_RE` match with a neutral opaque sentinel of equal length
before handing text to `_detect_pii`, so the model literally cannot see
placeholder internals as candidate spans, then unmask before substitution. This
makes the "never re-detect a placeholder" invariant structural rather than a set
of overlap checks after the fact.

## Tier-1 junk sweep cron

The deterministic layer stops *new* junk. The **tier-1 junk sweep** drains the
*accumulated* backlog (the 979 canary bindings and their fleet equivalents). It
is the local replacement for the arbiter cron and reuses the arbiter's slot
lifecycle (see `apps/cron/views.py` / `register_system_crons.py` — the
`pii_arbiter` / `pii-arbiter` registration is removed and this task takes its
place).

For each binding whose stored span text is **tier-1 junk** — i.e. it fails a P1
validator or P2 span-hygiene check (structure, invisible-char, fragment,
date/number, degenerate) — the sweep runs **heal → deny → delete**, in that
order, and only that order:

1. **Heal.** Deleting a binding breaks historical rehydration: any stored text
   still holding `[TYPE_N]` (chat messages, journal `Document.markdown`,
   workspace files) would surface the raw placeholder to the owner
   (`rehydrate_for_tenant` passes unknown placeholders through verbatim). Because
   a tier-1 span was *never real PII*, healing is safe and correct: substitute
   the original span text back in wherever the placeholder appears, so the stored
   surface reads as it should have all along.
2. **Deny.** Add the span's `canonical_key` to `Tenant.pii_denylist`
   (`{reason: "tier1-sweep", decided_at: ...}`) so the detector stops driving
   substitution/minting off it — otherwise the next inbound message re-mints the
   same junk under a fresh number.
3. **Delete.** Remove the now-orphaned `[TYPE_N]` row from `pii_entity_map`.

This mirrors the existing `EntityRegistryBulkDeleteView` (`deny=true` =
deny-then-delete) but adds the **heal** step first, which the endpoint does not
do — the endpoint is owner-driven cleanup of confirmed junk, so the caller
accepts that old references may go raw; the cron is unattended, so it must not
leave raw placeholders behind.

Invariants:

- **Idempotent.** Once a key is denied and its row deleted, re-running finds
  nothing to do for it (the row is gone; the key is on the denylist and skipped).
  A crash mid-tenant re-converges on the next tick — heal is a value substitution
  that is safe to repeat, deny is set-membership, delete is `pop`-if-present.
- **Per-tenant isolation.** Each tenant is processed under its own
  `select_for_update` transaction (the pattern in `denylist_degenerate_pii.py`
  and `EntityRegistryBulkDeleteView`). One tenant's lock contention, empty map,
  or malformed row never blocks or corrupts another's. The sweep re-reads the
  locked map inside the transaction so it can't clobber a concurrent inbound
  mint.
- **Conservative.** Only tier-1 junk (deterministically classifiable) is swept.
  `PERSON`/`LOCATION` spans that pass span-hygiene are left alone — they go to
  owner review, never auto-deleted.

The existing `denylist_degenerate_pii` management command remains as the
narrowest, manual, single-character/punctuation backstop; the sweep is the
broader automated successor covering the full tier-1 taxonomy.

## Review-queue endpoints

The residue after deterministic hygiene — plausible names and places the rules
can't rule on — is surfaced to the owner. Three operations, all JWT-scoped to
the requesting tenant (`IsAuthenticated`, `request.user.tenant`), under
`/api/v1/tenants/settings/`:

| Purpose | Method + path | Body |
|---|---|---|
| List review candidates | `GET pii-review-queue/` | — |
| Keep a binding (it *is* PII) | `POST pii-review-queue/keep/` | `{"placeholders": ["[PERSON_1]", ...]}` |
| Clean junk (delete + deny) | `POST entity-registry/bulk/` | `{"placeholders": [...], "deny": true}` |

- **`GET pii-review-queue/`** returns the `PERSON`/`LOCATION` bindings that
  survived deterministic hygiene and have not yet been reviewed (skip anything
  already on the denylist or already kept). Each row carries the placeholder, the
  real span value (safe — same tenant, same JWT, the owner already sees it in
  their rehydrated data), and the entity type, so a client can render a decision
  card or feed a batch to an on-device classifier.
- **`POST pii-review-queue/keep/`** marks bindings as reviewed-and-real so they
  drop out of the queue permanently — the local equivalent of the arbiter's
  `arbiter_judged_at = true` stamp, but set by the *owner* rather than a cloud
  model. It writes nothing to the denylist and does not touch the mapping;
  redaction continues unchanged.
- **Clean** is the *existing* `EntityRegistryBulkDeleteView` with `deny: true` —
  no new endpoint. The owner's "clean up" action denies + deletes the bindings
  they judged junk. (For chat-context single-value opt-out, the
  `pii-denylist/` endpoints remain the lighter-weight lever that leaves the
  mapping intact for historical rehydration — see the transparency directive
  §4.)

The `keep`/`clean` split maps exactly onto the arbiter's two outcomes
(`is_pii=true` → stamp/keep, `is_pii=false` → denylist/clean), with the judgment
moved from a cloud LLM to the owner (optionally assisted by an on-device model).

## Validation

The hygiene stack and any on-device review model are scored **offline against
the labeled cleanup set** before rollout — 1,103 audited bindings preserved in
the backup table `pii_map_backup_20260709` (124 real PII, 979 junk). The gate:
deterministic hygiene must reject the junk classes it targets with **zero real-PII
false deletions** (a wrongly-swept real name breaks rehydration and leaks a raw
placeholder to the owner). The existing `apps/pii/golden_set.json` /
`golden_check.py` harness is the pattern for wiring this as a regression check.

## Files

| File | Role |
|------|------|
| `apps/pii/hygiene.py` | **NEW** — P1 validators + P2 span hygiene + placeholder masking (pure functions) |
| `apps/pii/redactor.py` | `source` gating in `redact_user_message` / `RedactionSession` / `redact_tool_response`; mask-before-detect |
| `apps/pii/entity_registry.py` | `canonical_key` / `normalize_denylist_key` / `is_denied` / `get_name` — unchanged, consumed by the sweep |
| `apps/pii/arbiter.py` | **RETIRED** — cloud-egress cron removed |
| `apps/pii/management/commands/denylist_degenerate_pii.py` | Narrow manual backstop (single-char/punctuation); superseded by the sweep for the broader taxonomy |
| `apps/cron/views.py`, `apps/cron/management/commands/register_system_crons.py` | `pii_arbiter` slot replaced by the tier-1 junk sweep |
| `apps/tenants/views.py` | `pii-review-queue/` (list) + `pii-review-queue/keep/`; `EntityRegistryBulkDeleteView` (clean) unchanged |
| `apps/tenants/urls.py` | New review-queue routes |
| `docs/ios-chat-redaction-transparency-directive.md` | §Phase 2 — on-device review client contract |

## What this does NOT change

- **Rehydration is untouched.** The owner still sees real values everywhere they
  read; the agent still reasons in placeholder space. Heal only rewrites stored
  text for bindings that were *never* real PII.
- **The denylist contract is untouched.** Denying a key suppresses future
  substitution/minting but keeps the mapping row for historical rehydration —
  except in the sweep's heal→deny→**delete** flow, where the row is deleted only
  *after* healing has removed every reference to it.
- **Fail-open still holds.** If hygiene or detection errors, redaction returns
  the original text (`redact_*` swallow exceptions); the sweep skips a tenant on
  error and retries next tick. Cleaning is never allowed to block a message or
  corrupt a map.
