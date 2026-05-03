"""PII redaction for outgoing LLM provider traffic.

Detects and replaces PII in text before it's sent to model providers.
Uses tier-based policies: starter tier gets full redaction (OpenRouter),
premium gets financial-only (Anthropic direct), BYOK is off.

Detection uses a custom DeBERTa ONNX model (contextual PII) combined
with Presidio pattern recognizers (credit cards, IBANs).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from apps.pii.config import DEBERTA_LABEL_MAP, TIER_POLICIES

if TYPE_CHECKING:
    from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

# Matches placeholders like [PERSON_1], [EMAIL_ADDRESS_3]
_PLACEHOLDER_RE = re.compile(r"\[([A-Z_]+)_(\d+)\]")


@dataclass
class DetectedEntity:
    """A detected PII span — unified interface for DeBERTa + Presidio results."""

    entity_type: str
    start: int
    end: int
    score: float


def redact_text(
    text: str,
    *,
    tenant: Tenant | None = None,
    tier: str | None = None,
    allow_names: set[str] | None = None,
) -> str:
    """Redact PII from text based on tenant tier policy.

    For one-off redaction where you don't need the entity mapping.
    Use RedactionSession when you need to collect mappings across
    multiple texts (e.g., for rehydration).

    Returns:
        Text with PII replaced by typed placeholders like [PERSON_1].
    """
    if not text or not text.strip():
        return text

    # Resolve tier
    if tier is None and tenant is not None:
        tier = getattr(tenant, "model_tier", "starter")
    tier = tier or "starter"

    policy = TIER_POLICIES.get(tier, TIER_POLICIES["starter"])
    if not policy.get("enabled", False):
        return text

    entities = policy.get("entities", [])
    if not entities:
        return text

    try:
        result, _ = _redact(
            text,
            entities,
            policy["score_threshold"],
            allow_names or set(),
            tenant,
            type_counters={},
            entity_map={},
        )
        return result
    except Exception:
        logger.exception("PII redaction failed — returning original text")
        return text


class RedactionSession:
    """Maintains consistent entity numbering across multiple redact() calls.

    Use this when processing multiple documents for the same tenant so that
    entity numbers are unique across all texts. After processing, the
    entity_map dict maps placeholders to original values for rehydration.

    Usage::

        session = RedactionSession(tenant=tenant)
        for doc in documents:
            doc.content = session.redact(doc.content)
        tenant.pii_entity_map = session.entity_map
    """

    def __init__(
        self,
        *,
        tenant: Tenant | None = None,
        tier: str | None = None,
        allow_names: set[str] | None = None,
    ):
        self.tenant = tenant
        self.allow_names = allow_names or set()

        # Resolve tier and policy once
        if tier is None and tenant is not None:
            tier = getattr(tenant, "model_tier", "starter")
        self.tier = tier or "starter"

        policy = TIER_POLICIES.get(self.tier, TIER_POLICIES["starter"])
        self.enabled = policy.get("enabled", False)
        self.entities = policy.get("entities", [])
        self.score_threshold = policy.get("score_threshold", 0.7)

        # Cross-document state
        self._type_counters: dict[str, int] = {}
        self.entity_map: dict[str, str] = {}

    def redact(self, text: str) -> str:
        """Redact PII from text, updating the session's entity map."""
        if not text or not text.strip() or not self.enabled or not self.entities:
            return text

        try:
            result, _ = _redact(
                text,
                self.entities,
                self.score_threshold,
                self.allow_names,
                self.tenant,
                type_counters=self._type_counters,
                entity_map=self.entity_map,
            )
            return result
        except Exception:
            logger.exception("PII redaction failed — returning original text")
            return text


def rehydrate_text(text: str, entity_map: dict[str, str]) -> str:
    """Replace PII placeholders with original values.

    Args:
        text: Text potentially containing [ENTITY_TYPE_N] placeholders.
        entity_map: Mapping from placeholder to original value.

    Returns:
        Text with placeholders replaced by original values.
    """
    if not text or not entity_map:
        return text

    # Quick check: does the text contain any placeholders at all?
    if "[" not in text:
        return text

    def _replace(match: re.Match) -> str:
        placeholder = match.group(0)
        return entity_map.get(placeholder, placeholder)

    return _PLACEHOLDER_RE.sub(_replace, text)


def redact_user_message(
    text: str,
    tenant: Tenant,
    *,
    allow_user_name: bool = True,
) -> str:
    """Redact PII in a user's message before forwarding to OpenClaw.

    Reuses the tenant's existing entity map for consistency: known entities
    get the same placeholder they have in workspace context. New entities
    are detected and appended to the map.

    Args:
        allow_user_name: When True (default), the tenant user's own name is
            excluded from redaction.  Set to False for tool responses so the
            model never sees raw name fragments it can mix with contact
            placeholders.

    Returns the redacted text. Updates tenant.pii_entity_map in the DB
    if new entities are discovered.
    """
    if not text or not text.strip():
        return text

    tier = getattr(tenant, "model_tier", "starter")
    policy = TIER_POLICIES.get(tier, TIER_POLICIES["starter"])
    if not policy.get("enabled", False):
        return text

    try:
        return _redact_user_message(text, tenant, policy, allow_user_name=allow_user_name)
    except Exception:
        logger.exception("User message PII redaction failed — returning original")
        return text


def _redact_user_message(
    text: str,
    tenant: Tenant,
    policy: dict,
    *,
    allow_user_name: bool = True,
) -> str:
    """Internal: redact user message with known + new entity detection."""
    existing_map = getattr(tenant, "pii_entity_map", None) or {}

    # Step 1: Replace known entities from the existing map (exact string match)
    inverted = {v: k for k, v in existing_map.items()}  # original -> placeholder
    out = text
    for original, placeholder in sorted(inverted.items(), key=lambda x: -len(x[0])):
        # Replace longest matches first to avoid partial replacements
        out = out.replace(original, placeholder)

    # Step 2: Run detection on the (partially redacted) text for NEW entities
    # Derive current max counters from existing map
    type_counters: dict[str, int] = {}
    for placeholder_key in existing_map:
        match = _PLACEHOLDER_RE.match(placeholder_key)
        if match:
            etype, num = match.group(1), int(match.group(2))
            type_counters[etype] = max(type_counters.get(etype, 0), num)

    entities = policy.get("entities", [])
    score_threshold = policy.get("score_threshold", 0.7)

    # Build allow-list for tenant's own name (full, first, and last).
    # Skipped for tool responses (allow_user_name=False) so the model never
    # sees raw name fragments it can mix with contact placeholders.
    allow_names: set[str] = set()
    if allow_user_name:
        user = getattr(tenant, "user", None)
        if user is not None:
            display_name = getattr(user, "display_name", "") or ""
            if display_name:
                allow_names.add(display_name)
                parts = display_name.split()
                if len(parts) > 1:
                    allow_names.add(parts[0])  # first name
                    allow_names.add(parts[-1])  # last name
                elif parts:
                    allow_names.add(parts[0])

    results = _detect_pii(out, entities, score_threshold)
    results = _filter_results(results, out, allow_names)

    # Filter out results that overlap with already-replaced placeholders
    results = [r for r in results if not _PLACEHOLDER_RE.match(out[r.start : r.end])]

    if not results:
        return out

    new_map_entries: dict[str, str] = {}
    sorted_results = sorted(results, key=lambda r: r.start)
    replacements: list[tuple[int, int, str]] = []

    for result in sorted_results:
        etype = result.entity_type
        original = out[result.start : result.end]

        # Skip if this text is already a placeholder
        if _PLACEHOLDER_RE.match(original):
            continue

        # Check if this exact text is already known
        if original in inverted:
            replacements.append((result.start, result.end, inverted[original]))
            continue

        count = type_counters.get(etype, 0) + 1
        type_counters[etype] = count
        placeholder = f"[{etype}_{count}]"
        replacements.append((result.start, result.end, placeholder))
        new_map_entries[placeholder] = original

    # Apply replacements
    for start, end, placeholder in reversed(replacements):
        out = out[:start] + placeholder + out[end:]

    # Persist new entities to DB
    if new_map_entries:
        merged = {**existing_map, **new_map_entries}
        type(tenant).objects.filter(pk=tenant.pk).update(pii_entity_map=merged)
        # Update in-memory too
        tenant.pii_entity_map = merged

    return out


def redact_telegram_update(update: dict, tenant: Tenant) -> dict:
    """Redact PII in a Telegram update's message text before forwarding.

    Modifies the update dict in place and returns it.
    """
    for key in ("message", "edited_message"):
        msg = update.get(key)
        if msg and "text" in msg:
            msg["text"] = redact_user_message(msg["text"], tenant)

    # Handle callback_query.message.text
    cq = update.get("callback_query")
    if cq:
        cq_msg = cq.get("message")
        if cq_msg and "text" in cq_msg:
            cq_msg["text"] = redact_user_message(cq_msg["text"], tenant)

    return update


def redact_tool_response(data: Any, tenant: Tenant) -> Any:
    """Redact PII in a tool response (JSON dict/list) before returning to OpenClaw.

    Recursively walks the JSON structure and applies redaction to string values
    using the tenant's entity map for known entities + model for new ones.

    Skips keys that are identifiers/metadata (id, html_link, internal_date, etc.)
    to avoid corrupting structured data.
    """
    tier = getattr(tenant, "model_tier", "starter")
    policy = TIER_POLICIES.get(tier, TIER_POLICIES["starter"])
    if not policy.get("enabled", False):
        return data

    try:
        return _redact_tool_value(data, tenant, policy, _TOOL_SKIP_KEYS)
    except Exception:
        logger.exception("Tool response PII redaction failed — returning original")
        return data


# Keys whose values should NOT be redacted (IDs, URLs, timestamps, etc.)
_TOOL_SKIP_KEYS = frozenset(
    {
        "id",
        "thread_id",
        "html_link",
        "internal_date",
        "date",
        "status",
        "next_page_token",
        "result_size_estimate",
        "provider",
        "tenant_id",
        "label_ids",
        "start",
        "end",
        "message_id",
        "update_id",
    }
)


def _redact_tool_value(
    value: Any,
    tenant: Tenant,
    policy: dict,
    skip_keys: frozenset,
) -> Any:
    """Recursively redact string values in a JSON structure."""
    if isinstance(value, str):
        if not value.strip():
            return value
        # allow_user_name=False so the user's own name gets redacted too —
        # prevents the model from mixing the user's surname with contact
        # placeholders (e.g., "[PERSON_1] Jones" -> "Mitsumasa Jones").
        return redact_user_message(value, tenant, allow_user_name=False)
    elif isinstance(value, dict):
        return {
            k: (v if k in skip_keys else _redact_tool_value(v, tenant, policy, skip_keys)) for k, v in value.items()
        }
    elif isinstance(value, list):
        return [_redact_tool_value(item, tenant, policy, skip_keys) for item in value]
    else:
        return value


# ---------------------------------------------------------------------------
# Detection: DeBERTa model + Presidio pattern recognizers
# ---------------------------------------------------------------------------


def _detect_pii(
    text: str,
    entities: list[str],
    score_threshold: float,
) -> list[DetectedEntity]:
    """Detect PII using DeBERTa (contextual) + Presidio regex (financial).

    Runs the ONNX DeBERTa model for names, addresses, dates, passwords, etc.
    Runs Presidio CreditCardRecognizer and IbanRecognizer for deterministic
    financial PII with checksum validation.

    Returns a combined list of DetectedEntity, with adjacent same-type spans
    merged (e.g., GIVENNAME + SURNAME become a single PERSON span).
    """
    from apps.pii.engine import get_pattern_recognizers, get_pii_pipeline

    results: list[DetectedEntity] = []

    # 1. DeBERTa model — contextual PII
    pii_pipeline = get_pii_pipeline()
    model_results = pii_pipeline(text)

    for ent in model_results:
        if ent["score"] < score_threshold:
            continue
        entity_type = DEBERTA_LABEL_MAP.get(ent["entity_group"])
        if entity_type and entity_type in entities:
            # Trim leading/trailing whitespace from span boundaries —
            # aggregation_strategy="simple" can include boundary spaces
            start, end = ent["start"], ent["end"]
            span_text = text[start:end]
            start += len(span_text) - len(span_text.lstrip())
            end -= len(span_text) - len(span_text.rstrip())
            if start >= end:
                continue
            results.append(
                DetectedEntity(
                    entity_type=entity_type,
                    start=start,
                    end=end,
                    score=ent["score"],
                )
            )

    # Merge adjacent same-type spans (e.g., "Sarah" GIVENNAME + "Chen" SURNAME
    # both map to PERSON — merge into a single span covering "Sarah Chen")
    results = _merge_adjacent_spans(results)

    # 2. Presidio regex — credit cards (Luhn), IBANs (checksum), emails (regex fallback)
    pattern_recognizers = get_pattern_recognizers()
    for entity_type, recognizer in pattern_recognizers.items():
        if entity_type in entities:
            for r in recognizer.analyze(text=text, entities=[entity_type]):
                if r.score >= score_threshold:
                    results.append(
                        DetectedEntity(
                            entity_type=r.entity_type,
                            start=r.start,
                            end=r.end,
                            score=r.score,
                        )
                    )

    return results


def _merge_adjacent_spans(results: list[DetectedEntity]) -> list[DetectedEntity]:
    """Merge consecutive spans of the same entity type.

    After label mapping, GIVENNAME and SURNAME both become PERSON.
    "Sarah" (PERSON, 0-5) and "Chen" (PERSON, 6-10) should merge into
    "Sarah Chen" (PERSON, 0-10).

    Spans are considered adjacent if separated by 0-1 characters (a space).
    """
    if len(results) <= 1:
        return results

    sorted_results = sorted(results, key=lambda r: r.start)
    merged = [sorted_results[0]]

    for current in sorted_results[1:]:
        prev = merged[-1]
        gap = current.start - prev.end
        if prev.entity_type == current.entity_type and 0 <= gap <= 1:
            # Merge: extend previous span, use minimum score
            merged[-1] = DetectedEntity(
                entity_type=prev.entity_type,
                start=prev.start,
                end=current.end,
                score=min(prev.score, current.score),
            )
        else:
            merged.append(current)

    return merged


# ---------------------------------------------------------------------------
# Core redaction logic (placeholder assignment + string replacement)
# ---------------------------------------------------------------------------


def _redact(
    text: str,
    entities: list[str],
    score_threshold: float,
    allow_names: set[str],
    tenant: object | None,
    *,
    type_counters: dict[str, int],
    entity_map: dict[str, str],
) -> tuple[str, dict[str, str]]:
    """Run PII detection and replace with numbered placeholders.

    Mutates type_counters and entity_map in place for cross-document sessions.
    Returns (redacted_text, entity_map).
    """
    # Build the allow-list from tenant's display name (full, first, and last)
    if tenant is not None:
        user = getattr(tenant, "user", None)
        if user is not None:
            display_name = getattr(user, "display_name", "") or ""
            if display_name:
                allow_names = allow_names | {display_name}
                parts = display_name.split()
                if len(parts) > 1:
                    allow_names = allow_names | {parts[0], parts[-1]}
                elif parts:
                    allow_names = allow_names | {parts[0]}

    results = _detect_pii(text, entities, score_threshold)
    results = _filter_results(results, text, allow_names)

    if not results:
        return text, entity_map

    # Sort by position for consistent numbering
    sorted_results = sorted(results, key=lambda r: r.start)

    # Assign numbered placeholders per entity type
    replacements: list[tuple[int, int, str]] = []
    for result in sorted_results:
        etype = result.entity_type
        original = text[result.start : result.end]
        count = type_counters.get(etype, 0) + 1
        type_counters[etype] = count
        placeholder = f"[{etype}_{count}]"
        replacements.append((result.start, result.end, placeholder))
        entity_map[placeholder] = original

    # Apply replacements from end to start to preserve character positions
    out = text
    for start, end, placeholder in reversed(replacements):
        out = out[:start] + placeholder + out[end:]

    return out, entity_map


def _filter_results(
    results: list,
    text: str,
    allow_names: set[str],
) -> list:
    """Remove false positives and deduplicate overlapping spans."""
    filtered = []
    for result in results:
        matched_text = text[result.start : result.end].strip()
        matched_lower = matched_text.lower()

        # Skip allowed names (user's own name)
        if result.entity_type == "PERSON" and any(
            matched_lower == name.lower() or matched_text == name for name in allow_names
        ):
            continue

        filtered.append(result)

    # Deduplicate overlapping spans — keep the higher-score match
    filtered = _deduplicate_overlapping(filtered)

    return filtered


def _deduplicate_overlapping(results: list) -> list:
    """Remove overlapping entity spans, keeping the best match.

    When two entities overlap (e.g. PERSON "Email bob@test.com" vs
    EMAIL_ADDRESS "bob@test.com"), keep the one with the higher confidence
    score. On ties, prefer the more specific (shorter) span.
    """
    if not results:
        return results

    # Sort by start position, then by score descending
    sorted_results = sorted(results, key=lambda r: (r.start, -r.score))

    deduplicated = []
    for result in sorted_results:
        if not deduplicated:
            deduplicated.append(result)
            continue

        prev = deduplicated[-1]
        # Check for overlap: current starts before previous ends
        if result.start < prev.end:
            # Keep the higher-scoring one; on tie, prefer shorter (more specific)
            if result.score > prev.score or (
                result.score == prev.score and (result.end - result.start) < (prev.end - prev.start)
            ):
                deduplicated[-1] = result
            # Otherwise skip this result
        else:
            deduplicated.append(result)

    return deduplicated
