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
User messages:      Telegram/LINE ──> Django ──> OpenClaw (NOT redacted — see below)

                    REHYDRATION POINTS
                    ==================

Cron responses:     OpenClaw ──> Django ──[REHYDRATE]──> Telegram/LINE
Conversation:       OpenClaw ──> Django ──[REHYDRATE]──> Telegram/LINE
```

### Why user messages are NOT redacted

User messages pass through to the model unredacted. This is intentional:

1. The user explicitly shared the PII by typing it — they want the assistant to act on it
2. Redacting user messages replaces values with `[PERSON_1]` style placeholders, which confuses the model — it interprets them as broken template variables and asks for "real" information
3. The model handles placeholders naturally in **background context** (workspace files, tool results) but rejects them in **direct conversation**

The high-value redaction targets are workspace context and tool results, which contain PII about **other people** (contacts, email correspondents, calendar attendees) that the user didn't explicitly share in the current message.

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

When the model responds with placeholders (e.g., "You got an email from [EMAIL_ADDRESS_1] about the review"), Django replaces them with original values before sending to the user via Telegram or LINE.

Rehydration points:
- `apps/router/cron_delivery.py` — cron/proactive messages (both Telegram and LINE)
- `apps/router/poller.py` — Telegram conversation replies
- `apps/router/line_webhook.py` — LINE conversation replies

The entity mapping is stored as a JSON field on the `Tenant` model (`pii_entity_map`). Example:

```json
{
    "[PERSON_1]": "Sarah Chen",
    "[EMAIL_ADDRESS_1]": "sarah.chen@acme.com",
    "[PHONE_NUMBER_1]": "415-555-0199",
    "[LOCATION_1]": "Brooklyn, NY"
}
```

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
| User's own messages | Redacting confuses the model; user intentionally shared the PII | Low — user consented |
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
| `apps/router/poller.py` | Rehydration for Telegram replies |
| `apps/router/line_webhook.py` | Rehydration for LINE replies |
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
