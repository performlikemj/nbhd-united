"""PII redaction for outgoing LLM provider traffic.

Detects and replaces PII in text before it's sent to model providers.
Uses tier-based policies from ``TIER_POLICIES``. Only ``starter`` is
defined today; every tier resolves to it via ``.get(tier, starter)``, so
redaction is effectively full for all tiers (the historical
premium=financial-only / BYOK=off split is not currently implemented).

Detection uses a custom DeBERTa ONNX model (contextual PII) combined
with Presidio pattern recognizers (credit cards, IBANs).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from apps.pii.config import DEBERTA_LABEL_MAP, LABEL_SCORE_OVERRIDES, TIER_POLICIES
from apps.pii.entity_registry import (
    canonical_key as _canonical_key,
)
from apps.pii.entity_registry import (
    get_name as _entry_name,
)
from apps.pii.entity_registry import (
    inverted_names_ci as _inverted_names_ci,
)
from apps.pii.entity_registry import (
    is_denied as _is_denied,
)
from apps.pii.entity_registry import (
    to_storage_value as _entry_storage,
)

if TYPE_CHECKING:
    from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

# Matches placeholders like [PERSON_1], [EMAIL_ADDRESS_3]
_PLACEHOLDER_RE = re.compile(r"\[([A-Z_]+)_(\d+)\]")

# A bare lift number, rep/set count, or number+unit token. Fitness logs are
# the dominant false-positive source for the contextual (PERSON/LOCATION)
# labels: "benched 225", "squatted 140kg", "5x5 at 315 lbs". These are never
# identifying PII. Anchored fullmatch against the stripped span, case-
# insensitive. NOT applied to PHONE_NUMBER/CREDIT_CARD/IBAN/EMAIL — those come
# from checksum/pattern recognizers and legitimately contain digit runs.
_NUMERIC_OR_UNIT_RE = re.compile(
    r"""
    ^(
        \d{1,4}(\.\d+)?                                              # bare number: 82, 140.5
        (\s?(kg|kgs|lb|lbs|reps?|sets?|km|mi|min|sec|hrs?|bpm|cal|kcal))?  # optional unit
        | x\d+                                                       # x10
        | \d{1,4}x\d+                                                # 5x5, 3x12
    )$
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Exercise / gym vocabulary the DeBERTa model mislabels as PERSON (e.g.
# "deadlift" → STREET, "Spider Curls" → PERSON) or LOCATION. Matched against
# the full span, lowercased + stripped. Seeded from the prod false positives
# plus the canonical lift catalogue; generous on purpose — a suppressed gym
# term is a non-event, a leaked one is the bug we're fixing. A real person who
# happens to be named one of these is vanishingly unlikely and the arbiter can
# still promote genuine names it sees in context.
_FITNESS_VOCAB = frozenset(
    {
        # Compound lifts
        "deadlift",
        "deadlifts",
        "romanian deadlift",
        "romanian deadlifts",
        "squat",
        "squats",
        "front squat",
        "front squats",
        "back squat",
        "back squats",
        "bulgarian split squat",
        "bulgarian split squats",
        "split squat",
        "split squats",
        "front foot-elevated split squat",
        "front foot-elevated split squats",
        "bench press",
        "bench",
        "incline bench press",
        "incline dumbbell press",
        "overhead press",
        "ohp",
        "military press",
        "push press",
        "hip thrust",
        "hip thrusts",
        "glute bridge",
        "glute bridges",
        "leg press",
        "hack squat",
        "clean",
        "power clean",
        "clean and jerk",
        "snatch",
        # Accessory / isolation
        "lat pulldown",
        "lat pulldowns",
        "pulldown",
        "pulldowns",
        "row",
        "rows",
        "barbell row",
        "barbell rows",
        "dumbbell row",
        "dumbbell rows",
        "pendlay row",
        "pendlay rows",
        "cable row",
        "cable rows",
        "curl",
        "curls",
        "bicep curl",
        "bicep curls",
        "hammer curl",
        "hammer curls",
        "spider curl",
        "spider curls",
        "preacher curl",
        "preacher curls",
        "tricep extension",
        "tricep extensions",
        "skullcrusher",
        "skullcrushers",
        "dip",
        "dips",
        "pushup",
        "pushups",
        "push-up",
        "push-ups",
        "pullup",
        "pullups",
        "pull-up",
        "pull-ups",
        "chinup",
        "chinups",
        "chin-up",
        "chin-ups",
        "lunge",
        "lunges",
        "walking lunge",
        "walking lunges",
        "calf raise",
        "calf raises",
        "leg raise",
        "leg raises",
        "box jump",
        "box jumps",
        "broad jump",
        "broad jumps",
        "pallof press",
        "pallof",
        "pec deck",
        "face pull",
        "face pulls",
        "lateral raise",
        "lateral raises",
        "front raise",
        "front raises",
        "shrug",
        "shrugs",
        "fly",
        "flyes",
        "flies",
        "plank",
        "planks",
        "burpee",
        "burpees",
        "kettlebell swing",
        "kettlebell swings",
        "mountain climber",
        "mountain climbers",
        "situp",
        "situps",
        "sit-up",
        "sit-ups",
        "crunch",
        "crunches",
        "russian twist",
        "russian twists",
        # General terms / supplements / equipment
        "bodyweight",
        "cardio",
        "treadmill",
        "elliptical",
        "rower",
        "spin",
        "warmup",
        "warm-up",
        "warm up",
        "cooldown",
        "cool-down",
        "cool down",
        "superset",
        "supersets",
        "dropset",
        "dropsets",
        "amrap",
        "emom",
        "rep",
        "reps",
        "set",
        "sets",
        "pr",
        "one rep max",
        "1rm",
        "rpe",
        "creatine",
        "protein",
        "whey",
        "bcaa",
        "pre-workout",
        "preworkout",
        "dumbbell",
        "dumbbells",
        "barbell",
        "kettlebell",
        "kettlebells",
        "cable",
    }
)

# Single fitness words for a TOKEN-level check: the model frequently mislabels
# multi-word or partial exercise phrases where the whole span never matches
# ``_FITNESS_VOCAB`` exactly ("vinyasa flow" arrives as 'inyasa flow', "glute
# bridge march", "pallof hold", "pec deck flys", "max bench"). If ANY token in a
# LOCATION/PERSON span is one of these, the span is an exercise note, not PII.
# Curated to fitness-only words with no plausible name/place collision — an
# ambiguous common word ("row", "set", "clean", "max") is deliberately left out
# so this never suppresses a real name or address that merely contains it.
_FITNESS_TOKENS = frozenset(
    {
        "vinyasa",
        "yoga",
        "pilates",
        "flow",
        "glute",
        "glutes",
        "pallof",
        "pec",
        "pecs",
        "deck",
        "flys",
        "flyes",
        "bench",
        "deadlift",
        "deadlifts",
        "squat",
        "squats",
        "lunge",
        "lunges",
        "burpee",
        "burpees",
        "plank",
        "planks",
        "curls",
        "shrug",
        "shrugs",
        "superset",
        "supersets",
        "dropset",
        "dropsets",
        "amrap",
        "emom",
        "treadmill",
        "elliptical",
        "creatine",
        "kettlebell",
        "kettlebells",
        "dumbbell",
        "dumbbells",
        "barbell",
        "skullcrusher",
        "skullcrushers",
        "preworkout",
    }
)

# Multi-word exercise phrases matched exactly (lowercased span). Kept separate
# from ``_FITNESS_TOKENS`` for phrases whose only distinctive token is unsafe to
# add bare — e.g. "glute bridge march" (the 'march' token collides with the
# month, so we match the whole phrase instead of adding 'march').
_FITNESS_PHRASES = frozenset(
    {
        "glute bridge march",
        "glute bridge marches",
    }
)

# Common words the DeBERTa model tags as PERSON/LOCATION at high confidence
# because they open a sentence in imperative/adjective position. None are
# personal PII: NBHD console verbs/nouns, weekday + month abbreviations, and
# timezone abbreviations. Applied token-wise to LOCATION/PERSON spans — a span
# is dropped only when EVERY token is a stoplist word, so a real name that
# merely starts with one ("Mark Delgado") is untouched.
_COMMON_WORD_STOPLIST = frozenset(
    {
        # NBHD console app nouns / imperatives
        "goal",
        "rest",
        "open",
        "main",
        "task",
        "plan",
        "note",
        "focus",
        "steady",
        "felt",
        "rename",
        "done",
        "mindful",
        # infra / ops nouns that recur in operator chatter
        "cron",
        "canary",
        "can",
        "staging",
        "prod",
        # weekday abbreviations ("sat" -> name-collision set below)
        "mon",
        "tue",
        "tues",
        "wed",
        "weds",
        "thu",
        "thur",
        "thurs",
        "fri",
        "sun",
        # month abbreviations ("may" excluded — collides with the modal verb)
        "jan",
        "feb",
        "mar",
        "apr",
        "jun",
        "jul",
        "aug",
        "sep",
        "sept",
        "oct",
        "nov",
        "dec",
        # timezone abbreviations
        "jst",
        "utc",
        "gmt",
        "est",
        "edt",
        "cst",
        "cdt",
        "mst",
        "mdt",
        "pst",
        "pdt",
        "cet",
        "cest",
        "bst",
        "ist",
        "aest",
    }
)

# Words that ARE real first names ("Mark task…", "Max effort…", "Sat with…")
# and so are stoplisted ONLY when they open the text (imperative position). Mid-
# sentence they are left alone, and even at the start a following non-stoplist
# token ("Max Verstappen") keeps the span, so genuine names survive.
_NAME_COLLISION_STOPLIST = frozenset(
    {
        "mark",
        "max",
        "sat",
        "don",
        "art",
        "will",
        "grace",
        "joy",
    }
)

# Splits a lowercased span into alphabetic tokens for the fitness / common-word
# guards ("pec deck flys" -> ['pec', 'deck', 'flys']).
_ALPHA_TOKEN_RE = re.compile(r"[^a-z]+")

# ISO date / ISO-week / slash-date spans the model tags as LOCATION (ZIPCODE
# collapses to LOCATION too) — e.g. "2026-W25" @0.99, "2026-06-30". Anchored
# fullmatch so a real address containing digits is never caught.
_DATE_LIKE_RE = re.compile(
    r"""
    ^(
        \d{4}-W\d{1,2}              # ISO week: 2026-W25
      | \d{4}-\d{2}-\d{2}           # ISO date: 2026-06-30
      | \d{4}-\d{2}                 # year-month: 2026-06
      | \d{4}/\d{1,2}/\d{1,2}       # 2026/6/30
      | \d{1,2}/\d{1,2}/\d{2,4}     # 6/30/2026
    )$
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _span_tokens(matched_lower: str) -> list[str]:
    """Alphabetic tokens of a lowercased span (digits/punct are separators)."""
    return [t for t in _ALPHA_TOKEN_RE.split(matched_lower) if t]


def _is_fitness_span(matched_lower: str) -> bool:
    """True when a span is an exercise note, not PII.

    Whole-span exact match against the canonical vocab / phrase sets, plus a
    token-level check so partial or multi-word spans ("glute bridge march",
    'inyasa flow') still suppress. Only tokens ≥3 chars count, to avoid a stray
    two-letter fragment tripping the guard.
    """
    if matched_lower in _FITNESS_VOCAB or matched_lower in _FITNESS_PHRASES:
        return True
    return any(len(t) >= 3 and t in _FITNESS_TOKENS for t in _span_tokens(matched_lower))


def _at_sentence_start(text: str, start: int) -> bool:
    """True when ``start`` is the first token of the text or of a sentence.

    Used to gate the name-collision stoplist: "Mark task…" (imperative) vs
    "…met Mark" (a name). Leading quotes/brackets/dashes before the span are
    treated as still sentence-initial.
    """
    prefix = text[:start].rstrip(" \t\r\n\"'“”‘’([{-—–:")
    return prefix == "" or prefix[-1] in ".!?…"


def _is_common_word_span(matched_lower: str, at_sentence_start: bool) -> bool:
    """True when every token of a span is a stoplisted common word.

    General stoplist words always count; name-collision words count only when
    the span opens the sentence. Requiring ALL tokens to be stoplisted keeps a
    real name that merely begins with a collision word ("Max Verstappen").
    """
    tokens = _span_tokens(matched_lower)
    if not tokens:
        return False
    for token in tokens:
        if token in _COMMON_WORD_STOPLIST:
            continue
        if at_sentence_start and token in _NAME_COLLISION_STOPLIST:
            continue
        return False
    return True


def _is_degenerate_span(text: str) -> bool:
    """True for spans too short or too featureless to be real PII.

    A stripped span under 3 chars, or one with no letter and no digit, is
    almost always a mis-detected fragment (a single letter, ``_``, ``[``,
    ``az``). Dropping these regardless of type is safe: Presidio's checksummed
    matches (credit cards, IBANs) are always ≥ 8 chars, so a real financial
    hit never trips this. This is also why the degenerate prod entity-map rows
    are skipped rather than deleted — the row stays for historical rehydration,
    it just stops driving redaction.
    """
    stripped = (text or "").strip()
    if len(stripped) < 3:
        return True
    return not any(ch.isalpha() or ch.isdigit() for ch in stripped)


def _is_numeric_or_unit_span(text: str) -> bool:
    """True when the stripped span is a bare number or number+unit token."""
    return bool(_NUMERIC_OR_UNIT_RE.match((text or "").strip()))


def _sub_outside_placeholders(text: str, pattern: re.Pattern, replacement: str) -> str:
    """Apply ``pattern.sub(replacement, …)`` only to the parts of ``text`` that
    are NOT already ``[TYPE_N]`` placeholders.

    Step 1's entity substitution runs a regex per stored name over the whole
    working string. Without this guard a stored name containing a capital
    letter or ``_`` (or a degenerate ``[`` / ``_`` row) rewrites the interior
    of a placeholder minted by an earlier iteration, exploding one token into
    nested garbage (``[CRYPTO_ADDRESS_16]'m`` etc.). Splitting on the
    placeholder pattern and substituting only in the gaps extends the
    ``_hit_inside_placeholder`` invariant to Step 1.
    """
    out_parts: list[str] = []
    last = 0
    for m in _PLACEHOLDER_RE.finditer(text):
        out_parts.append(pattern.sub(replacement, text[last : m.start()]))
        out_parts.append(m.group(0))  # placeholder kept verbatim
        last = m.end()
    out_parts.append(pattern.sub(replacement, text[last:]))
    return "".join(out_parts)


def _hit_inside_placeholder(hit: DetectedEntity, ranges: list[tuple[int, int]]) -> bool:
    """True when an NER hit overlaps any existing placeholder range.

    We drop these hits entirely — running token classification over the
    partially-redacted output can flag the internal tokens of a placeholder
    (``EMAIL_ADDRESS_1`` etc.) as PERSON/USERNAME and the replacement loop
    would corrupt the placeholder into nested garbage like ``[[PERSON_2]]``.
    """
    return any(hit.start < ph_end and ph_start < hit.end for ph_start, ph_end in ranges)


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

        # Cross-document state. `entity_map` only carries NEW mints from
        # this session — callers union it onto the tenant map.
        self._type_counters: dict[str, int] = {}
        self.entity_map: dict[str, str] = {}

        # Seed from the tenant's existing map so workspace mints dedup
        # against entities the tenant already knows about. Two effects:
        #  - "Sautai" already in the tenant map gets reused instead of
        #    minted as a fresh [PERSON_N+1] every sync.
        #  - Counter base shifts past existing placeholder numbers, so a
        #    fresh session never clobbers [PERSON_1] with a new entity.
        self._inverted_ci: dict[str, tuple[str, str]] = {}
        self._denylist: dict[str, Any] = {}
        if tenant is not None:
            existing_map = getattr(tenant, "pii_entity_map", None) or {}
            self._inverted_ci = _inverted_names_ci(existing_map)
            for placeholder_key in existing_map:
                match = _PLACEHOLDER_RE.match(placeholder_key)
                if match:
                    etype, num = match.group(1), int(match.group(2))
                    self._type_counters[etype] = max(self._type_counters.get(etype, 0), num)
            # Workspace memory sync also respects the user's denylist so
            # false-positive entities don't get re-minted from documents.
            self._denylist = getattr(tenant, "pii_denylist", None) or {}

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
                inverted_ci=self._inverted_ci,
                denylist=self._denylist,
            )
            return result
        except Exception:
            logger.exception("PII redaction failed — returning original text")
            return text


def rehydrate_text(text: str, entity_map: dict[str, Any]) -> str:
    """Replace PII placeholders with original values.

    Args:
        text: Text potentially containing ``[ENTITY_TYPE_N]`` placeholders.
        entity_map: Mapping from placeholder to entry. Entries may be
            either the legacy string shape (``"Nana"``) or the registry
            dict shape (``{"name": "Nana", "relationship": ...}``);
            both are accepted transparently.

    Returns:
        Text with placeholders replaced by the entry's ``name``.
        Unknown placeholders are left as-is.
    """
    if not text or not entity_map:
        return text

    # Quick check: does the text contain any placeholders at all?
    if "[" not in text:
        return text

    def _replace(match: re.Match) -> str:
        placeholder = match.group(0)
        entry = entity_map.get(placeholder)
        if entry is None:
            return placeholder
        name = _entry_name(entry)
        return name or placeholder

    return _PLACEHOLDER_RE.sub(_replace, text)


def rehydrate_for_tenant(tenant: Tenant | None, text: str) -> str:
    """Rehydrate ``[TYPE_N]`` placeholders to real values for a tenant.

    The single egress seam for the common outbound pattern
    ``if tenant.pii_entity_map: rehydrate_text(text, tenant.pii_entity_map)``.
    EVERY user-facing send path that may carry agent-authored text MUST
    route the text through this (or ``rehydrate_text``) before delivery —
    otherwise a raw ``[PERSON_1]`` placeholder leaks to the user. Safe on a
    None tenant, an empty/absent map, or empty text: returns text unchanged.
    """
    if not text or tenant is None:
        return text
    entity_map = getattr(tenant, "pii_entity_map", None)
    if not entity_map:
        return text
    return rehydrate_text(text, entity_map)


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
    denylist = getattr(tenant, "pii_denylist", None) or {}

    # Step 1: Replace known entities from the existing map (case-insensitive
    # match). ``inverted_ci`` is keyed by ``canonical_key(name)`` so
    # "Sautai", "sautai", and " Sautai " all resolve to the same
    # placeholder. The value tuple carries the display name (for regex
    # building) and the canonical placeholder (lowest-numbered if the
    # map has legacy duplicates from before this fix).
    inverted_ci = _inverted_names_ci(existing_map)
    out = text
    # Longest names first so "Jay Haughton" matches before "Jay".
    for original, placeholder in sorted(
        ((name, ph) for name, ph in inverted_ci.values()),
        key=lambda x: -len(x[0]),
    ):
        if not original:
            # Defensive: re.escape("") == "" and re.sub("", X, text)
            # explodes the text. Never iterate empty originals.
            continue
        if _is_denied(denylist, original):
            # Legacy false-positive entry. The placeholder stays in the
            # map (rehydration of historical refs still works) but it
            # stops driving redaction. This is how the user clears
            # accumulated NER bloat without breaking stored text.
            continue
        if _is_degenerate_span(original):
            # Degenerate stored names (single letters, "az", "_", "[") were
            # mis-minted by NER and match everywhere — including inside the
            # placeholders this loop emits — garbling the whole message. Skip
            # them here (no data migration); the row stays for rehydration.
            continue
        # Word-boundary-aware substitution: a stored 3+ char fragment must
        # never rewrite the interior of a longer word ("don" ⊄ "done",
        # "end" ⊄ "weekend"). A ``\b`` only asserts correctly when adjacent
        # to a word char, so anchor each edge only when the name's edge
        # character is itself alphanumeric/underscore — names with punctuation
        # edges (emails, trailing ".") keep matching against neighbouring
        # punctuation as before.
        esc = re.escape(original)
        left = r"\b" if (original[:1].isalnum() or original[:1] == "_") else ""
        right = r"\b" if (original[-1:].isalnum() or original[-1:] == "_") else ""
        pattern = re.compile(left + esc + right, re.IGNORECASE)
        # Substitute only outside existing placeholders so a stored name that
        # contains a capital letter or ``_`` can never rewrite a placeholder's
        # interior (the Bug A nested-explosion class).
        out = _sub_outside_placeholders(out, pattern, placeholder)

    # Step 2: Run detection on the (partially redacted) text for NEW entities.
    # Per-type counters for newly-minted placeholders are derived later from a
    # row-locked snapshot (see the mint/persist block below), not from the
    # stale ``existing_map`` read at function start — that snapshot can be
    # superseded by a concurrent redaction before we write.
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
    results = _filter_results(results, out, allow_names, denylist=denylist, tenant=tenant)

    # Drop NER hits that fall inside an existing placeholder. Some models
    # (lakshyakh93/deberta_finetuned_pii in particular) classify tokens
    # inside ``[EMAIL_ADDRESS_1]`` as PERSON/USERNAME and the redactor used
    # to corrupt the placeholder into nested garbage like ``[[PERSON_1]]``.
    placeholder_ranges = [(m.start(), m.end()) for m in _PLACEHOLDER_RE.finditer(out)]
    results = [r for r in results if not _hit_inside_placeholder(r, placeholder_ranges)]

    if not results:
        return out

    sorted_results = sorted(results, key=lambda r: r.start)

    # Collect the NER mints that need a fresh placeholder. The actual
    # placeholder numbers are assigned LATER, under a per-tenant row lock, so
    # the counter is derived from a snapshot that no concurrent redaction can
    # mutate between read and write. ``known`` carries spans that already map
    # to an existing placeholder (no minting, no lock needed for those).
    known_replacements: list[tuple[int, int, str]] = []
    to_mint: list[tuple[int, int, str, str, float]] = []  # start, end, etype, original, score
    for result in sorted_results:
        etype = result.entity_type
        original = out[result.start : result.end]

        # Skip if this text is already a placeholder
        if _PLACEHOLDER_RE.match(original):
            continue

        # Case-insensitive lookup against known + newly-minted entries.
        # Step 1's regex pass should have caught most known matches, but
        # NER can still surface spans Step 1 missed (e.g., longest-first
        # ordering edge cases, multi-word vs single-word variants).
        ci_key = _canonical_key(original)
        if ci_key and ci_key in inverted_ci:
            known_replacements.append((result.start, result.end, inverted_ci[ci_key][1]))
            continue

        to_mint.append((result.start, result.end, etype, original, result.score))

    new_map_entries: dict[str, dict[str, Any]] = {}
    replacements: list[tuple[int, int, str]] = list(known_replacements)

    if not to_mint:
        # Nothing new to persist; just rehydrate known placeholders.
        for start, end, placeholder in reversed(replacements):
            out = out[:start] + placeholder + out[end:]
        return out

    # Mint + persist under a per-tenant row lock. The redactor runs from three
    # independent inbound processes (Telegram drain, LINE webhook, iOS chat)
    # plus the arbiter cron and memory_sync. Without a row lock, two concurrent
    # mints derived from the same stale snapshot can mint the same
    # ``[PERSON_N]`` for different people and the second full-dict write
    # clobbers the first — outbound rehydration would then substitute one
    # contact's real name into a reply about another. We re-read under
    # ``select_for_update``, re-derive counters from the locked snapshot, assign
    # the final placeholders there, and write — so the placeholder baked into
    # ``out`` always matches what the map stores.
    from django.db import transaction

    with transaction.atomic():
        locked_map = (
            type(tenant)
            .objects.select_for_update()
            .filter(pk=tenant.pk)
            .values_list("pii_entity_map", flat=True)
            .first()
        ) or {}

        # Re-derive per-type counters from the LOCKED snapshot, not the stale
        # one read at function start.
        locked_counters: dict[str, int] = {}
        for placeholder_key in locked_map:
            match = _PLACEHOLDER_RE.match(placeholder_key)
            if match:
                ckey_etype, num = match.group(1), int(match.group(2))
                locked_counters[ckey_etype] = max(locked_counters.get(ckey_etype, 0), num)

        # Case-insensitive view of the locked map so a name already present
        # collapses onto its existing placeholder instead of minting a dup.
        locked_inverted_ci = _inverted_names_ci(locked_map)

        merged = dict(locked_map)
        for start, end, etype, original, score in to_mint:
            ci_key = _canonical_key(original)
            if ci_key and ci_key in locked_inverted_ci:
                # Concurrent redaction (or this same one earlier) already
                # minted this entity — reuse its placeholder.
                placeholder = locked_inverted_ci[ci_key][1]
                replacements.append((start, end, placeholder))
                # Telemetry — a span NER freshly DETECTED reached the mint path
                # (Step 1's regex and the function-start known-entity check both
                # missed it) and collapsed onto an EXISTING placeholder instead
                # of minting. This is the silent, permanent same-name fusion we
                # want measurable: two different people who share a name land on
                # one placeholder here and can never be separated again. Same
                # PCI/PII discipline as pii_mint below — NEVER log the raw span
                # (these logs ship to Azure Log Analytics in cleartext); tenant
                # id, type, placeholder, score and a coarse span length only.
                # ``source`` is the one split we can tell apart cheaply:
                # ``same_message`` — minted earlier in THIS call (same new name
                # twice in one message; benign), vs ``concurrent`` — from the
                # row-locked DB snapshot (another redaction minted it during our
                # read→lock window, or our in-memory map was stale). A name that
                # genuinely pre-existed in the map is caught by the earlier
                # known-entity check and never reaches this branch.
                logger.info(
                    "pii_reuse tenant=%s type=%s placeholder=%s score=%.3f span_len=%d source=%s",
                    getattr(tenant, "id", "?"),
                    etype,
                    placeholder,
                    score,
                    len(original),
                    "same_message" if placeholder in new_map_entries else "concurrent",
                )
                continue

            count = locked_counters.get(etype, 0) + 1
            locked_counters[etype] = count
            placeholder = f"[{etype}_{count}]"
            replacements.append((start, end, placeholder))
            entry = _entry_storage(original)
            new_map_entries[placeholder] = entry
            merged[placeholder] = entry
            if ci_key:
                locked_inverted_ci[ci_key] = (original, placeholder)
            # Telemetry — capture score on every mint so future threshold
            # tuning can be data-driven instead of vibes-driven. NEVER log the
            # raw span: this is the PII redactor, and its logs ship to Azure
            # Log Analytics in cleartext — emitting the detected value (card
            # numbers, passwords, IBANs, emails) would defeat the module's
            # whole purpose and is a PCI-DSS violation. tenant id, type and
            # score are sufficient for tuning; log only the span length as a
            # coarse, non-reversible shape signal.
            logger.info(
                "pii_mint tenant=%s type=%s placeholder=%s score=%.3f span_len=%d",
                getattr(tenant, "id", "?"),
                etype,
                placeholder,
                score,
                len(original),
            )

        if new_map_entries:
            type(tenant).objects.filter(pk=tenant.pk).update(pii_entity_map=merged)

    # Update in-memory too
    if new_map_entries:
        tenant.pii_entity_map = merged

    # Apply replacements (after the lock — string slicing needs no DB). Numbers
    # baked here match the persisted map because they were assigned under lock.
    for start, end, placeholder in reversed(replacements):
        out = out[:start] + placeholder + out[end:]

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

    # 1. DeBERTa model — contextual PII (best effort).
    # If the model failed to load (ABI mismatch, missing weights), the
    # engine raises the cached load error. We swallow it here without
    # logging — the engine logs once at error level on first failure.
    # Pattern recognizers below still run, so financial PII stays redacted.
    try:
        pii_pipeline = get_pii_pipeline()
        model_results = pii_pipeline(text)
    except Exception:
        model_results = []

    for ent in model_results:
        raw_label = ent["entity_group"]
        # A detection must clear both the tier threshold and any per-label
        # override (checked against the RAW label, before it is collapsed by
        # DEBERTA_LABEL_MAP — e.g. PIN requires ≥0.7 while everything else
        # rides the 0.5 tier threshold).
        effective_threshold = max(score_threshold, LABEL_SCORE_OVERRIDES.get(raw_label, 0.0))
        if ent["score"] < effective_threshold:
            continue
        entity_type = DEBERTA_LABEL_MAP.get(raw_label)
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
    inverted_ci: dict[str, tuple[str, str]] | None = None,
    denylist: dict[str, Any] | None = None,
) -> tuple[str, dict[str, str]]:
    """Run PII detection and replace with numbered placeholders.

    Mutates ``type_counters``, ``entity_map``, and (if provided)
    ``inverted_ci`` in place for cross-document sessions.

    When ``inverted_ci`` is provided (workspace memory sync path),
    detected spans whose canonical key is already known reuse the
    existing placeholder instead of minting a new one. New mints get
    registered back so subsequent calls in the same session dedup.

    When ``inverted_ci`` is ``None`` (one-off ``redact_text`` callers),
    behaviour matches the pre-fix mint-everything path.

    ``denylist`` (when non-empty) suppresses detection of spans whose
    canonical key the tenant has marked as "not PII for me". See
    ``entity_registry.is_denied``.

    Returns ``(redacted_text, entity_map)``.
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
    results = _filter_results(results, text, allow_names, denylist=denylist, tenant=tenant)

    if not results:
        return text, entity_map

    # Sort by position for consistent numbering
    sorted_results = sorted(results, key=lambda r: r.start)

    # Assign numbered placeholders per entity type
    replacements: list[tuple[int, int, str]] = []
    for result in sorted_results:
        etype = result.entity_type
        original = text[result.start : result.end]

        # Reuse a known placeholder if the session was seeded with one
        # for this entity (case-insensitive). Skips minting entirely.
        if inverted_ci is not None:
            ci_key = _canonical_key(original)
            if ci_key and ci_key in inverted_ci:
                replacements.append((result.start, result.end, inverted_ci[ci_key][1]))
                continue

        count = type_counters.get(etype, 0) + 1
        type_counters[etype] = count
        placeholder = f"[{etype}_{count}]"
        replacements.append((result.start, result.end, placeholder))
        entity_map[placeholder] = original
        # Register the mint for in-session dedup so a second mention of
        # the same name in a later document collapses onto this one.
        if inverted_ci is not None:
            ci_key = _canonical_key(original)
            if ci_key:
                inverted_ci[ci_key] = (original, placeholder)

    # Apply replacements from end to start to preserve character positions
    out = text
    for start, end, placeholder in reversed(replacements):
        out = out[:start] + placeholder + out[end:]

    return out, entity_map


def _log_skip(tenant: object | None, result: DetectedEntity, reason: str, span_len: int) -> None:
    """Debug telemetry when a mint-time guard suppresses a detection.

    Mirrors the ``pii_mint`` line's shape (tenant, type, score, span_len) and
    the same rule: NEVER log the span text — these logs ship to Azure Log
    Analytics in cleartext and the whole point of this module is to keep PII
    out of them. span_len is a coarse, non-reversible shape signal.
    """
    logger.debug(
        "pii_skip tenant=%s type=%s reason=%s score=%.3f span_len=%d",
        getattr(tenant, "id", "?"),
        result.entity_type,
        reason,
        result.score,
        span_len,
    )


def _filter_results(
    results: list,
    text: str,
    allow_names: set[str],
    *,
    denylist: dict[str, Any] | None = None,
    tenant: object | None = None,
) -> list:
    """Remove false positives and deduplicate overlapping spans.

    The optional ``denylist`` is the tenant's ``pii_denylist`` JSON
    field — canonical-keyed strings the user has marked as "not PII
    for me". Detections whose canonical key is denylisted are dropped
    regardless of entity type, so the same denylist entry suppresses
    both PERSON and LOCATION false positives without the user having
    to think about which type the model assigned.

    Also applies the mint-time guards (degenerate span, bare
    number/unit, fitness vocabulary) that keep NER false positives from
    minting a placeholder. ``tenant`` is used only for skip telemetry.
    """
    filtered = []
    for result in results:
        matched_text = text[result.start : result.end].strip()
        matched_lower = matched_text.lower()

        # Skip allowed names (user's own name)
        if result.entity_type == "PERSON" and any(
            matched_lower == name.lower() or matched_text == name for name in allow_names
        ):
            continue

        # Skip tenant-denylisted spans (manually flagged as non-PII).
        if _is_denied(denylist, matched_text):
            continue

        # Degenerate spans (single letters, punctuation) are never real PII,
        # regardless of the label the model assigned.
        if _is_degenerate_span(matched_text):
            _log_skip(tenant, result, "degenerate", len(matched_text))
            continue

        # Numeric and fitness-vocabulary guards apply ONLY to the loosely
        # typed contextual labels. PHONE_NUMBER/CREDIT_CARD/IBAN/EMAIL come
        # from checksum/pattern recognizers and legitimately contain digits or
        # short tokens, so we must not soften them here.
        if result.entity_type in ("LOCATION", "PERSON"):
            if _is_numeric_or_unit_span(matched_text):
                _log_skip(tenant, result, "numeric", len(matched_text))
                continue
            if _is_fitness_span(matched_lower):
                _log_skip(tenant, result, "fitness_vocab", len(matched_text))
                continue
            if _is_common_word_span(matched_lower, _at_sentence_start(text, result.start)):
                _log_skip(tenant, result, "common_word", len(matched_text))
                continue
            if result.entity_type == "LOCATION" and _DATE_LIKE_RE.match(matched_text):
                _log_skip(tenant, result, "date_like", len(matched_text))
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
