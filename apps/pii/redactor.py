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
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from apps.fuel import catalog as fuel_catalog
from apps.pii.config import ADDRESS_CONTEXT_LABELS, DEBERTA_LABEL_MAP, LABEL_SCORE_OVERRIDES, TIER_POLICIES
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
    from apps.pii.provisional import PiiIngress
    from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

# Data-only dependency: PII imports the pure ``apps.fuel.catalog`` module so
# public exercise vocabulary follows the shipped iOS picture catalog. No Fuel
# models, Django runtime views, or exercise names from tenant data are imported.
_ALPHA_TOKEN_RE = re.compile(r"[^a-z]+")


def _span_tokens(matched_lower: str) -> list[str]:
    """Alphabetic tokens of a lowercased span (digits/punct are separators)."""
    return [token for token in _ALPHA_TOKEN_RE.split(matched_lower) if token]


@dataclass(frozen=True)
class RedactionOutcome:
    """Text plus an engine-issued confirmation that redaction completed."""

    text: str
    confirmed: bool
    reason: str


class NeuralDetectorUnavailable(Exception):
    """The neural detector did not complete, although deterministic checks may have."""


_neural_detector_outcome = threading.local()


def _reset_neural_detector_outcome() -> None:
    _neural_detector_outcome.available = None


def _neural_detector_available() -> bool | None:
    return getattr(_neural_detector_outcome, "available", None)


_CONFIRMED_REDACTION_TOKEN = object()


@dataclass(frozen=True)
class ConfirmedRedaction:
    """Placeholder-space text carrying module-issued provenance.

    The private token is intentionally identity-checked at persistence seams.
    A caller cannot obtain a valid value by setting a boolean or by constructing
    this dataclass with an arbitrary token.
    """

    text: str
    reason: str
    _provenance: object = field(repr=False, compare=False)


def _mint_confirmed(text: str, reason: str) -> ConfirmedRedaction:
    """Mint a confirmation inside an engine-controlled gate."""
    return ConfirmedRedaction(
        text=text,
        reason=reason,
        _provenance=_CONFIRMED_REDACTION_TOKEN,
    )


def as_confirmed(outcome: RedactionOutcome) -> ConfirmedRedaction | None:
    """Re-enter the provenance-bearing path from an engine-written receipt."""
    if not isinstance(outcome, RedactionOutcome) or outcome.confirmed is not True:
        return None
    return _mint_confirmed(outcome.text, outcome.reason)


def confirm_assistant_output(tenant: Tenant, text: str) -> ConfirmedRedaction | None:
    """Scrub known entity values from model output, then confirm it for capture.

    Assistant text is authored in placeholder space, but model output can still
    echo a mapped real value. The existing deterministic known-value scrub is
    called directly so an internal failure refuses to mint instead of taking its
    public fail-open compatibility path.
    """
    try:
        from apps.pii.egress import _redact_known_values

        scrubbed = _redact_known_values(tenant, text)
    except Exception:
        logger.exception(
            "Assistant output confirmation failed tenant=%s",
            getattr(tenant, "id", "?"),
        )
        return None
    return _mint_confirmed(scrubbed, "assistant-output-confirmed")


def redaction_receipt(payload: dict | None) -> RedactionOutcome:
    """Read a queue/buffer receipt with rolling-deploy-safe defaults.

    The receipt deliberately does not duplicate text inside JSON; callers pair
    this outcome with the row's ``user_text``. Legacy or malformed rows can
    never be mistaken for positively confirmed placeholder-space text.
    """
    if not isinstance(payload, dict) or "redaction" not in payload:
        return RedactionOutcome(text="", confirmed=False, reason="pre-receipt-row")

    receipt = payload.get("redaction")
    if not isinstance(receipt, dict):
        return RedactionOutcome(text="", confirmed=False, reason="invalid-receipt")

    reason = receipt.get("reason")
    if not isinstance(reason, str) or not reason:
        reason = "invalid-receipt"
    return RedactionOutcome(
        text="",
        confirmed=receipt.get("confirmed") is True,
        reason=reason,
    )


def confirmed_from_receipt_row(
    payload: dict | None,
    stored_text: str,
) -> ConfirmedRedaction | None:
    """Mint confirmed row text only when its colocated receipt confirms it.

    ``stored_text`` MUST be the text column written in the same transaction as
    ``payload``'s receipt: ``PendingMessage.user_text`` or
    ``BufferedMessage.user_text``. Keeping the pairing here prevents callers
    from attaching a confirmed receipt to text from another source.
    """
    receipt = redaction_receipt(payload)
    if receipt.confirmed is not True:
        return None
    return _mint_confirmed(stored_text, receipt.reason)


# Matches bare and model-context-annotated placeholders, for example
# ``[PERSON_1]`` and ``[PERSON_1|coworker at ORG_2]``.  The annotation is
# deliberately part of the token so every placeholder-aware seam can treat the
# two wire forms identically.
_PLACEHOLDER_RE = re.compile(r"\[([A-Z_]+)_(\d+)(?:\|[^\]]*)?\]")

# Rehydration variant that also accepts markdown-escaped placeholders.
# The assistant sometimes writes ``\[PERSON_444\]`` in journal markdown so
# the brackets render literally; rehydration must translate that form too
# or the escaped placeholder leaks to the owner verbatim (observed in prod
# daily notes). Kept separate from ``_PLACEHOLDER_RE`` because detection /
# metadata paths operate on redactor-emitted text, which is never escaped.
_REHYDRATE_PLACEHOLDER_RE = re.compile(r"\\?\[([A-Z_]+)_(\d+)(?:\|[^\]]*)?\\?\]")

_RELATIONSHIP_ENTITY_TYPES = frozenset({"PERSON", "ORG", "PLACE", "LOCATION"})
_MAX_PLACEHOLDER_ANNOTATION_CHARS = 80

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

# A BUILDINGNUMBER span that is really a measurement: bare number with optional
# thousands separators / decimal part and an optional trailing unit (same
# whitelist as _NUMERIC_OR_UNIT_RE). Anchored fullmatch against the stripped
# span. Integer part deliberately capped at 4 leading digits so a 5-digit ZIP
# mislabeled BUILDINGNUMBER can never match; alphanumeric house numbers
# ("221B", "82-4") don't match either — both keep flowing to redaction.
_BARE_MEASUREMENT_RE = re.compile(
    r"""
    ^
    \d{1,4}(?:,\d{3})*                  # 82 · 180 · 1,050
    (?:[.,]\d+)?                        # 82.5 · 82,5 (intl decimal comma)
    (?:\s?(?:kg|kgs|lb|lbs|reps?|sets?|km|mi|min|sec|hrs?|bpm|cal|kcal))?  # 82kg · 180 lbs
    $
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
        # Catalog-safe anatomy/movement tokens. Deliberately excludes surnames
        # and places such as arnold, pendlay, copenhagen, and meadows.
        "tricep",
        "triceps",
        "bicep",
        "biceps",
        "calf",
        "calves",
        "pushdown",
        "pushdowns",
        "lateral",
    }
)

# Multi-word exercise phrases matched exactly (lowercased span). Kept separate
# from ``_FITNESS_TOKENS`` for phrases whose only distinctive token is unsafe to
# add bare — e.g. "glute bridge march" (the 'march' token collides with the
# month, so we match the whole phrase instead of adding 'march').
_CATALOG_FITNESS_PHRASES = frozenset(
    " ".join(tokens) for raw in fuel_catalog.fitness_phrases() if len(tokens := _span_tokens(raw.casefold())) >= 2
)
_FITNESS_PHRASES = (
    frozenset(
        {
            "glute bridge march",
            "glute bridge marches",
            # Required whole-span protections absent from catalog v2. They stay
            # phrase-only because both distinctive tokens are real surnames.
            "zercher squat",
            "kroc row",
        }
    )
    | _CATALOG_FITNESS_PHRASES
)

# Safe as exact bare-token retire targets. This is narrower than
# ``_FITNESS_TOKENS`` so adding a partial-span suppression never silently makes
# an old binding destructively eligible for retirement.
_RETIRABLE_FITNESS_TOKENS = frozenset(
    {
        "tricep",
        "triceps",
        "bicep",
        "biceps",
        "calf",
        "calves",
        "pushdown",
        "pushdowns",
        "lateral",
    }
)
_FITNESS_SURNAME_TOKENS = frozenset(
    {
        "arnold",
        "farmer",
        "jefferson",
        "hindu",
        "copenhagen",
        "cossack",
        "hack",
        "meadows",
        "pendlay",
        "zercher",
        "kroc",
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

# ---------------------------------------------------------------------------
# Fleet evidence (cross-tenant ignore analysis, 2026-08-08): words that two or
# more tenants explicitly marked "not PII" and that are implausible as a
# personal name. Name-SHAPED denies from the same data (max, mar, theo, la,
# moon, spark, claude) are deliberately absent — under-redacting a real name is
# the failure mode here, and so are the surname-shaped words (quick, daily,
# morning, evening, breezy), which moved to _FLEET_PHRASE_STOPLIST below.
#
# Kept SEPARATE from _COMMON_WORD_STOPLIST on purpose: the sentence-start
# name-collision escape in _is_common_word_span may combine with the legacy
# console vocabulary ("Mark task" is an imperative) but must NEVER combine with
# this set — "Mark Calendar" is a person, not a template phrase.
# ---------------------------------------------------------------------------
_FLEET_WORD_STOPLIST = frozenset(
    {
        # Assistant/app template vocabulary minted as PERSON (830 live
        # "calendar" bindings fleet-wide, 345 "quick wins").
        "nbhd",
        "calendar",
        "wins",
        "briefing",
        "briefings",
        "weekly",
        "status",
        "schedule",
        "lesson",
        "lessons",
        "email",
        "background",
        "complete",
        "running",
        "await",
        "project",
        "setup",
        "weather",
        "push",
        "pull",
        "heartbeat",
        "check",
        "checkin",
        # Glue token so hyphenated template phrases collapse under the
        # all-tokens rule ("heartbeat check-in" tokenizes to
        # ['heartbeat', 'check', 'in']). Harmless on its own: a bare "in"
        # is already dropped by the degenerate-span floor.
        "in",
        # Brands / products the model labels PERSON or LOCATION.
        "gmail",
        "google",
        "youtube",
        "telegram",
        "fedex",
        "nvidia",
        "overcast",
        "totemo",
        "playoff",
        "drizzle",
        # Interjection / food / group nouns from the canary incident set.
        "hmm",
        "gyoza",
        "houthis",
        # Markdown/list scaffolding words that ride along in template spans.
        "reply",
        "section",
    }
)

# Multi-word template phrases matched as a WHOLE span, mirroring
# ``_FITNESS_PHRASES``. These exist because their distinctive token is
# surname-shaped and unsafe to stoplist bare: "Quick", "Daily", "Morning",
# "Evening" and "Breezy" are all real surnames, so a lone span carrying one must
# keep redacting while the template phrase it usually appears in does not.
#
# Matching is on the span's ``_span_tokens`` joined by single spaces, not on the
# raw span, so punctuation and markdown noise still hit: "Quick Wins\n-" and
# "evening check-in" both normalize onto an entry here. Entries must therefore
# be written in that normalized form (see "evening check in" below).
_FLEET_PHRASE_STOPLIST = frozenset(
    {
        "quick wins",
        "morning briefing",
        "morning briefings",
        "daily briefing",
        "evening check in",  # normalized form of "evening check-in"
    }
)

# Nationality adjectives the model tags PERSON or LOCATION when a user writes
# about language practice, food, or news ("I practiced my Japanese today"). A
# demonym is never a personal name, so these are safe fleet-wide. Kept in their
# own constant rather than folded into the common-word set so the demonym policy
# stays auditable and extendable on its own; it is consumed by the SAME
# all-tokens-must-match rule, so "Japanese Yamamoto" still redacts.
#
# Deliberately absent: german, french, english, dutch, israel — each is also a
# surname or given name in common use.
_DEMONYM_STOPLIST = frozenset(
    {
        "japanese",
        "american",
        "chinese",
        "korean",
        "italian",
        "spanish",
        "canadian",
        "australian",
        "indian",
        "russian",
        "mexican",
        "brazilian",
        "romanian",
        "bulgarian",
        "nordic",
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


# Birth-context cues that promote a bare DATE span to DATE_OF_BIRTH. The model's
# raw DATE label is dropped fleet-wide (it fires on every yyyy-mm-dd journal
# heading — see config.py), so a date only redacts when one of these sits beside
# it: a disclosed birth date is identifying PII, an ordinary calendar date is not.
_BIRTH_CONTEXT_RE = re.compile(
    r"""
        date\s+of\s+birth        # date of birth
      | birth\s?date             # birthdate / birth date
      | \bd\.?o\.?b\b            # DOB / D.O.B.
      | \bborn\b                 # born (on) …
      | birthday                 # birthday
      | 生年月日                  # JP: date of birth
      | 誕生日                    # JP: birthday
      | 生まれ                    # JP: born
    """,
    re.IGNORECASE | re.VERBOSE,
)

# How far on each side of a DATE span to scan for a birth-context cue. Birth
# phrasing overwhelmingly PRECEDES the date ("date of birth is …", "生年月日は…"),
# so the lookbehind window is generous and the lookahead deliberately small.
_BIRTH_CONTEXT_BEFORE = 48
_BIRTH_CONTEXT_AFTER = 16


def _has_birth_context(text: str, start: int, end: int) -> bool:
    """True when a birth-context cue sits within the window around a DATE span."""
    lo = max(0, start - _BIRTH_CONTEXT_BEFORE)
    hi = min(len(text), end + _BIRTH_CONTEXT_AFTER)
    return bool(_BIRTH_CONTEXT_RE.search(text[lo:hi]))


def _is_fitness_span(matched_lower: str, *, full_text: str = "") -> bool:
    """True when a span is an exercise note, not PII.

    Whole-span exact match against the canonical vocab / phrase sets, plus a
    token-level check so partial or multi-word spans ("glute bridge march",
    'inyasa flow') still suppress. Only tokens ≥3 chars count, to avoid a stray
    two-letter fragment tripping the guard.
    """
    matched_phrase = " ".join(_span_tokens(matched_lower))
    leaf_phrase = " ".join(_span_tokens(full_text.casefold())) if full_text else ""
    if matched_lower in _FITNESS_VOCAB or matched_phrase in _FITNESS_PHRASES:
        return True
    # The detector may return only the surname-shaped token from an otherwise
    # exact exercise leaf ("Arnold" from "Arnold press"). The recursive store
    # authors each detail_json leaf alone, so exact whole-leaf membership is the
    # narrow contextual guard: prose such as "Met Arnold for lunch" is not a
    # catalog phrase and remains PII.
    if leaf_phrase in _FITNESS_PHRASES:
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


# Tokens that stay stoplisted for DETECTION but must never drive a destructive
# retire. Each is a real given/family name somewhere in the tenant base — Jan,
# Jun, Mar (María del Mar), Sun (孫), Can (Turkish), Thu (Vietnamese, extremely
# common), Mon, Main, Jul, Sep — that only earned its stoplist slot as a
# month/weekday abbreviation or an ops noun. Dropping a fresh detection for one
# of these is recoverable — the user re-adds the contact. Retiring the binding
# is not: it silently removes protection a user may have created by hand, so
# ``is_never_a_name`` (the backfill predicate) refuses them.
_RETIRE_EXEMPT_TOKENS = frozenset({"jan", "jun", "mar", "sun", "can", "thu", "mon", "main", "jul", "sep"})


def _is_fleet_stoplisted_token(token: str) -> bool:
    """True for a token that is never a personal name on ANY tenant.

    Single source of truth for the fleet stoplists (legacy console vocabulary +
    the fleet-evidence set + demonyms). The name-collision set is NOT consulted
    here: those words ARE real names, suppressed only positionally by
    :func:`_is_common_word_span`.
    """
    return token in _COMMON_WORD_STOPLIST or token in _FLEET_WORD_STOPLIST or token in _DEMONYM_STOPLIST


def _is_fleet_stoplisted_span(tokens: list[str]) -> bool:
    """True when a token list is fleet junk: a known template phrase, or every
    token stoplisted. The one rule shared by the detection filter and the retire
    backfill, so the two can never drift.
    """
    if not tokens:
        return False
    if " ".join(tokens) in _FLEET_PHRASE_STOPLIST:
        return True
    return all(_is_fleet_stoplisted_token(token) for token in tokens)


def is_never_a_name(text: str) -> bool:
    """True when ``text`` is fleet junk: a template phrase, or all-stoplisted.

    The public form of the rule, shared by the detection filter and the
    ``retire_stoplisted_bindings`` backfill so the "what counts as junk"
    decision can never drift between what we stop minting and what we retire.

    Three properties the callers depend on: a span with any non-stoplisted token
    is False ("Quick Delgado" is a name); a span with no a-z tokens at all is
    False — which exempts CJK names, whose identifying signal the Latin tokenizer
    cannot see; and :data:`_RETIRE_EXEMPT_TOKENS` is False, so the destructive
    caller is strictly narrower than the detection filter.
    """
    tokens = _span_tokens(text.casefold())
    if not tokens:
        return False
    if any(token in _RETIRE_EXEMPT_TOKENS for token in tokens):
        return False
    # Whole catalog phrases are safe to retire, but never a bare surname/place
    # token even if that token happens to be a catalog alias (e.g. "Pendlay").
    if len(tokens) == 1 and tokens[0] in _FITNESS_SURNAME_TOKENS:
        return False
    joined = " ".join(tokens)
    if len(tokens) >= 2 and joined in _FITNESS_PHRASES:
        return True
    if len(tokens) == 1 and tokens[0] in _RETIRABLE_FITNESS_TOKENS:
        return True
    return _is_fleet_stoplisted_span(tokens)


def _is_common_word_span(matched_lower: str, at_sentence_start: bool) -> bool:
    """True when a span is console/template vocabulary rather than a name.

    Two independent ways to qualify:

    - Fleet junk (:func:`_is_fleet_stoplisted_span`) — a known template phrase,
      or every token stoplisted. Position-independent.
    - The sentence-start escape for :data:`_NAME_COLLISION_STOPLIST` words,
      which ARE real names and so only suppress in imperative position ("Mark
      task…" vs "…met Mark").

    The escape deliberately combines ONLY with the legacy console vocabulary. A
    span mixing a collision word with the fleet-evidence or demonym sets is a
    PERSON — "Mark Calendar" and "Grace Google" are people, so they survive
    while "Mark task" (both legacy) still drops.
    """
    tokens = _span_tokens(matched_lower)
    if not tokens:
        return False
    if _is_fleet_stoplisted_span(tokens):
        return True
    if not at_sentence_start:
        return False
    return all(token in _COMMON_WORD_STOPLIST or token in _NAME_COLLISION_STOPLIST for token in tokens)


def _is_degenerate_span(text: str) -> bool:
    """True for spans too short or too featureless to be real PII.

    A stripped span under 3 chars, or one with no letter and no digit, is
    almost always a mis-detected fragment (a single letter, ``_``, ``[``,
    ``az``). Dropping these regardless of type is safe: Presidio's checksummed
    matches (credit cards, IBANs) are always ≥ 8 chars, so a real financial
    hit never trips this. This is also why the degenerate prod entity-map rows
    are skipped rather than deleted — the row stays for historical rehydration,
    it just stops driving redaction.

    The 3-char floor is Latin-calibrated. CJK names pack far more identifying
    signal per character: a bare 2-character span is a complete, common Japanese/
    Chinese family name (田中, 佐藤), so any span carrying a Han/kana character is
    exempt from the length floor. Without this, once ``snap_to_word_boundaries``
    stops over-expanding a 2-kanji name, that name would flip from over-redacted
    straight to LEAKED in cleartext (dropped here as "degenerate").
    """
    stripped = (text or "").strip()
    if not stripped:
        return True
    try:
        from apps.pii.hygiene import contains_cjk
    except Exception:
        contains_cjk = None
    if contains_cjk is not None and contains_cjk(stripped):
        return False
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


def _seed_counters_from_map(entity_map: dict) -> dict[str, int]:
    """Derive ``{TYPE: max suffix}`` from the placeholder keys of ``entity_map``.

    Only the max-per-type is needed — a fresh mint takes ``+ 1`` of it. Malformed
    keys are ignored (they never index a numbered placeholder).
    """
    counters: dict[str, int] = {}
    for placeholder_key in entity_map:
        match = _PLACEHOLDER_RE.match(placeholder_key)
        if match:
            etype, num = match.group(1), int(match.group(2))
            counters[etype] = max(counters.get(etype, 0), num)
    return counters


def _apply_stored_high_water(counters: dict[str, int], stored_counters: dict[str, Any] | None) -> None:
    """Raise each per-type counter to the tenant's stored monotonic high-water.

    ``counters`` is the max suffix re-derived from the CURRENT ``pii_entity_map``
    (via :func:`_seed_counters_from_map`); ``stored_counters`` is
    ``Tenant.pii_type_counters`` — the highest suffix EVER minted per type, which
    never drops on deletion. Seeding every mint from the max of the two is the
    whole fix: even after a binding is deleted (lowering the map-derived max), the
    next mint still allocates ABOVE the stored high-water, so a freed number can
    never be recycled onto a different value. Mutates ``counters`` in place. Non-
    integer stored values are skipped defensively (a hand-edited row must not
    crash the redactor). A missing/empty ``stored_counters`` is a legacy pre-
    migration tenant — numbering falls back to the map maxima, unchanged.
    """
    if not stored_counters:
        return
    for etype, high in stored_counters.items():
        try:
            high_int = int(high)
        except (TypeError, ValueError):
            continue
        counters[etype] = max(counters.get(etype, 0), high_int)


def next_placeholder_number(
    etype: str,
    entity_map: dict[str, Any] | None,
    stored_counters: dict[str, Any] | None,
) -> int:
    """Return the next monotonic placeholder number to mint for ``etype``.

    Public wrapper over the detector's own numbering so manual entity-registry
    adds (``EntityRegistryListView.post``) and detector mints allocate from the
    SAME high-water source and can never diverge onto a recycled number. It
    combines the max suffix re-derived from ``entity_map``
    (:func:`_seed_counters_from_map`) with the tenant's stored monotonic
    high-water ``pii_type_counters`` (:func:`_apply_stored_high_water`) and
    returns ``max(the two) + 1``.

    Caller contract (identical to the mint loop): hold the tenant row lock, read
    ``pii_entity_map`` + ``pii_type_counters`` from the LOCKED snapshot, call
    this, then persist the returned number back into ``pii_type_counters`` in the
    same ``update()`` — so a freed number is never reissued to a different value.
    """
    counters = _seed_counters_from_map(entity_map or {})
    _apply_stored_high_water(counters, stored_counters)
    return counters.get(etype, 0) + 1


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


# ---------------------------------------------------------------------------
# Mint policy — WHO is allowed to coin NEW placeholders
# ---------------------------------------------------------------------------
#
# The prod audit found 979/1103 bindings were junk, and the source was never
# human chat: it was MACHINE text — agent-authored workspace markdown (table
# separators, headings, timestamps NER labels PERSON/ACCOUNT) and raw tool
# payloads (newsletter senders, email bodies, financial labels with no
# validation). So minting is now gated by WHERE the text came from. Replacing
# entities the tenant map ALREADY knows is allowed under every policy — the gate
# only decides whether unfamiliar text is permitted to create a NEW binding, so
# privacy for known people is preserved everywhere.
MINT_ALL = "all"  # human-typed chat ingress — mint everything (legacy behavior)
MINT_VALIDATED = "validated"  # tool responses — mint only validator-approved types
MINT_NEVER = "never"  # agent-authored markdown (memory sync, co-pilot) — mint nothing
_MINT_POLICIES = frozenset({MINT_ALL, MINT_VALIDATED, MINT_NEVER})


def _structured_validator():
    """Return ``apps.pii.hygiene.validate_structured`` or ``None`` if unavailable.

    Imported lazily (repo convention for cross-app deps) so redactor import does
    not hard-order against the hygiene module landing, and so the validated-mint
    gate can fail CLOSED — mint nothing — rather than crash if hygiene is absent.
    Signature: ``validate_structured(entity_type: str, text: str) -> bool``.
    """
    try:
        from apps.pii.hygiene import validate_structured
    except Exception:
        return None
    return validate_structured


# Email-shape floor used ONLY until apps.pii.hygiene lands (see below). A single
# @ with a dotted domain, no whitespace — deliberately loose: it just needs to
# tell a real address apart from a neural mislabel, not fully RFC-validate.
_EMAIL_SHAPE_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _fallback_structured_valid(entity_type: str, text: str) -> bool:
    """Conservative built-in mint floor used ONLY when ``apps.pii.hygiene`` is
    absent (merge-order safety), fully superseded once hygiene lands.

    Vouches for email-shaped ``EMAIL_ADDRESS`` spans so inbound-correspondent
    protection never regresses — the task requires emails keep minting from tool
    text. Checksummed types (cards, IBANs) are deliberately NOT vouched here:
    they need real Luhn/checksum validation that is hygiene's job, and the audit
    showed NEURAL card labels ("django" tagged CREDIT_CARD) are junk, so a
    type-only floor would mint junk. Neural PERSON/LOCATION never pass.
    """
    if entity_type == "EMAIL_ADDRESS":
        return bool(_EMAIL_SHAPE_RE.match((text or "").strip()))
    return False


def _should_mint_new(mint: str, entity_type: str, text: str) -> bool:
    """Whether a NEWLY-DETECTED span (one the tenant map does not already know)
    may coin a fresh placeholder under ``mint``.

    Known-entity replacement runs BEFORE this gate on every path, so a ``False``
    here never un-protects a known contact — it only decides whether unfamiliar
    text mints a new binding. ``validated`` defers the per-type judgment to
    ``hygiene.validate_structured`` (email-shaped addresses, Luhn cards, IBAN
    checksums pass; neural PERSON/LOCATION do not). Until hygiene is importable it
    falls back to a conservative email-only floor (``_fallback_structured_valid``)
    so email protection never regresses, while the junk-prone classes still
    fail closed.
    """
    if mint == MINT_ALL:
        return True
    if mint == MINT_NEVER:
        return False
    validate = _structured_validator()
    if validate is None:
        return _fallback_structured_valid(entity_type, text)
    try:
        return bool(validate(entity_type, text))
    except Exception:
        return False


def _known_name_edge_anchor(edge_char: str) -> str:
    r"""Return a ``\b`` word-boundary anchor for a stored name's edge — EXCEPT for
    CJK edges, where it must be omitted.

    ``\b`` only asserts a boundary between a word char and a non-word char. In
    unspaced Japanese ("田中です") both "中" and "で" are word chars, so a trailing
    ``\b`` never matches and a stored CJK name silently fails to re-mask across
    turns (its cross-turn protection would depend on the model re-detecting it
    every message). For a CJK edge we drop the anchor — as we already do for
    punctuation edges — mirroring the CJK-aware word snap. Non-CJK alnum/``_``
    edges keep ``\b`` so a stored Latin fragment can't rewrite a longer word.
    """
    if not edge_char or not (edge_char.isalnum() or edge_char == "_"):
        return ""
    from apps.pii.hygiene import contains_cjk

    return "" if contains_cjk(edge_char) else r"\b"


def known_name_pattern(original: str) -> re.Pattern:
    """Compile the exact conditional-boundary matcher used for substitution."""
    esc = re.escape(original)
    left = _known_name_edge_anchor(original[:1])
    right = _known_name_edge_anchor(original[-1:])
    return re.compile(left + esc + right, re.IGNORECASE)


def known_value_matches(text: str, original: str) -> bool:
    """Whether raw text contains a value exactly where substitution would."""
    if not text or not original or _is_degenerate_span(original):
        return False
    return bool(known_name_pattern(original).search(text))


def _replace_known_only(
    text: str,
    inverted_ci: dict[str, tuple[str, str]],
    denylist: dict[str, Any] | None,
) -> str:
    """Substitute ONLY names the tenant map already knows — mint nothing.

    The ``mint='never'`` path (workspace memory-sync + galaxy co-pilot). Both feed
    the redactor AGENT-authored markdown, the audit's dominant junk-mint source,
    so we skip the detector entirely and coin nothing: known people stay masked via
    a literal, case-insensitive, word-boundary pass, and machine structure
    (headings, table rules, timestamps) is left verbatim. The guards mirror the
    inbound Step 1 known-entity pass exactly — denylisted and degenerate stored
    names are skipped, and substitution runs only OUTSIDE existing placeholders so
    a stored name can never rewrite a placeholder's interior. No detection, no new
    bindings, no DB write. Kept a small self-contained duplicate of Step 1 rather
    than refactoring ``_redact_user_message`` so this stays surgical.
    """
    if not text or not inverted_ci:
        return text
    out = text
    # Longest names first so "Jay Haughton" matches before "Jay".
    for original, placeholder in sorted(
        ((name, ph) for name, ph in inverted_ci.values()),
        key=lambda x: -len(x[0]),
    ):
        if not original:
            continue
        if _is_denied(denylist, original):
            continue
        if _is_degenerate_span(original):
            continue
        pattern = known_name_pattern(original)
        out = _sub_outside_placeholders(out, pattern, placeholder)
    return out


def metadata_in_placeholder_space(metadata: str, entity_map: dict[str, Any]) -> str:
    """Return a short metadata descriptor containing no known raw names.

    Relationship text is user-curated and may itself mention another registry
    entity (``"recruiter at Optiver"``).  Reuse the registry's deterministic
    known-entity masking pass, then flatten nested bracket placeholders to bare
    identifiers (``ORG_2``) so the outer annotated token remains parseable.
    """
    descriptor = " ".join(metadata.split())
    if not descriptor:
        return ""
    descriptor = _replace_known_only(descriptor, _inverted_names_ci(entity_map), None)
    descriptor = _PLACEHOLDER_RE.sub(lambda match: f"{match.group(1)}_{match.group(2)}", descriptor)
    # Annotation delimiters cannot be nested.  Replace user-authored delimiter
    # characters instead of escaping them into a second wire grammar.
    descriptor = descriptor.replace("|", "/").replace("[", "").replace("]", "")
    descriptor = " ".join(descriptor.split()).strip()
    if len(descriptor) > _MAX_PLACEHOLDER_ANNOTATION_CHARS:
        descriptor = descriptor[:_MAX_PLACEHOLDER_ANNOTATION_CHARS].rstrip()
    return descriptor


def annotate_model_context(text: str, entity_map: dict[str, Any] | None) -> str:
    """Annotate identity placeholders for model-bound context without PII.

    PERSON/ORG/PLACE tokens with a curated relationship carry that relationship
    in placeholder space.  Tokens without one are explicitly ``unresolved``.
    Existing annotations are refreshed idempotently from the current registry.
    Other PII classes keep their historical bare form because a relationship is
    not meaningful for an email address, card number, and so on.
    """
    if not text or "[" not in text:
        return text

    registry = entity_map or {}

    def _annotate(match: re.Match) -> str:
        entity_type, number = match.group(1), match.group(2)
        if entity_type not in _RELATIONSHIP_ENTITY_TYPES:
            return match.group(0)
        placeholder = f"[{entity_type}_{number}]"
        entry = registry.get(placeholder)
        relationship = entry.get("relationship") if isinstance(entry, dict) else ""
        descriptor = metadata_in_placeholder_space(relationship, registry) if isinstance(relationship, str) else ""
        return f"[{entity_type}_{number}|{descriptor or 'unresolved'}]"

    return _PLACEHOLDER_RE.sub(_annotate, text)


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

    ``mint`` controls whether unfamiliar text may coin NEW placeholders (see the
    MINT_* constants). Callers feeding AGENT-authored markdown (memory sync,
    galaxy co-pilot) pass ``mint='never'`` so machine structure can't mint junk;
    the default ``'all'`` preserves the mint-everything behavior. Model-bound
    callers pass ``annotate=True`` to attach relationship/unresolved metadata;
    storage-oriented callers keep the default bare wire form.
    """

    def __init__(
        self,
        *,
        tenant: Tenant | None = None,
        tier: str | None = None,
        allow_names: set[str] | None = None,
        mint: str = MINT_ALL,
        annotate: bool = False,
    ):
        self.tenant = tenant
        self.allow_names = allow_names or set()
        self.annotate = annotate

        # Mint policy for this session (see the MINT_* constants). Default
        # ``all`` preserves the historical mint-everything behavior for callers
        # that don't opt in; an unknown value coerces to ``all`` (least
        # surprising — a typo must not silently stop protecting known people).
        # Agent-authored callers (memory sync, co-pilot) pass ``never``.
        self.mint = mint if mint in _MINT_POLICIES else MINT_ALL

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
            # Retired bindings are excluded: a tombstoned entity must stop being
            # substituted into agent-authored text (mint='never') and must not be
            # reused as a mint target. Counters still seed from the FULL map, so a
            # retired placeholder's number is never reissued.
            self._inverted_ci = _inverted_names_ci(existing_map, include_retired=False)
            self._type_counters = _seed_counters_from_map(existing_map)
            # Never number below the tenant's monotonic high-water mark, so a
            # session mint can't reuse a suffix freed by an earlier deletion.
            # Harmless for the mint='never' callers (memory sync, co-pilot,
            # cluster naming) that never allocate; load-bearing for friends/scrub
            # which mints under 'all' but never persists its session map.
            _apply_stored_high_water(self._type_counters, getattr(tenant, "pii_type_counters", None))
            # Workspace memory sync also respects the user's denylist so
            # false-positive entities don't get re-minted from documents.
            self._denylist = getattr(tenant, "pii_denylist", None) or {}

    def redact(self, text: str) -> str:
        """Redact PII from text, updating the session's entity map."""
        if not text or not text.strip() or not self.enabled or not self.entities:
            return text

        # mint='never': replace only already-known entities, coin nothing new.
        # Agent-authored markdown is the audit's #1 junk-mint source, so this
        # path runs no detector — known people stay masked, machine structure
        # (headings, table rules, timestamps) is left verbatim. No mint, no write.
        if self.mint == MINT_NEVER:
            try:
                result = _replace_known_only(text, self._inverted_ci, self._denylist)
                existing_map = getattr(self.tenant, "pii_entity_map", None) or {}
                if self.annotate:
                    return annotate_model_context(result, {**existing_map, **self.entity_map})
                return result
            except Exception:
                logger.exception("PII known-only replacement failed — returning original text")
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
                mint=self.mint,
            )
            existing_map = getattr(self.tenant, "pii_entity_map", None) or {}
            if self.annotate:
                return annotate_model_context(result, {**existing_map, **self.entity_map})
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
        # Normalize the (possibly markdown-escaped) match back to the
        # canonical ``[TYPE_N]`` key the entity map is keyed by.
        placeholder = f"[{match.group(1)}_{match.group(2)}]"
        entry = entity_map.get(placeholder)
        if entry is None:
            return match.group(0)
        name = _entry_name(entry)
        return name or match.group(0)

    return _REHYDRATE_PLACEHOLDER_RE.sub(_replace, text)


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


def redact_known_entities(tenant: Tenant | None, text: str) -> str:
    """Mask only PII the tenant map ALREADY knows — reuse-only, mints nothing.

    Runs NO model detection and NEVER writes to ``tenant.pii_entity_map``. It
    rewrites only spans that already have a placeholder in the tenant's map,
    case-insensitively and longest-match-first (so ``Jay Haughton`` beats
    ``Jay``), skipping denylisted entries and never touching the interior of an
    existing ``[TYPE_N]`` placeholder. Legacy string-shaped map entries are
    coerced transparently via ``entity_registry``.

    Use this for text that is NOT the user's own message — e.g. agent-authored
    platform-issue reports — where minting new placeholders would pollute the
    map with tooling text. For actual user ingress use ``redact_user_message``
    (which does detect + mint). Safe on a None tenant, empty/absent map, or
    empty text: returns text unchanged.
    """
    if not text or tenant is None:
        return text
    existing_map = getattr(tenant, "pii_entity_map", None) or {}
    if not existing_map:
        return text
    denylist = getattr(tenant, "pii_denylist", None) or {}
    # Retired bindings are skipped for the same reason denylisted ones are: the
    # user (or the stoplist backfill) declared the name is not PII for them, so
    # it must stop being masked. Rehydration of text already carrying the
    # placeholder is unaffected — that path keys by placeholder, not by name.
    inverted_ci = _inverted_names_ci(existing_map, include_retired=False)
    return _replace_known_only(text, inverted_ci, denylist)


def redact_user_message(
    text: str,
    tenant: Tenant,
    *,
    allow_user_name: bool = True,
    mint: str = MINT_ALL,
    ingress: PiiIngress | None = None,
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
        mint: Which NEW bindings this call may coin (see the MINT_* constants).
            Human-typed chat ingress leaves the default ``'all'``; the tool-
            response path passes ``'validated'`` so machine text only mints
            structurally-validated types (emails, cards, IBANs) and never coins
            a junk placeholder from a neural PERSON/LOCATION hit.

    Returns the redacted text. Updates tenant.pii_entity_map in the DB
    if new entities are discovered.
    """
    return redact_user_message_checked(
        text,
        tenant,
        allow_user_name=allow_user_name,
        mint=mint,
        ingress=ingress,
    ).text


def redact_user_message_checked(
    text: str,
    tenant: Tenant,
    *,
    allow_user_name: bool = True,
    mint: str = MINT_ALL,
    ingress: PiiIngress | None = None,
) -> RedactionOutcome:
    """Redact a user message and report whether the engine completed.

    Disabled-policy and exception paths retain the existing fail-open text
    behavior, but are explicitly unconfirmed so downstream persistence cannot
    mistake the original string for placeholder-space text.
    """
    if not text or not text.strip():
        return RedactionOutcome(text=text, confirmed=False, reason="empty-input")

    tier = getattr(tenant, "model_tier", "starter")
    policy = TIER_POLICIES.get(tier, TIER_POLICIES["starter"])
    if not policy.get("enabled", False):
        return RedactionOutcome(text=text, confirmed=False, reason="redaction-disabled")

    _reset_neural_detector_outcome()
    try:
        redacted = _redact_user_message(
            text,
            tenant,
            policy,
            allow_user_name=allow_user_name,
            mint=mint,
            ingress=ingress,
        )
    except Exception:
        logger.exception("User message PII redaction failed — returning original")
        return RedactionOutcome(text=text, confirmed=False, reason="redaction-error")
    if _neural_detector_available() is not True:
        return RedactionOutcome(text=redacted, confirmed=False, reason="neural-unavailable")
    return RedactionOutcome(text=redacted, confirmed=True, reason="redacted")


def _redact_user_message(
    text: str,
    tenant: Tenant,
    policy: dict,
    *,
    allow_user_name: bool = True,
    mint: str = MINT_ALL,
    ingress: PiiIngress | None = None,
) -> str:
    """Internal: redact user message with known + new entity detection."""
    if ingress is not None:
        # Reactivate before the known-value pass so the current occurrence uses
        # its historical placeholder even when the neural detector misses it.
        from apps.pii.provisional import reactivate_provisional_matches

        reactivate_provisional_matches(tenant, text, ingress)
    existing_map = getattr(tenant, "pii_entity_map", None) or {}
    denylist = getattr(tenant, "pii_denylist", None) or {}

    # Step 1: Replace known entities from the existing map (case-insensitive
    # match). ``inverted_ci`` is keyed by ``canonical_key(name)`` so
    # "Sautai", "sautai", and " Sautai " all resolve to the same
    # placeholder. The value tuple carries the display name (for regex
    # building) and the canonical placeholder (lowest-numbered if the
    # map has legacy duplicates from before this fix).
    #
    # Retired bindings are excluded here and in the post-detection known-entity
    # check below, so retiring a binding actually STOPS substitution — the
    # symmetric partner of the denylist skip a few lines down. Without this the
    # retire backfill would be cosmetic: Step 1 never consults ``_filter_results``,
    # so a retired "calendar" binding would keep masking the word forever.
    inverted_ci = _inverted_names_ci(existing_map, include_retired=False)
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
        # punctuation as before. CJK edges also drop the anchor (unspaced
        # Japanese has no ``\b`` between "田中" and "です"), so a stored Japanese
        # name re-masks across turns without needing a fresh model detection.
        esc = re.escape(original)
        left = _known_name_edge_anchor(original[:1])
        right = _known_name_edge_anchor(original[-1:])
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

        # Mint-policy gate. Known entities were replaced above (always allowed);
        # a NEW binding is coined only when the policy vouches for this type/text.
        # Tool responses run ``validated`` — a neural PERSON/LOCATION span from an
        # email body is left verbatim rather than minting a junk placeholder,
        # while an email-shaped or checksummed span still mints so inbound
        # correspondents stay protected. Chat ingress runs ``all`` (unchanged).
        if not _should_mint_new(mint, etype, original):
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
        # Read both the map AND the monotonic high-water counters from the LOCKED
        # row so numbering is derived from a snapshot no concurrent redaction can
        # mutate between our read and write.
        locked_row = (
            type(tenant)
            .objects.select_for_update()
            .filter(pk=tenant.pk)
            .values("pii_entity_map", "pii_type_counters")
            .first()
        ) or {}
        locked_map = locked_row.get("pii_entity_map") or {}
        stored_counters = locked_row.get("pii_type_counters") or {}

        # Re-derive per-type counters from the LOCKED snapshot, not the stale one
        # read at function start, then raise each to the stored high-water. The
        # high-water step is what stops recycling: a suffix freed by a prior
        # delete (bulk-delete, junk sweep) drops out of the map maxima but the
        # counter never fell, so the next mint still allocates above it.
        locked_counters = _seed_counters_from_map(locked_map)
        _apply_stored_high_water(locked_counters, stored_counters)

        # Case-insensitive view of the locked map so a name already present
        # collapses onto its existing placeholder instead of minting a dup.
        # Retired bindings are excluded: a tombstone is not a reuse target, so a
        # name that genuinely comes back mints a FRESH placeholder rather than
        # resurrecting the retired one (directive A9).
        locked_inverted_ci = _inverted_names_ci(locked_map, include_retired=False)
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
            from apps.pii.provisional import should_mint_provisional

            if should_mint_provisional(tenant, etype, original, ingress):
                seen_at = ingress.occurred_at.isoformat()
                entry = _entry_storage(
                    original,
                    provisional=True,
                    first_seen_at=seen_at,
                    last_seen_at=seen_at,
                    seen_events=[],
                    seen_dates=[],
                )
            else:
                entry = _entry_storage(original)
            logger.info(
                "pii_policy_mint tenant=%s type=%s outcome=%s",
                getattr(tenant, "id", "?"),
                etype,
                "provisional" if entry.get("provisional") else "permanent",
            )
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
            # One atomic write for the map AND the advanced high-water counters —
            # no new race surface: the counter is only ever raised, and it is
            # written under the same row lock as the map. ``locked_counters`` was
            # seeded from ``stored_counters`` so it carries every previously-
            # recorded type forward (never drops a type we didn't touch).
            type(tenant).objects.filter(pk=tenant.pk).update(
                pii_entity_map=merged,
                pii_type_counters=locked_counters,
            )

    # Update in-memory too, mirroring the persisted write.
    # Install the locked snapshot even when every requested mint collapsed onto
    # a concurrent writer's binding. The post-redaction ingress recorder must
    # see that binding to close the first-appearance mint/count race.
    tenant.pii_entity_map = merged
    tenant.pii_type_counters = locked_counters

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

    Recursively walks the JSON structure and applies redaction to string values.
    Tool text is MACHINE-generated (raw email bodies, tool JSON) — the audit's #2
    junk-mint source (newsletter senders, invisible-char runs, unvalidated
    financial labels). So this path mints under ``validated``: known entities are
    still replaced, email-shaped / checksummed spans still mint (inbound
    correspondents stay protected), but a neural PERSON/LOCATION hit is left
    verbatim rather than coining a new junk binding.

    Skips keys that are identifiers/metadata (id, html_link, internal_date, etc.)
    to avoid corrupting structured data.
    """
    tier = getattr(tenant, "model_tier", "starter")
    policy = TIER_POLICIES.get(tier, TIER_POLICIES["starter"])
    if not policy.get("enabled", False):
        return data

    try:
        redacted = _redact_tool_value(data, tenant, policy, _TOOL_SKIP_KEYS)
        return _annotate_model_value(redacted, getattr(tenant, "pii_entity_map", None) or {}, _TOOL_SKIP_KEYS)
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
        # mint='validated' so machine text only mints structurally-validated
        # types; neural PERSON/LOCATION here replace-if-known but never coin new.
        return redact_user_message(value, tenant, allow_user_name=False, mint=MINT_VALIDATED)
    elif isinstance(value, dict):
        return {
            k: (v if k in skip_keys else _redact_tool_value(v, tenant, policy, skip_keys)) for k, v in value.items()
        }
    elif isinstance(value, list):
        return [_redact_tool_value(item, tenant, policy, skip_keys) for item in value]
    else:
        return value


def _annotate_model_value(value: Any, entity_map: dict[str, Any], skip_keys: frozenset) -> Any:
    """Recursively annotate already-redacted tool data at its model boundary."""
    if isinstance(value, str):
        return annotate_model_context(value, entity_map)
    if isinstance(value, dict):
        return {
            key: (item if key in skip_keys else _annotate_model_value(item, entity_map, skip_keys))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_annotate_model_value(item, entity_map, skip_keys) for item in value]
    return value


# ---------------------------------------------------------------------------
# Detection: DeBERTa model + Presidio pattern recognizers
# ---------------------------------------------------------------------------


def _has_adjacent_address_label(ent: dict, model_results: list[dict], max_gap: int = 2) -> bool:
    """True when another RAW model span with an address-family label
    (ADDRESS_CONTEXT_LABELS) overlaps or sits within ``max_gap`` characters
    of ``ent`` on either side."""
    for other in model_results:
        if other is ent or other["entity_group"] not in ADDRESS_CONTEXT_LABELS:
            continue
        gap = max(other["start"] - ent["end"], ent["start"] - other["end"])
        if gap <= max_gap:
            return True
    return False


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

    # Hygiene helpers are best-effort: a missing module must degrade to raw
    # detection, never crash the redactor. mask_placeholders keeps the model from
    # re-detecting the interior of existing [TYPE_N] tokens; snap fixes the
    # truncated-span class ('amaica' -> 'Jamaica'). Both preserve character
    # offsets, so reported spans still index the ORIGINAL ``text`` unchanged.
    try:
        from apps.pii.hygiene import mask_placeholders, snap_to_word_boundaries
    except Exception:
        mask_placeholders = None
        snap_to_word_boundaries = None

    detect_text = mask_placeholders(text) if mask_placeholders is not None else text

    results: list[DetectedEntity] = []

    # 1. DeBERTa model — contextual PII (best effort).
    # If the model failed to load (ABI mismatch, missing weights), the
    # engine raises the cached load error. We swallow it here without
    # logging — the engine logs once at error level on first failure.
    # Pattern recognizers below still run, so financial PII stays redacted.
    try:
        pii_pipeline = get_pii_pipeline()
        model_results = pii_pipeline(detect_text)
        _neural_detector_outcome.available = True
    except Exception:
        _neural_detector_outcome.available = False
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
        # BUILDINGNUMBER is the one label we distrust: DeBERTa fires it on
        # bare measurements (body weight "82kg", "180 lbs", daily "82.5").
        # Skip it ONLY when the span is numeric-only AND no address-family
        # raw span sits adjacent — "82 Baker Street" keeps its 82 (STREET is
        # 1 char away), a lone weight never mints [LOCATION_n]. Checked on
        # the RAW label: after DEBERTA_LABEL_MAP collapses to LOCATION and
        # _merge_adjacent_spans runs, this distinction no longer exists.
        if raw_label == "BUILDINGNUMBER":
            span_text = text[ent["start"] : ent["end"]].strip()
            if _BARE_MEASUREMENT_RE.match(span_text) and not _has_adjacent_address_label(ent, model_results):
                continue
        entity_type = DEBERTA_LABEL_MAP.get(raw_label)
        # DATE has no static label-map entry (it fires on every journal date
        # heading, so it is dropped fleet-wide). Promote it to DATE_OF_BIRTH ONLY
        # when a birth-context cue sits beside the span — a disclosed birth date
        # redacts, while an ordinary calendar date still passes through untouched.
        if entity_type is None and raw_label == "DATE" and "DATE_OF_BIRTH" in entities:
            if _has_birth_context(text, ent["start"], ent["end"]):
                entity_type = "DATE_OF_BIRTH"
        if entity_type and entity_type in entities:
            # Trim leading/trailing whitespace from span boundaries —
            # aggregation_strategy="simple" can include boundary spaces. Extract
            # against the ORIGINAL text (offsets are shared with the masked copy).
            start, end = ent["start"], ent["end"]
            span_text = text[start:end]
            start += len(span_text) - len(span_text.lstrip())
            end -= len(span_text) - len(span_text.rstrip())
            if start >= end:
                continue
            # Snap name/place spans to word boundaries so a truncated neural span
            # recovers the whole word instead of minting a fragment. Restricted to
            # PERSON/LOCATION: for structured types a digit run is meaningful and
            # expansion could grab an adjacent digit.
            if snap_to_word_boundaries is not None and entity_type in ("PERSON", "LOCATION"):
                start, end = snap_to_word_boundaries(text, start, end)
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
    results = _merge_adjacent_spans(results, text)

    # 2. Presidio regex — credit cards (Luhn), IBANs (checksum), emails (regex
    # fallback). Runs over the placeholder-masked copy too, so a numeric
    # recognizer can never latch onto a placeholder's counter digits.
    pattern_recognizers = get_pattern_recognizers()
    for entity_type, recognizer in pattern_recognizers.items():
        if entity_type in entities:
            for r in recognizer.analyze(text=detect_text, entities=[entity_type]):
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


def _particle_separates(text: str, prev: DetectedEntity, current: DetectedEntity) -> bool:
    """True when the 1-char gap between two same-type spans is a real particle
    that means they are DIFFERENT people — and splitting them is safe.

    The merge bridges a 1-char gap to join "Sarah" + "Chen". In unspaced Japanese
    that 1 char can be a grammatical particle ("田中と佐藤" — と = "and"), where
    merging would fuse two different people onto one placeholder. Two guards,
    BOTH required, decide when to keep the spans separate instead:

    - the bridge char is HIRAGANA (particles are hiragana: と/を/に/は/が/の). A
      KATAKANA connector (ノ ヶ ツ ・) is name-INTERNAL (一ノ瀬, 保土ヶ谷,
      マイケル・ジョーンズ) and must NOT split the name.
    - neither resulting fragment would be dropped by the junk filter. A single
      kanji surname split off on its own ("林" out of "林と森") is ``too_short``
      and would be dropped — which leaks the name in cleartext. If splitting
      would strand a droppable fragment we merge instead (fusion is safe;
      cleartext is not).
    """
    from apps.pii.hygiene import contains_hiragana, is_junk_span

    bridge = text[prev.end : current.start]
    if not contains_hiragana(bridge):
        return False
    prev_text = text[prev.start : prev.end]
    cur_text = text[current.start : current.end]
    if is_junk_span(prev_text, prev.entity_type)[0] or is_junk_span(cur_text, current.entity_type)[0]:
        return False
    return True


def _merge_adjacent_spans(results: list[DetectedEntity], text: str = "") -> list[DetectedEntity]:
    """Merge consecutive spans of the same entity type.

    After label mapping, GIVENNAME and SURNAME both become PERSON.
    "Sarah" (PERSON, 0-5) and "Chen" (PERSON, 6-10) should merge into
    "Sarah Chen" (PERSON, 0-10).

    Spans are considered adjacent if separated by 0-1 characters (a space). The
    one exception is a hiragana particle between two independently-redactable
    names, which keeps them separate so two different people get distinct
    placeholders — see :func:`_particle_separates`.
    """
    if len(results) <= 1:
        return results

    sorted_results = sorted(results, key=lambda r: r.start)
    merged = [sorted_results[0]]

    for current in sorted_results[1:]:
        prev = merged[-1]
        gap = current.start - prev.end
        if prev.entity_type == current.entity_type and 0 <= gap <= 1 and not _particle_separates(text, prev, current):
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
    mint: str = MINT_ALL,
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

    ``mint`` gates whether unfamiliar spans may coin NEW placeholders (see the
    MINT_* constants). Known-entity reuse via ``inverted_ci`` is unaffected — the
    gate only blocks fresh mints, so ``validated`` mints validator-approved types
    and ``never`` mints nothing while both still reuse known bindings.

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

        # Mint-policy gate: known entities were reused above; a NEW binding is
        # coined only when the policy allows it for this type/text (agent-authored
        # sessions run ``never`` and never reach a mint here).
        if not _should_mint_new(mint, etype, original):
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
    minting a placeholder, plus the deterministic ``apps.pii.hygiene`` layer
    (structural junk + structured-type validation). ``tenant`` is used only for
    skip telemetry.
    """
    # Best-effort: a missing hygiene module must degrade to the legacy guards,
    # never drop every hit. Both callables stay None when unavailable.
    try:
        from apps.pii.hygiene import is_junk_span, validate_structured
    except Exception:
        is_junk_span = None
        validate_structured = None

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
            if _is_fitness_span(matched_lower, full_text=text):
                _log_skip(tenant, result, "fitness_vocab", len(matched_text))
                continue
            if _is_common_word_span(matched_lower, _at_sentence_start(text, result.start)):
                _log_skip(tenant, result, "common_word", len(matched_text))
                continue
            if result.entity_type == "LOCATION" and _DATE_LIKE_RE.match(matched_text):
                _log_skip(tenant, result, "date_like", len(matched_text))
                continue

        # Deterministic hygiene — the audit's junk taxonomy (markdown/table
        # structure, zero-width/HTML-entity runs, self-redacted placeholder
        # fragments, dates/measurements, code identifiers) across ALL types.
        if is_junk_span is not None:
            junk, reason = is_junk_span(matched_text, result.entity_type)
            if junk:
                _log_skip(tenant, result, reason, len(matched_text))
                continue

        # Structured-type validation — neural CREDIT_CARD/EMAIL/PHONE/ACCOUNT/
        # CRYPTO labels carry NO checksum, so "django" as a card or a temperature
        # range as an account reached the mint before this. PERSON/LOCATION are
        # free-form and validate_structured returns False for them by contract
        # (the mint gate needs that) — so they are NEVER gated here, only the
        # structurally-shaped types are.
        if (
            validate_structured is not None
            and result.entity_type not in ("PERSON", "LOCATION")
            and not validate_structured(result.entity_type, matched_text)
        ):
            _log_skip(tenant, result, "structured_invalid", len(matched_text))
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
