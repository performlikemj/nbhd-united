"""Content-free tool-contract telemetry.

`emit_tool_event` is the only sanctioned writer for `ToolContractEvent`. Its job is
to make the content-free invariant STRUCTURAL rather than a rule people remember:

- **Allowlist.** Every detail key must be declared in `DETAIL_ALLOWLIST` for the
  call site's namespace (or be one of `COMMON_DETAIL_KEYS`). Unknown keys are
  dropped, never stored — including from namespaces nobody registered.
- **Shape.** Values must be scalars. Strings must look like codes, not prose:
  `SAFE_TOKEN` rejects anything containing a space or free-text punctuation, so a
  sentence cannot ride in under an allowlisted key. Dicts and lists are dropped
  outright, which is what "no nested free text" means in practice.
- **Length.** Surviving strings are capped at `MAX_STRING_LEN`.
- **Fail-open.** Telemetry observes tool calls; it must never break or slow one.
  Every failure path swallows the exception and logs it.

The dropped/truncated counts are themselves recorded (`dropped_keys`,
`truncated_keys`) so a call site that is silently losing data shows up as a number
rather than as nothing at all.
"""

from __future__ import annotations

import logging
import re
import uuid

logger = logging.getLogger(__name__)

# Longest string value we will store. Codes and enum values are far shorter; this
# is a backstop, not a budget.
MAX_STRING_LEN = 64

# Most detail dicts we will store per event. Bounds the JSONB payload.
MAX_DETAIL_KEYS = 20

# A value that "looks like a code". No whitespace, no sentence punctuation — this
# is the rule that stops prose, not the length cap.
SAFE_TOKEN = re.compile(r"\A[A-Za-z0-9._:/+@-]*\Z")

# Flags every namespace may emit. Kept tiny and structural.
COMMON_DETAIL_KEYS = frozenset(
    {
        "status",  # int HTTP status
        "method",  # GET/POST/...
        "app",  # Django app segment the runtime route is mounted under
    }
)

# Per-namespace detail keys. EXTEND THIS when a fix wave ships its enrichment
# event (Phase 4) — a key that is not listed here is dropped at write time.
DETAIL_ALLOWLIST: dict[str, frozenset[str]] = {
    # Generic capture at the runtime middleware. Common keys only, by design:
    # the middleware knows nothing tool-specific, so it must not invent flags.
    "runtime": frozenset(),
    "fuel": frozenset(
        {
            "weekday_key_style",
            "start_today_reject",
            "date_source",
            # Wave 2 fix pack — shape-only flags for the legal-but-wrong paths.
            "category",  # WorkoutCategory value (ours, post-normalization)
            "field",  # which flat-detail numeric field was malformed
            "cardio_rows_skipped",  # legacy rows the progress aggregate stepped over
            "preferred_days_style",  # int | name | mixed
            "rpe_clamped",
            "weeks",  # the plan's legal week count
            "week_key",  # the out-of-range override key
            # Phase 2c deterministic catalog/variety chain (shape only).
            "catalog_total",
            "catalog_matched",
            "catalog_unmatched",
            "catalog_coverage",
            "matched_canonical",
            "matched_slug",
            "matched_alias",
            "matched_plural",
            "matched_equipment_prefix",
            "guard_policy",
            "guard_tracks",
            "intentional_repeat",
            "rotation_compiler_expansions",
            "searched_before_write",
        }
    ),
    # "pattern" is the typed cron pattern (pure_reminder, ...) — it separates
    # "one tool is teaching the model badly" from "the whole cron surface is".
    "cron": frozenset({"tz_missing", "dow_source", "schedule_kind", "pattern"}),
    "datebook": frozenset({"origin", "image_before_config"}),
    # Wave 1 money-truth fixes. Deliberately shape-only: an account nickname, a
    # balance, or an APR value never appears here — `bound` and `*_count` say
    # WHICH WAY the input was wrong and HOW MANY rows were involved, which is all
    # a rate query needs.
    "finance": frozenset(
        {
            "account_kind",  # asset | debt
            "txn_type",  # payment | deposit | withdrawal | ...
            "bound",  # low | high | unparseable | same — which side of a range failed
            "field_count",  # partial update: fields the body actually carried
            "candidate_count",  # ambiguous nickname: how many accounts matched
            "match_tier",  # iexact | icontains
            "date_source",  # body | tenant_today
        }
    ),
}


def _clean_code(value, *, field: str, fallback: str, max_len: int) -> str:
    """Return a code-shaped string, or `fallback` when the input is not one."""
    if value is None:
        return fallback
    text = str(value)
    if not text:
        return fallback
    if not SAFE_TOKEN.match(text):
        # Refusing here (rather than sanitizing) keeps prose out of an indexed
        # column. The warning is how the developer finds their mistake.
        logger.warning("tool_event %s is not code-shaped; storing %r instead", field, fallback)
        return fallback
    return text[:max_len]


def _sanitize_detail(namespace: str, detail: dict | None) -> dict:
    """Drop everything not allowlisted, scalar, and code-shaped."""
    if not detail:
        return {}

    allowed = COMMON_DETAIL_KEYS | DETAIL_ALLOWLIST.get(namespace, frozenset())
    if namespace not in DETAIL_ALLOWLIST:
        logger.warning("tool_event namespace %r has no allowlist; common keys only", namespace)

    clean: dict = {}
    dropped = 0
    truncated = 0

    for key, value in detail.items():
        if len(clean) >= MAX_DETAIL_KEYS:
            dropped += 1
            continue
        if not isinstance(key, str) or key not in allowed:
            dropped += 1
            continue

        # bool before int: bool is an int subclass and must stay a bool.
        if value is None or isinstance(value, (bool, int, float)):
            clean[key] = value
            continue
        if isinstance(value, str):
            if not SAFE_TOKEN.match(value):
                dropped += 1
                continue
            if len(value) > MAX_STRING_LEN:
                value = value[:MAX_STRING_LEN]
                truncated += 1
            clean[key] = value
            continue

        # dicts, lists, objects — the shapes free text hides in.
        dropped += 1

    if dropped:
        clean["dropped_keys"] = dropped
    if truncated:
        clean["truncated_keys"] = truncated
    return clean


def _coerce_tenant_id(tenant_id) -> uuid.UUID | None:
    if tenant_id is None:
        return None
    if isinstance(tenant_id, uuid.UUID):
        return tenant_id
    try:
        return uuid.UUID(str(tenant_id))
    except (ValueError, AttributeError, TypeError):
        logger.warning("tool_event received an unusable tenant_id; storing NULL")
        return None


def emit_tool_event(
    *,
    tool_name: str,
    outcome: str,
    namespace: str = "runtime",
    tenant_id=None,
    reason_code: str = "",
    detail: dict | None = None,
    duration_ms: int | None = None,
):
    """Record one tool-contract event. Never raises.

    Returns the created `ToolContractEvent`, or None if emission failed (which is
    logged, not propagated — a broken telemetry table must not break a tool call).
    """
    try:
        # Imported here so the module stays importable during app loading and so
        # tests can patch the model without an import-order dance.
        from .models import ToolContractEvent

        clean_namespace = _clean_code(namespace, field="namespace", fallback="unknown", max_len=32)
        clean_tool = _clean_code(tool_name, field="tool_name", fallback="invalid_tool_name", max_len=120)

        if outcome not in ToolContractEvent.Outcome.values:
            logger.warning("tool_event got unknown outcome %r; recording as error", outcome)
            outcome = ToolContractEvent.Outcome.ERROR

        clean_reason = _clean_code(reason_code, field="reason_code", fallback="", max_len=64) if reason_code else ""

        if duration_ms is not None:
            try:
                duration_ms = max(0, int(duration_ms))
            except (TypeError, ValueError):
                duration_ms = None

        return ToolContractEvent.objects.create(
            namespace=clean_namespace,
            tool_name=clean_tool,
            tenant_id=_coerce_tenant_id(tenant_id),
            outcome=outcome,
            reason_code=clean_reason,
            detail=_sanitize_detail(clean_namespace, detail),
            duration_ms=duration_ms,
        )
    except Exception:
        # Fail-open is the whole contract: the observed call already succeeded or
        # failed on its own merits, and telemetry does not get a vote.
        logger.warning("tool_event emission failed for %r", tool_name, exc_info=True)
        return None
