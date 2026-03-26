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

## Technology: Microsoft Presidio

[Presidio](https://github.com/microsoft/presidio) is an open-source PII detection and anonymization library developed by Microsoft. It combines:

- **NLP-based detection** via spaCy language models (`en_core_web_sm`) for person names, locations, and other contextual entities
- **Pattern-based detection** via regex and the `phonenumbers` library for structured PII (emails, phone numbers, credit cards, IBANs)
- **Configurable confidence thresholds** to balance detection accuracy vs. false positives

### Why Presidio

- **Python-native**: Runs in the Django process, no external services or sidecars needed
- **Entity-type granularity**: Can enable/disable specific entity types per tier (e.g., starter gets full redaction, premium only financial)
- **Proven at scale**: Used in enterprise Microsoft products, well-maintained, active community
- **No data leaves the process**: Unlike cloud PII services, Presidio runs locally — the PII never leaves the Django container

### Engine initialization

The Presidio `AnalyzerEngine` loads the spaCy NLP model (~12MB) once per Django process and is reused across all requests via a lazy singleton (`apps/pii/engine.py`). First-call latency is ~2 seconds; subsequent calls are ~10-50ms per KB of text.

The spaCy model (`en_core_web_sm`) is baked into the production Docker image during build.

## What gets redacted

### Entity types by tier

| Entity | Starter | Premium | BYOK | Detection method |
|--------|---------|---------|------|-----------------|
| `PERSON` | Yes | No | No | spaCy NER |
| `EMAIL_ADDRESS` | Yes | No | No | Regex |
| `PHONE_NUMBER` | Yes | Yes | No | `phonenumbers` library + context words |
| `CREDIT_CARD` | Yes | Yes | No | Luhn checksum + regex |
| `IBAN_CODE` | Yes | Yes | No | Regex + checksum |
| `LOCATION` | Yes | No | No | spaCy NER |

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

- **Country/city denylist**: Names like "Jordan", "Georgia", "Victoria" that Presidio misidentifies as `PERSON` are excluded via `COUNTRY_DENYLIST` in `apps/pii/config.py`
- **User's own name excluded**: The tenant user's `display_name` (and first name) are added to an allow-list so the model can address them by name
- **Confidence threshold**: Starter tier uses 0.7, premium uses 0.8 (higher = fewer false positives)
- **Overlap deduplication**: When Presidio detects overlapping entities (e.g., "bob" as PERSON inside "bob@test.com" as EMAIL_ADDRESS), the higher-confidence match wins
- **Graceful failure**: If Presidio errors, the original text is returned unredacted — redaction never blocks the user experience

## What is NOT covered

| Gap | Reason | Risk level |
|-----|--------|-----------|
| User's own messages | Redacting confuses the model; user intentionally shared the PII | Low — user consented |
| PII the model generates from reasoning | Model may infer names from context patterns | Very low — rare |
| OpenClaw's internal conversation memory | Accumulated context from past turns lives in OpenClaw, not Django | Medium — mitigated by workspace redaction covering the densest PII |
| Tool results from non-Django plugins | If a future plugin calls external APIs directly (bypassing Django), those results won't be redacted | N/A currently — all plugins route through Django |

## Files

| File | Role |
|------|------|
| `apps/pii/__init__.py` | App init |
| `apps/pii/config.py` | Tier policies, entity types, system message |
| `apps/pii/engine.py` | Lazy-singleton Presidio analyzer + anonymizer |
| `apps/pii/redactor.py` | `redact_text()`, `RedactionSession`, `rehydrate_text()`, `redact_tool_response()`, `redact_user_message()` |
| `apps/pii/tests.py` | 40 tests covering all redaction and rehydration paths |
| `apps/orchestrator/memory_sync.py` | Workspace context redaction integration |
| `apps/orchestrator/config_generator.py` | Coordinate quantization |
| `apps/integrations/runtime_views.py` | Tool result redaction (Gmail, Calendar, Reddit) |
| `apps/router/cron_delivery.py` | Rehydration for cron/proactive messages |
| `apps/router/poller.py` | Rehydration for Telegram replies |
| `apps/router/line_webhook.py` | Rehydration for LINE replies |
| `apps/tenants/models.py` | `pii_entity_map` JSONField on Tenant |
| `Dockerfile` | `spacy download en_core_web_sm` in production image |

## Dependencies

```
presidio-analyzer>=2.2
presidio-anonymizer>=2.2
spacy>=3.7
```

spaCy model: `en_core_web_sm` (12MB, installed at Docker build time)
