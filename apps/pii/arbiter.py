"""LLM-as-arbiter cron task that prunes PII false positives into the denylist.

Issue #660. The DeBERTa NER model is left to mint conservatively (catches
real people but also tags ``goal``, ``calendar``, ``intro``, ``🏆 wins`` as
PERSON). This task sweeps recently-minted entities and asks Claude Haiku
whether each is actually a person or location worth redacting.

Per [[feedback-llm-not-formula-for-judgment]] — backend computes evidence,
LLM makes judgments. NER's binary score becomes raw signal; the LLM weighs
it with the span text itself.

Outcomes:
  - ``is_pii=false`` → canonical key written to ``Tenant.pii_denylist`` with
    ``{"reason": "arbiter", "decided_at": now}``. The redactor consults
    the denylist on the next message and stops driving redaction off the
    matching ``pii_entity_map`` entries (rehydration of historical refs
    still works because the entity_map row stays untouched).
  - ``is_pii=true`` → entity entry gets ``arbiter_judged_at`` stamped so
    the next sweep skips it.

The entity_map row is never deleted by this task — that would break
rehydration of stored placeholder references in workspace files and chat
history.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import requests
from django.conf import settings
from django.utils import timezone

from apps.pii.entity_registry import canonical_key, coerce

logger = logging.getLogger(__name__)

# Claude Haiku 4.5 over Sonnet: per-span decisions are unambiguous for the
# obvious false positives we're targeting. Sonnet would pay 5x for the
# same answer. Routed via OpenRouter to share the platform's API key
# rather than threading per-tenant BYO creds into a system task.
ARBITER_MODEL = "anthropic/claude-haiku-4-5"
ARBITER_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
ARBITER_TIMEOUT_SECONDS = 30

ARBITER_BATCH_SIZE = 50

# First-run safety valves. The 826-entry canary takes ~5 hourly ticks to
# drain at 200/tick. Steady state is a few entries per tick, so these caps
# never fire under normal traffic.
ARBITER_MAX_TENANTS_PER_RUN = 200
ARBITER_MAX_ENTRIES_PER_TENANT_PER_RUN = 200

# Placeholder types the arbiter judges. ``EMAIL_ADDRESS``, ``PHONE_NUMBER``,
# ``CREDIT_CARD``, and ``IBAN`` come from Presidio's deterministic
# recognizers (regex + checksum), so they have no false-positive class
# the arbiter could improve on.
_JUDGED_PLACEHOLDER_PREFIXES = ("PERSON_", "LOCATION_")

ARBITER_SYSTEM_PROMPT = """\
You are a PII validator. Each item is a span flagged by a NER model as potential PII for a single user. \
Decide for each span whether it is actually a personal name or a place worth redacting from messages \
that will be sent to a third-party LLM.

Return ONLY valid JSON matching this schema:
{
  "decisions": [
    {"id": <integer>, "is_pii": true|false}
  ]
}

Rules:
- Personal names ARE PII (real first names, nicknames, surnames, full names).
- Common English words and noun labels are NOT PII even if a NER model flagged them
  (e.g. "goal", "calendar", "intro", "wins", "tracker", "session").
- App / brand / product names are NOT PII for redaction purposes
  (e.g. "Sautai", "Spotify", "OpenAI", "ChatGPT").
- Bar / restaurant / venue names are NOT PII ("The Angel's Share", "Eleven Madison Park").
- Emoji, single punctuation marks, and obvious non-name fragments are NOT PII.
- Month and weekday names ("Mar", "Jan", "Mon") are NOT PII.
- Exercise names and gym jargon are NOT PII ("Pallof", "deadlift").
- Place names ARE PII when specific enough to identify a person
  (city, neighborhood, address, employer name).
- Generic geographic terms are NOT PII ("home", "office", "the gym").
- When in doubt, default to is_pii=true — we'd rather keep redacting than leak.
- Echo back the integer "id" field verbatim. Return one decision per input item.
- Do not invent ids we did not send."""


def _entries_to_judge(tenant: Any) -> list[dict[str, Any]]:
    """Return entries from ``tenant.pii_entity_map`` needing arbiter judgment.

    Skips:
      - entries already stamped ``arbiter_judged_at`` (handled in a prior sweep)
      - entries whose canonical key is already on ``pii_denylist`` (manual or
        prior arbiter decision — the redactor already short-circuits them)
      - entries with empty names (defensive — shouldn't happen post-coerce)
      - placeholder types we don't judge (EMAIL/PHONE/CREDIT_CARD/IBAN — see
        ``_JUDGED_PLACEHOLDER_PREFIXES``)
    """
    entity_map = tenant.pii_entity_map or {}
    denylist = tenant.pii_denylist or {}
    out: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for placeholder, entry in entity_map.items():
        if not any(placeholder.startswith(f"[{prefix}") for prefix in _JUDGED_PLACEHOLDER_PREFIXES):
            continue
        coerced = coerce(entry)
        name = coerced.get("name", "")
        if not name:
            continue
        if coerced.get("arbiter_judged_at"):
            continue
        key = canonical_key(name)
        if not key:
            continue
        if key in denylist:
            continue
        if key in seen_keys:
            # Multiple placeholders share this canonical key (legacy bloat
            # before [[project-entity-map-bloat-root-causes]] case-insensitive
            # merge). Judging once covers all of them — the denylist key
            # suppresses every placeholder that maps to it, and the
            # confirmed-stamp loop below stamps every duplicate placeholder
            # so they all skip the next sweep.
            continue
        seen_keys.add(key)
        out.append({"placeholder": placeholder, "name": name, "key": key})
    return out


def _call_arbiter_llm(items: list[dict[str, Any]]) -> tuple[dict[str, bool], dict[str, int]]:
    """Ask Haiku to judge each item; return ``({key: is_pii}, usage)``.

    Returns ``({}, {})`` on any failure (missing API key, HTTP error,
    non-JSON response, etc.) so the caller defers judgment to the next
    cron tick. We never default-deny: a malformed response shouldn't
    add false positives to the denylist.
    """
    api_key = getattr(settings, "OPENROUTER_API_KEY", "")
    if not api_key or not items:
        return {}, {}

    # Send integer ids the LLM echoes back, NOT the canonical key string.
    # Earlier shape round-tripped the key through the LLM and lost decisions
    # whenever the model normalized Unicode quotes / dashes / diacritics
    # (e.g. "The Angel’s Share" with U+2019 came back as straight
    # ASCII "'", so ``decisions.get(key)`` missed every time). Integers
    # have no normalization surface.
    payload_lines = [f"- id={i} name={item['name']!r}" for i, item in enumerate(items)]
    user_message = "Decide is_pii for each entry:\n\n" + "\n".join(payload_lines)

    try:
        resp = requests.post(
            ARBITER_OPENROUTER_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": ARBITER_MODEL,
                "messages": [
                    {"role": "system", "content": ARBITER_SYSTEM_PROMPT},
                    {"role": "user", "content": user_message},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0,
            },
            timeout=ARBITER_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        logger.exception("pii_arbiter LLM call failed; deferring")
        return {}, {}

    raw = (data.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("pii_arbiter returned non-JSON: %r", raw[:200])
        return {}, {}

    raw_decisions = parsed.get("decisions", [])
    out: dict[str, bool] = {}
    for d in raw_decisions:
        if not isinstance(d, dict):
            continue
        idx = d.get("id")
        is_pii = d.get("is_pii")
        # Accept ``true``/``false`` and integer ``1``/``0``; some models emit
        # the numeric form even with ``response_format=json_object`` set.
        # ``isinstance(True, int)`` is True (bool subclasses int), so we
        # have to exclude bools from the numeric branch explicitly.
        if isinstance(is_pii, bool):
            decision_bool = is_pii
        elif isinstance(is_pii, int) and is_pii in (0, 1):
            decision_bool = bool(is_pii)
        else:
            continue
        if isinstance(idx, int) and 0 <= idx < len(items):
            out[items[idx]["key"]] = decision_bool

    if raw_decisions and not out:
        # Items were sent and the LLM responded, but we couldn't extract a
        # single usable decision. Surfaces drift in the response shape so
        # future regressions don't loop silently for days (cf. the 2026-05-25
        # Unicode-apostrophe stuck-batch incident on canary).
        logger.warning(
            "pii_arbiter parsed %d decisions but matched none — raw=%r",
            len(raw_decisions),
            raw[:300],
        )

    usage = data.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    return out, usage


def _apply_decisions_for_tenant(
    tenant: Any,
    batch: list[dict[str, Any]],
    decisions: dict[str, bool],
    now_iso: str,
) -> tuple[int, int]:
    """Write denylist + judged-at stamps for a single tenant batch.

    Returns ``(denied_count, confirmed_count)``. Skips items the LLM
    didn't return a decision for — they get re-judged next tick.
    """
    from apps.tenants.models import Tenant

    entity_map = dict(tenant.pii_entity_map or {})
    denylist = dict(tenant.pii_denylist or {})
    map_changed = False
    denylist_changed = False
    denied = 0
    confirmed = 0

    # Pre-compute canonical-key → [placeholders] so a denied key stamps every
    # duplicate placeholder in one pass. Legacy bloat means a tenant may have
    # 59 placeholders all pointing to "sautai"; the dedup in _entries_to_judge
    # sends one to the LLM, but we still want all 59 marked judged.
    key_to_placeholders: dict[str, list[str]] = {}
    for ph, entry in entity_map.items():
        name = coerce(entry).get("name", "")
        k = canonical_key(name)
        if k:
            key_to_placeholders.setdefault(k, []).append(ph)

    for item in batch:
        key = item["key"]
        is_pii = decisions.get(key)
        if is_pii is None:
            continue

        if is_pii:
            confirmed += 1
        else:
            denied += 1
            if key not in denylist:
                denylist[key] = {"reason": "arbiter", "decided_at": now_iso}
                denylist_changed = True

        for placeholder in key_to_placeholders.get(key, [item["placeholder"]]):
            existing = entity_map.get(placeholder)
            if existing is None:
                continue
            if isinstance(existing, str):
                entity_map[placeholder] = {"name": existing, "arbiter_judged_at": now_iso}
                map_changed = True
            elif isinstance(existing, dict):
                if existing.get("arbiter_judged_at") == now_iso:
                    continue
                entity_map[placeholder] = {**existing, "arbiter_judged_at": now_iso}
                map_changed = True

    updates: dict[str, Any] = {}
    if map_changed:
        updates["pii_entity_map"] = entity_map
        tenant.pii_entity_map = entity_map
    if denylist_changed:
        updates["pii_denylist"] = denylist
        tenant.pii_denylist = denylist
    if updates:
        Tenant.objects.filter(pk=tenant.pk).update(**updates)

    return denied, confirmed


def _estimate_cost_dollars(usage: dict[str, int]) -> float:
    """Rough Haiku 4.5 cost estimate for telemetry only.

    Per-token prices change; this is a logging convenience, not billing
    truth. Real per-record cost is computed by ``record_usage`` from the
    canonical ``MODEL_COSTS`` table.
    """
    in_tok = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
    out_tok = usage.get("completion_tokens") or usage.get("output_tokens") or 0
    # Haiku 4.5 list pricing (Anthropic, May 2026): $1/MTok in, $5/MTok out
    return (in_tok * 1.0 + out_tok * 5.0) / 1_000_000


def pii_arbiter_task() -> dict[str, int]:
    """Hourly sweep that auto-promotes NER false positives to the denylist.

    Idempotent. Entries already on the denylist or already stamped with
    ``arbiter_judged_at`` are skipped, so re-running the sweep within
    the same hour is a no-op for steady-state tenants.

    Returns a summary dict for cron logs.
    """
    from apps.billing.services import record_usage
    from apps.tenants.models import Tenant

    now_iso = timezone.now().isoformat()
    tenants_seen = 0
    tenants_with_work = 0
    entries_judged = 0
    entries_denied = 0
    entries_confirmed = 0
    batches_sent = 0

    candidate_tenants = (
        Tenant.objects.exclude(pii_entity_map={})
        .only("id", "pii_entity_map", "pii_denylist")
        .order_by("id")[:ARBITER_MAX_TENANTS_PER_RUN]
    )

    for tenant in candidate_tenants:
        tenants_seen += 1
        items = _entries_to_judge(tenant)
        if not items:
            continue
        items = items[:ARBITER_MAX_ENTRIES_PER_TENANT_PER_RUN]
        tenants_with_work += 1

        for start in range(0, len(items), ARBITER_BATCH_SIZE):
            batch = items[start : start + ARBITER_BATCH_SIZE]
            decisions, usage = _call_arbiter_llm(batch)
            batches_sent += 1
            if not decisions:
                continue

            denied, confirmed = _apply_decisions_for_tenant(tenant, batch, decisions, now_iso)
            entries_judged += denied + confirmed
            entries_denied += denied
            entries_confirmed += confirmed

            in_tok = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
            out_tok = usage.get("completion_tokens") or usage.get("output_tokens") or 0
            if in_tok or out_tok:
                try:
                    record_usage(
                        tenant,
                        event_type="pii_arbiter",
                        input_tokens=in_tok,
                        output_tokens=out_tok,
                        model_used=ARBITER_MODEL,
                        is_system=True,
                    )
                except Exception:
                    logger.exception("pii_arbiter record_usage failed for tenant=%s", tenant.pk)

    logger.info(
        "pii_arbiter sweep complete tenants_seen=%d tenants_with_work=%d batches=%d judged=%d denied=%d confirmed=%d",
        tenants_seen,
        tenants_with_work,
        batches_sent,
        entries_judged,
        entries_denied,
        entries_confirmed,
    )
    return {
        "tenants_seen": tenants_seen,
        "tenants_with_work": tenants_with_work,
        "batches": batches_sent,
        "entries_judged": entries_judged,
        "entries_denied": entries_denied,
        "entries_confirmed": entries_confirmed,
    }
