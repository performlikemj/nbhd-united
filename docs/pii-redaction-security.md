# PII Redaction for LLM Provider Traffic

## Overview

NBHD United routes user data through third-party LLM providers (OpenRouter, Anthropic, OpenAI) to power each tenant's AI assistant. This document describes the PII redaction system that prevents personally identifiable information from reaching these providers unnecessarily.

## Threat Model

Each OpenClaw tenant container sends enriched prompts to LLM providers containing:

- **Workspace context**: Journal entries, goals, tasks, daily notes — accumulated personal data with contact names, email addresses, phone numbers, reflections
- **Tool results**: Gmail messages (from/to/cc addresses, message bodies), Google Calendar events (attendee emails, event descriptions), Reddit activity
- **User messages**: Whatever the user types in Telegram or LINE
- **Coordinates**: Precise lat/lon for weather forecasts

The risk varies by provider:

| Tier | Provider | Risk | Policy |
|------|----------|------|--------|
| Starter | OpenRouter (aggregator) | **High** — third-party aggregator, data passes through intermediate infrastructure | Full redaction |
| Premium | Anthropic (direct) | **Lower** — direct API with data processing agreement | Financial PII only |
| BYOK | User's own keys | **User-accepted** — user chose to use their own provider | No redaction |

## Architecture

PII redaction happens entirely in the Django control plane. No changes to OpenClaw containers or the Node.js runtime are required.

```
                    REDACTION POINTS
                    ================

Workspace sync:     Django ──[REDACT]──> Azure File Share ──> OpenClaw reads
Tool results:       Plugin ──> Django ──[REDACT]──> Plugin ──> OpenClaw LLM
Weather URLs:       Django ──[QUANTIZE coords]──> OpenClaw config
User messages:      Telegram/LINE/iOS ──> Django ──[REDACT]──> OpenClaw LLM
Owner journal edit: Web/iOS ──[RE-REDACT]──> Document.markdown ──> OpenClaw reads

                    REHYDRATION POINTS
                    ==================

Cron responses:     OpenClaw ──> Django ──[REHYDRATE]──> Telegram/LINE/iOS
Conversation:       OpenClaw ──> Django ──[REHYDRATE]──> Telegram/LINE/iOS
Journal reads:      Document.markdown ──[REHYDRATE]──> Web/iOS owner surfaces
```

### Inbound user messages are redacted

Earlier versions of this system passed user messages straight through to the model. **That is no longer true.** Inbound user text is now redacted on every channel before it reaches the assistant, and the reply is rehydrated on the way back out:

- **iOS / web chat** — `apps/router/chat_views.py:322` (`redact_user_message`), inside the `enqueue_tenant_turn` chokepoint. Only the LLM-bound payload is redacted; the user's own `AppChatMessage.user_text` is persisted verbatim so the `?since=` feed echoes exactly what they typed.
- **LINE** — `apps/router/line_webhook.py:1253`.
- **Telegram** — `apps/router/poller.py:1378`.

So the assistant reasons over placeholder space (`[LOCATION_330]`, `[PERSON_1]`), never the raw value. Newly-detected entities are minted into `Tenant.pii_entity_map` under a per-tenant row lock at detection time.

**The egress seam that closes the loop is `rehydrate_for_tenant` (`apps/pii/redactor.py:230`).** Its docstring states the load-bearing invariant: *every user-facing send path that may carry agent-authored text MUST route it through `rehydrate_for_tenant` (or `rehydrate_text`) before delivery* — otherwise a raw `[PERSON_1]` leaks to the user. `apps/pii/tests/test_rehydration_egress.py` guards that contract. Unknown placeholders (e.g. a binding the owner deleted) pass through verbatim rather than crashing.

**The split that makes this safe — owner sees real values, agent never does:**

- **Agent-facing surfaces stay redacted** — the LLM chat payload, workspace files on the Azure File Share, tool results, and the journal content the runtime re-reads (`RuntimeDailyNotesView`) all remain in placeholder space.
- **Owner-facing surfaces get real values** — anything the tenant themself reads (chat history, cron/proactive deliveries, action confirmations, journal documents) is rehydrated first. The owner shared the PII; they see it.

The old worry that redacting direct conversation confuses the model — it treats `[PERSON_1]` as a broken template variable and asks for "real" information — is mitigated by the workspace instruction doc (`templates/openclaw/docs/privacy-redaction.md`), which tells the model to preserve placeholders verbatim, plus the rehydration seam restoring real values before the owner reads the reply. The high-value targets remain workspace context and tool results, which carry PII about **other people** (contacts, email correspondents, calendar attendees) the user never typed in the current message.

## Technology: Custom DeBERTa Model + Presidio Pattern Recognizers

PII detection uses two engines:

1. **Custom DeBERTa-v3-base model** (ONNX INT8, ~230 MB) — fine-tuned on the [ai4privacy/pii-masking-400k](https://huggingface.co/datasets/ai4privacy/pii-masking-400k) dataset for contextual PII detection: names, addresses, dates of birth, passwords, usernames, phone numbers, emails, IP addresses, and ID documents. Achieves 92.4% F1 on the validation set.

2. **Presidio pattern recognizers** (regex only, no spaCy) — `CreditCardRecognizer` (Luhn checksum) and `IbanRecognizer` (country-format validation) for deterministic financial PII detection.

### Why this approach

- **Context-aware names**: The DeBERTa model distinguishes "Jordan" (person) from "Jordan" (country) contextually, eliminating the need for a manual denylist
- **Commercially licensed**: Base model (MIT) + training data (Apache 2.0) = fully commercial use
- **No data leaves the process**: Both engines run in-process in the Django container
- **Fits in 2 GiB**: The ONNX INT8 model uses ~230 MB RAM, shared across gunicorn workers via mmap
- **Deterministic financial PII**: Presidio's Luhn checksum and IBAN validation provide near-100% detection for credit cards and IBANs

### Engine initialization

The DeBERTa model loads on first use (~230 MB) via a lazy singleton in `apps/pii/engine.py`. ONNX Runtime memory-maps the weights, so they are shared across all 4 gunicorn workers via the OS page cache. First-call latency is ~2 seconds; subsequent calls are fast.

The ONNX model is baked into the production Docker image at `/app/pii-model`. Training scripts are in `scripts/train_pii_model.py` and `scripts/export_pii_model.py`.

## What gets redacted

### Entity types by tier

| Entity | Starter | Detection method |
|--------|---------|-----------------|
| `PERSON` | Yes | DeBERTa (GIVENNAME, SURNAME, USERNAME) |
| `EMAIL_ADDRESS` | Yes | DeBERTa (EMAIL) |
| `PHONE_NUMBER` | Yes | DeBERTa (TELEPHONENUM) |
| `CREDIT_CARD` | Yes | DeBERTa (CREDITCARDNUMBER) + Presidio Luhn checksum |
| `IBAN_CODE` | Yes | Presidio regex + checksum |
| `LOCATION` | Yes | DeBERTa (STREET, CITY, ZIPCODE, BUILDINGNUM) |
| `DATE_OF_BIRTH` | Yes | DeBERTa (DATEOFBIRTH) |
| `PASSWORD` | Yes | DeBERTa (PASSWORD) |
| `IP_ADDRESS` | Yes | DeBERTa (IPV4, IPV6) |
| `ID_DOCUMENT` | Yes | DeBERTa (DRIVERLICENSENUM, IDCARDNUM, PASSPORT) |
| `ACCOUNT` | Yes | DeBERTa (ACCOUNTNUM) |
| `TAX_NUMBER` | Yes | DeBERTa (TAXNUM) |
| `SOCIAL_NUMBER` | Yes | DeBERTa (SOCIALNUM) |

Configuration: `apps/pii/config.py`

### Redaction layers

**Layer 1: Workspace context** (`apps/orchestrator/memory_sync.py`)
- All journal documents (goals, tasks, ideas, daily notes) are redacted before upload to the tenant's Azure File Share
- Uses `RedactionSession` for consistent entity numbering across documents
- Entity mapping stored on `Tenant.pii_entity_map` for rehydration
- Runs every ~30 minutes via QStash cron

**Layer 2: Coordinate quantization** (`apps/orchestrator/config_generator.py`)
- User's precise lat/lon (from `location_lat`/`location_lon`) is rounded to 1 decimal place (~11km resolution) before being embedded in weather forecast URLs
- Sufficient for weather accuracy, insufficient for street-level identification

**Layer 3: Tool result redaction** (`apps/integrations/runtime_views.py`)
- Gmail message lists: `from` field redacted
- Gmail message detail: `from`, `to`, `cc`, `bcc`, `body_text`, `snippet` redacted
- Calendar events: `summary` (may contain names), descriptions redacted
- Reddit tool results: all string values walked and redacted
- Redaction happens in the Django view before returning the API response to the plugin

### Rehydration

When the model responds with placeholders (e.g., "You got an email from [EMAIL_ADDRESS_1] about the review"), Django replaces them with original values before sending to the user via Telegram, LINE, or the iOS/web app.

Rehydration points:
- `apps/router/cron_delivery.py` — cron/proactive messages (both Telegram and LINE)
- `apps/router/poller.py` — Telegram conversation replies
- `apps/router/line_webhook.py` — LINE conversation replies
- `apps/router/pending_queue.py` — iOS/web app conversation replies (stored rehydrated at drain time in `_clean_assistant_text_for_app`)

The entity mapping is stored as a JSON field on the `Tenant` model (`pii_entity_map`). Example:

```json
{
    "[PERSON_1]": "Sarah Chen",
    "[EMAIL_ADDRESS_1]": "sarah.chen@acme.com",
    "[PHONE_NUMBER_1]": "415-555-0199",
    "[LOCATION_1]": "Brooklyn, NY"
}
```

### Per-message redaction metadata (owner transparency)

Now that inbound chat is redacted, the owner can no longer tell — from the rendered bubble alone — that a value was hidden from their assistant. To surface that, each `AppChatMessage` carries two optional JSON columns:

| Column | Covers | Shape |
|--------|--------|-------|
| `user_redactions` | placeholders minted/reused from the user's own message | `[{"placeholder": "[LOCATION_330]", "value": "july"}]` |
| `reply_redactions` | placeholders that appeared in the assistant's reply before rehydration | `[{"placeholder": "[PERSON_1]", "value": "Sarah"}]` |

`null`/absent means nothing was obfuscated on that turn. These are captured at the two write-time chokepoints where placeholder-space text and the entity map are both in scope — the user side in `apps/router/chat_views.py` (right after `redact_user_message`) and the reply side inside `_clean_assistant_text_for_app` (`apps/router/pending_queue.py`, immediately before `rehydrate_text`). Each is a pure `_PLACEHOLDER_RE` scan plus entity-map lookups — no extra NER inference and no read-path mint. (Re-running detection on a serve path would risk minting new placeholders as a side effect, mutating `pii_entity_map` on a read; capturing on write avoids that.) The `value` is the same real string the owner already sees in the rehydrated bubble, so exposing it adds no new PII.

Both fields ride the existing chat wire shapes (`_serialize_message` and the `?since=` feed row) as additive optional keys, so older clients ignore them. The client uses them to show which substrings the assistant did not see and to offer a per-value opt-out — see `docs/ios-chat-redaction-transparency-directive.md`.

### Owner-facing journal rehydration boundary

Journal content (daily recaps, tasks, goals) is written by the assistant, which runs on redacted input — so a stored task title reads `Book hotel for [LOCATION_330]`. Serving that raw to the owner leaks the placeholder; that is a missing-rehydration bug, not intended behavior. The fix rehydrates at the **owner-facing serving boundary only**, never inside the shared projection:

- **Read side** — `DocumentSerializer` (`apps/journal/document_serializers.py`, the owner document reads used by web + iOS) and `JournalStatusView` (`GET /api/v1/journal/status/`) rehydrate `markdown` / task + goal titles before returning them to the owner. This must NOT be done inside `build_journal_status`, which also feeds the OpenClaw runtime (`apps/integrations/runtime_views.py`) and must stay redacted.
- **Write side** — because both clients round-trip the same `markdown` field back on edit / tap-to-complete, `DocumentDetailView.patch` and `DocumentAppendView.post` **re-redact** the incoming markdown (`redact_user_message`) before storing. Without this, a read-then-save would land the rehydrated real value in `Document.markdown`, re-expose it to the agent via `RuntimeDailyNotesView`, and destroy the placeholder (breaking future rehydration).

This boundary is entirely server-side — iOS and web need no client change to benefit, since both already read and write these same Document endpoints.

## False positive mitigation

- **Context-aware detection**: The DeBERTa model distinguishes ambiguous names (person vs. place) using surrounding context, eliminating the need for manual denylists
- **User's own name excluded**: The tenant user's `display_name`, first name, and last name are added to an allow-list so the model can address them by name
- **Confidence threshold**: Starter tier uses 0.7 (higher = fewer false positives)
- **Adjacent span merging**: GIVENNAME + SURNAME are merged into a single PERSON entity to avoid fragmented placeholders
- **Overlap deduplication**: When multiple engines detect overlapping entities (e.g., DeBERTa CREDITCARDNUMBER and Presidio CREDIT_CARD), the higher-confidence match wins
- **Graceful failure**: If detection errors, the original text is returned unredacted — redaction never blocks the user experience

## What is NOT covered

| Gap | Reason | Risk level |
|-----|--------|-----------|
| Fail-open redaction | Redaction never blocks the user experience: if detection errors on a message, the original text is returned unredacted (`redact_user_message` swallows exceptions), so that one turn reaches the model with real values and carries no `user_redactions` metadata | Low — transient, per-message |
| PII the model generates from reasoning | Mitigated by `docs/privacy-redaction.md` workspace doc instructing the model to preserve placeholders verbatim | Low — model may still hallucinate in edge cases |
| OpenClaw's internal conversation memory | Accumulated context from past turns lives in OpenClaw, not Django | Medium — mitigated by workspace redaction covering the densest PII |
| Tool results from non-Django plugins | If a future plugin calls external APIs directly (bypassing Django), those results won't be redacted | N/A currently — all plugins route through Django |

## Files

| File | Role |
|------|------|
| `apps/pii/__init__.py` | App init |
| `apps/pii/config.py` | Tier policies, entity types |
| `apps/pii/engine.py` | Lazy-singleton DeBERTa ONNX pipeline + Presidio pattern recognizers |
| `apps/pii/redactor.py` | `redact_text()`, `RedactionSession`, `rehydrate_text()`, `redact_tool_response()`, `redact_user_message()`, `_detect_pii()` |
| `apps/pii/tests.py` | 40 tests covering all redaction and rehydration paths |
| `apps/orchestrator/memory_sync.py` | Workspace context redaction integration |
| `apps/orchestrator/config_generator.py` | Coordinate quantization |
| `apps/integrations/runtime_views.py` | Tool result redaction (Gmail, Calendar, Reddit) |
| `apps/router/cron_delivery.py` | Rehydration for cron/proactive messages |
| `apps/router/poller.py` | Inbound Telegram redaction + rehydration for Telegram replies |
| `apps/router/line_webhook.py` | Inbound LINE redaction + rehydration for LINE replies |
| `apps/router/chat_views.py` | Inbound iOS/web chat redaction + `user_redactions` capture |
| `apps/router/pending_queue.py` | iOS/web reply rehydration + `reply_redactions` capture |
| `apps/journal/document_serializers.py` | Owner-facing journal document rehydration (read side) |
| `apps/journal/status_views.py` | Owner-facing journal status rehydration (read side) |
| `templates/openclaw/docs/privacy-redaction.md` | Model instructions for preserving placeholders (starter tier only) |
| `apps/tenants/models.py` | `pii_entity_map` JSONField on Tenant |
| `Dockerfile` | ONNX model baked into image at `/app/pii-model` |
| `scripts/train_pii_model.py` | Training script for the DeBERTa PII model |
| `scripts/export_pii_model.py` | ONNX export + INT8 quantization |

## Dependencies

```
presidio-analyzer>=2.2    # Pattern recognizers only (credit card, IBAN)
onnxruntime>=1.16          # ONNX model inference
transformers>=4.35         # Tokenizer + pipeline
optimum[onnxruntime]>=1.14 # ORTModelForTokenClassification
sentencepiece>=0.2         # DeBERTa tokenizer
```

PII model: custom DeBERTa-v3-base ONNX INT8 (~230 MB, baked into Docker image at build time).
Training guide: `docs/pii-model-training.md`
