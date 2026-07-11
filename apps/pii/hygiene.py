"""Deterministic span hygiene + structured-type validation for PII detection.

The prod audit of the canary tenant found 979/1103 bindings were junk — never
human chat, always MACHINE text: agent-authored workspace markdown (table
separators, headings, timestamps), raw tool payloads (newsletter senders,
zero-width runs, email bodies), and neural financial labels with no validation
("django"/"USER.md" as CREDIT_CARD, temperature ranges as ACCOUNT). This module
is the deterministic layer that keeps that junk from ever minting a placeholder.
It replaces the cloud arbiter's role of *catching* structural junk (the arbiter
shipped PERSON/LOCATION span text to a cloud LLM; that egress is being retired).

Design constraints (WHY this file is framework-free)
----------------------------------------------------
This module is imported by BOTH the redactor detection seam (Django request
path) AND a standalone junk-sweep task that reprocesses historical bindings. So
it depends on stdlib only — no Django, no models, no network. Every function is
pure: same input → same output, no I/O, no global state. That is what lets the
sweep run it over a million stored spans without a DB round-trip per span, and
lets the redactor call it inline on every inbound message.

Bias: FALSE-JUNK ON REAL PII IS THE FAILURE MODE TO AVOID. The sweep and an
on-device user-review flow are the safety nets that catch leftovers, so when a
rule is ambiguous these functions deliberately err toward NOT-junk / valid.
"""

from __future__ import annotations

import re
import unicodedata

# ---------------------------------------------------------------------------
# Placeholder masking (fed to the NER model so it can never re-detect the
# interior of an existing [TYPE_N] token as fresh PII).
# ---------------------------------------------------------------------------

# Matches [PERSON_1] and the markdown-escaped \[PERSON_1\] variant the agent
# sometimes writes in journal markdown. The type group allows underscores
# (EMAIL_ADDRESS, CRYPTO_ADDRESS) and the trailing _\d+ is the mint counter.
_MASK_PLACEHOLDER_RE = re.compile(r"\\?\[[A-Z_]+_\d+\\?\]")

# Same-length neutral filler. One code point per replaced char keeps every
# character offset stable, so spans the model reports over the surrounding text
# still map cleanly back onto the ORIGINAL (unmasked) string.
_FILLER = "•"  # bullet '•' — not a letter/digit, so it never re-detects


def mask_placeholders(text: str) -> str:
    """Replace ``[TYPE_N]`` (and ``\\[TYPE_N\\]``) tokens with same-length filler.

    Detection re-runs over PARTIALLY-redacted text (Step 1 substitutes known
    contacts before the model sees the message). Without masking, the NER model
    classifies the tokens inside ``[EMAIL_ADDRESS_1]`` as PERSON/USERNAME and the
    redactor used to explode the placeholder into nested garbage ("[CRYP",
    "CODE_1]ADDRESS" in the audit). Masking to a bullet run the model treats as
    noise removes the input that caused it, while length preservation keeps all
    reported offsets valid against the unmasked source.
    """
    if not text or "[" not in text:
        return text
    return _MASK_PLACEHOLDER_RE.sub(lambda m: _FILLER * len(m.group(0)), text)


# ---------------------------------------------------------------------------
# CJK awareness. Japanese/Chinese scripts have no ASCII word breaks, so the
# alnum-walk word snapping below (built for space-delimited languages) would run
# a short name span across the entire surrounding sentence. Detecting CJK code
# points lets the snap stop at a script boundary, and lets the redactor's
# degenerate-span floor treat a 2-character CJK name as the complete name it is.
# ---------------------------------------------------------------------------


def _is_cjk_char(ch: str) -> bool:
    """True for a Han ideograph or Japanese kana code point.

    Covers the scripts with no ASCII word separators that break the alnum-walk
    snapping: Hiragana, Katakana (incl. halfwidth), CJK Unified Ideographs and
    the common extension/compatibility blocks. Deliberately narrow — it gates a
    redaction heuristic, so it errs toward the scripts we can verify.
    """
    o = ord(ch)
    return (
        0x3040 <= o <= 0x30FF  # Hiragana + Katakana
        or 0x31F0 <= o <= 0x31FF  # Katakana phonetic extensions
        or 0x3400 <= o <= 0x4DBF  # CJK Unified Ideographs Extension A
        or 0x4E00 <= o <= 0x9FFF  # CJK Unified Ideographs
        or 0xF900 <= o <= 0xFAFF  # CJK Compatibility Ideographs
        or 0xFF66 <= o <= 0xFF9D  # Halfwidth Katakana
    )


def contains_cjk(text: str) -> bool:
    """True when ``text`` carries any Han/kana character (see :func:`_is_cjk_char`)."""
    return any(_is_cjk_char(ch) for ch in text)


def _is_snap_expandable(ch: str) -> bool:
    """True when word-snapping may absorb ``ch`` into a span: a unicode-alnum
    word char that is NOT CJK.

    Excluding CJK is the whole fix for the over-expansion bug: in a space-less
    Japanese sentence every character is alnum, so the unrestricted walk swallowed
    the entire sentence into one name span. Latin/digit recovery ('amaica' ->
    'Jamaica') is unaffected — those code points are alnum and non-CJK.
    """
    return ch.isalnum() and not _is_cjk_char(ch)


# ---------------------------------------------------------------------------
# Word-boundary snapping (fixes truncated neural spans: 'amaica' -> 'Jamaica').
# ---------------------------------------------------------------------------


def snap_to_word_boundaries(text: str, start: int, end: int) -> tuple[int, int]:
    """Expand a span so both edges sit on a word boundary in ``text``.

    The DeBERTa tokenizer sometimes reports a span that starts or ends mid-word
    ("amaica" for Jamaica, "tingham Forest" for Nottingham Forest). Extracting
    that fragment mints a junk half-name AND leaves the rest of the real name in
    cleartext. Snapping walks each edge outward over adjacent alphanumeric
    characters (unicode-aware) until it hits a non-word char (space, punctuation,
    the bullet filler from :func:`mask_placeholders`), recovering the whole word.

    The walk stops at CJK code points (:func:`_is_snap_expandable`): Japanese/
    Chinese has no ASCII spaces, so without that stop a 2-char name detection
    (田中) expanded to swallow the whole unpunctuated sentence and stored the
    sentence as a fake name. For CJK we trust the detector's span instead.

    Expansion-only on purpose: growing a span to the full word it touches can
    only make redaction MORE complete, never leak. It never crosses whitespace or
    punctuation, so it cannot merge a name with an unrelated neighbouring word.
    """
    if not text:
        return start, end
    n = len(text)
    start = max(0, min(start, n))
    end = max(0, min(end, n))
    if start >= end:
        return start, end
    while start > 0 and _is_snap_expandable(text[start - 1]):
        start -= 1
    while end < n and _is_snap_expandable(text[end]):
        end += 1
    return start, end


# ---------------------------------------------------------------------------
# Junk-span classification.
# ---------------------------------------------------------------------------

# Zero-width / invisible / bidi format chars observed minting phantom spans from
# tool payloads (newsletter HTML). unicodedata category 'Cf' (format) covers most
# of these generically; U+034F COMBINING GRAPHEME JOINER is category 'Mn' so it is
# listed explicitly. Normal whitespace (\t\n\r) is handled by the structure rule.
_INVISIBLE_CHARS = frozenset(
    chr(cp)
    for cp in (
        0x200B,  # zero-width space
        0x200C,  # zero-width non-joiner
        0x200D,  # zero-width joiner
        0x2060,  # word joiner
        0x00AD,  # soft hyphen
        0x034F,  # combining grapheme joiner (category Mn — not caught by Cf)
        0xFEFF,  # BOM / zero-width no-break space
        0x180E,  # Mongolian vowel separator
        0x2061,  # function application
        0x2062,  # invisible times
        0x2063,  # invisible separator
        0x2064,  # invisible plus
        0x200E,  # left-to-right mark
        0x200F,  # right-to-left mark
    )
)

# HTML entities (&#39; &lt; &amp;) leak from raw tool payloads; their presence in
# a "name"/"location" span is a reliable machine-text tell.
_HTML_ENTITY_RE = re.compile(r"&(?:#\d{2,}|#x[0-9a-fA-F]+|[a-zA-Z]{2,8});")

# 3+ consecutive hyphens: a markdown table divider / horizontal rule ("----").
# Single/double hyphens are left alone so hyphenated names ("Baden-Württemberg",
# "Jean-Luc") survive.
_HR_RE = re.compile(r"-{3,}")

# A bracket-stripped placeholder fragment ("CODE_1", "PERSON_1"). The bracket
# check catches the audit examples directly; this is defence for fragments that
# lost their brackets in some transform. Kept ALL-CAPS-prefix + counter so it
# won't fire on ordinary snake_case.
_PARTIAL_PLACEHOLDER_RE = re.compile(r"[A-Z]{2,}_\d+")

# ISO/slash dates and clock times — junk under ANY entity type (no detector is
# supposed to emit DATE; a date mislabeled PERSON/ACCOUNT/IP is always junk).
_DATE_CLOCK_RE = re.compile(
    r"""
    (?:
        \d{4}-[Ww]\d{1,2}              # ISO week: 2026-W25
      | \d{4}-\d{1,2}-\d{1,2}          # ISO date: 2026-05-30
      | \d{4}-\d{1,2}                  # year-month: 2026-05
      | \d{4}/\d{1,2}/\d{1,2}          # 2026/5/30
      | \d{1,2}/\d{1,2}/\d{2,4}        # 5/30/2026
      | \d{1,2}:\d{2}(?::\d{2})?\s*(?:[ap]m)?   # 08:05, 18:29:00, 8:00 pm
      | \d{1,2}\s*[ap]m                # 8am, 8 pm
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Bare number / measurement / range — junk ONLY for the loosely-typed contextual
# labels (PERSON/LOCATION). For phones/cards/accounts a digit run is EXPECTED, so
# this is not applied to them (validate_structured is their gate instead).
_BARE_NUM_RANGE_RE = re.compile(
    r"""
    (?:
        \d{1,4}(?:\.\d+)?\s?(?:kg|kgs|lbs?|reps?|sets?|km|mi|mins?|secs?|hrs?|hours?
                              |bpm|cal|kcal|%|°?[cf]|°|cm|mm|ml|ft|in)?   # 82, 140.5kg, 82%
      | \d{1,4}(?:\.\d+)?\s*[–—-]\s*\d{1,4}(?:\.\d+)?\s?(?:°?[cf]|°|%|kg|lbs?|km|mi)?  # 18-29, 18–29°C
      | x\d+                                                             # x10
      | \d{1,4}x\d+                                                      # 5x5
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Hyphenated postal code (Japan's NNN-NNNN). Shares the exact surface shape of a
# bare numeric range ("18-29"), so a CORRECTLY-labeled ZIPCODE hit (which
# collapses to LOCATION) would otherwise be swallowed by the range guard above
# and never redact — a real leak for a Japan-centric user base. A US ZIP+4
# ("94103-1234") has a 5-digit side that already exceeds the range regex's
# 4-digit cap, so this carve-out is only needed for the short JP shape.
_POSTAL_CODE_RE = re.compile(r"\d{3}-\d{4}")

# Single-token code identifiers. Applied ONLY to PERSON/LOCATION — those are the
# labels the audit found firing on code tokens (filenames, dotted modules, commit
# shas) from agent notes and tool output. Structured/secret types are NEVER run
# through this: their legitimate values (hex crypto addresses, alnum account
# numbers, digit-run phones) collide with these shapes, and validate_structured
# already gates them.
_FILE_EXT_RE = re.compile(
    r"\.(?:py|js|jsx|ts|tsx|md|json|ya?ml|sh|bash|zsh|txt|csv|tsv|html?|css|scss|sass"
    r"|sql|go|rs|java|rb|php|xml|toml|ini|cfg|conf|lock|env|log|png|jpe?g|gif|svg|pdf"
    r"|zip|gz|tar)$",
    re.IGNORECASE,
)
# A dot followed by a 2+ char lowercase/digit run (".py", ".pii", ".redactor").
# Requiring a lowercase/digit run after the dot skips uppercase abbreviations
# ("U.S.", "L.A.") so real location abbreviations are not flagged.
_DOTTED_IDENT_RE = re.compile(r"\.[a-z0-9]{2,}")
_SNAKE_RE = re.compile(r"[A-Za-z0-9]+(?:_[A-Za-z0-9]+)+")
# All-lowercase kebab so Title-Case hyphenated NAMES ("Jean-Luc") are excluded.
_KEBAB_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)+")
_HEX_BLOB_RE = re.compile(r"[0-9a-f]{7,64}", re.IGNORECASE)

# Structured-secret run: min length, no whitespace, alnum + common secret/account
# punctuation only. Excludes the degree sign / en-dash of a temperature range and
# any spaced free text.
_SECRET_RUN_RE = re.compile(r"[A-Za-z0-9\-_.@!#$%^&*+]+")

_IPV4_RE = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")
# Loose IPv6/MAC: hex groups joined by ':' (2+ colons). MAC addresses map to
# IP_ADDRESS in the label map and are legitimately redactable here.
_IPV6_RE = re.compile(r"(?=.*:.*:)[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7}")

# Types with no structural shape — free-form names/places. validate_structured
# returns False for these BY CONTRACT with the redactor mint gate
# (_should_mint_new): a neural PERSON/LOCATION span from a tool-response body
# must not mint. The detection FILTER never gates these on validate_structured.
_UNSTRUCTURED_TYPES = frozenset({"PERSON", "LOCATION"})


def is_junk_span(text: str, entity_type: str) -> tuple[bool, str]:
    """Return ``(True, reason_code)`` when a candidate span is deterministic junk.

    Reason codes: ``structure``, ``invisible``, ``placeholder_fragment``,
    ``numeric_datelike``, ``identifier``, ``too_short``. ``(False, "")`` when the
    span looks like plausible PII and should be kept.

    Type-agnostic rules (structure, invisible, placeholder fragments, dates/clocks,
    too-short) apply to every type — none of them ever match a legitimate
    card/phone/email/account. The bare-number/range and code-identifier rules are
    scoped to PERSON/LOCATION, because a digit run or hex token is a valid shape for
    the structured types and would be false-junk there.
    """
    if not text:
        return True, "too_short"
    stripped = text.strip()

    # too_short: empty-after-strip or a single char — nothing to redact.
    if len(stripped) < 2:
        return True, "too_short"

    # invisible: zero-width / format / control chars, or HTML entities.
    if _has_invisible(text) or _HTML_ENTITY_RE.search(text):
        return True, "invisible"

    # structure: markdown / multiline machine text. Checked before the
    # pure-punctuation case below so a table divider ("|----|----|") reports
    # ``structure`` rather than the less-specific ``too_short``.
    if _is_structure(text):
        return True, "structure"

    # placeholder_fragment: detection re-ran over already-redacted text.
    if "[" in text or "]" in text or _PARTIAL_PLACEHOLDER_RE.search(text):
        return True, "placeholder_fragment"

    # numeric_datelike (dates/clocks): junk under every type EXCEPT
    # DATE_OF_BIRTH — the one label that deliberately emits a date-shaped span as
    # PII (the birth-context gate in redactor._detect_pii). Without this carve-out
    # an ISO-format birth date ("1990-03-15") would be culled here before it could
    # redact.
    if entity_type != "DATE_OF_BIRTH" and _DATE_CLOCK_RE.fullmatch(stripped):
        return True, "numeric_datelike"

    # Pure punctuation (no letter/digit) that survived the structure check —
    # an emoticon / stray symbol run, never PII.
    if not any(ch.isalnum() for ch in stripped):
        return True, "too_short"

    # Remaining rules are name/place-only — a digit run or code token is a valid
    # shape for the structured types and validate_structured gates those.
    if entity_type in _UNSTRUCTURED_TYPES:
        # A JP postal code (150-0041) shares the bare-range shape but is a real,
        # correctly-labeled ZIPCODE — exempt it so the range guard doesn't drop it.
        if _BARE_NUM_RANGE_RE.fullmatch(stripped) and not _POSTAL_CODE_RE.fullmatch(stripped):
            return True, "numeric_datelike"
        if _is_code_identifier(stripped):
            return True, "identifier"

    return False, ""


def _has_invisible(text: str) -> bool:
    """True when the text carries a zero-width / format / stray control char."""
    for ch in text:
        if ch in _INVISIBLE_CHARS:
            return True
        cat = unicodedata.category(ch)
        if cat == "Cf":  # format chars: ZWSP/ZWNJ/ZWJ/word-joiner/soft-hyphen/BOM/bidi
            return True
        if cat == "Cc" and ch not in "\t\n\r":  # stray control chars
            return True
    return False


def _is_structure(text: str) -> bool:
    """True for markdown / multiline machine text (headings, tables, lists)."""
    if "\n" in text or "\r" in text:
        return True
    if "|" in text or "**" in text:
        return True
    if _HR_RE.search(text):
        return True
    stripped = text.strip()
    if stripped.startswith("#"):
        return True
    return stripped.startswith(("- ", "* ", "+ ", "> "))


def _is_code_identifier(stripped: str) -> bool:
    """True for a single-token code identifier (PERSON/LOCATION only caller).

    Multi-word spans return False here — real names/addresses have spaces, and the
    audit's identifier junk was always single tokens. Sub-rules err conservative:
    kebab requires all-lowercase (keeps "Jean-Luc"), dotted requires a lowercase
    run after the dot (keeps "U.S."), hex requires a digit (keeps "facade").
    """
    if not stripped or any(ch.isspace() for ch in stripped):
        return False
    lowered = stripped.lower()
    if "://" in lowered or lowered.startswith(("http", "www.")):
        return True
    if "/" in stripped or "\\" in stripped:
        return True
    if _FILE_EXT_RE.search(stripped):
        return True
    if _DOTTED_IDENT_RE.search(stripped):
        return True
    # snake_case / kebab-case must carry a LETTER — else a hyphenated number run
    # like a zip+4 ("94103-1234") or "555-1234" would false-match as code.
    has_alpha = any(ch.isalpha() for ch in stripped)
    if has_alpha and (_SNAKE_RE.fullmatch(stripped) or _KEBAB_RE.fullmatch(stripped)):
        return True
    if _HEX_BLOB_RE.fullmatch(stripped) and any(ch.isdigit() for ch in stripped):
        return True
    return False


# ---------------------------------------------------------------------------
# Structured-type validation.
# ---------------------------------------------------------------------------


def validate_structured(entity_type: str, text: str) -> bool:
    """True when a structured span passes shape/checksum validation for its type.

    The neural model emits CREDIT_CARD/EMAIL/PHONE/ACCOUNT/CRYPTO labels with NO
    validation (the audit's "django" CREDIT_CARD, temperature-range ACCOUNT). This
    is the deterministic gate: Presidio's checksummed hits pass unchanged, neural
    false positives are culled.

    PERSON/LOCATION return ``False`` — they have no structural shape, and the
    redactor mint gate (``_should_mint_new``) relies on that to keep tool-response
    neural names from minting. The detection FILTER must therefore NEVER gate
    PERSON/LOCATION on this function (see ``redactor._filter_results``).
    """
    if not text:
        return False
    s = text.strip()
    if not s:
        return False

    if entity_type in _UNSTRUCTURED_TYPES:
        return False

    if entity_type == "EMAIL_ADDRESS":
        if _EMAIL_RE.fullmatch(s):
            return True
        # Relay-obfuscated form: privacy forwarders (duck.com) rewrite the
        # sender as ``local_at_domain[_alias@duck.com]``, so a REAL contact
        # address can arrive with ``_at_`` instead of a literal ``@``. The
        # labeled prod eval caught exactly one real keeper failing here
        # (false-junk gate) — normalize ``_at_`` -> ``@`` and re-check so the
        # obfuscated form validates as the email it stands for.
        if "_at_" in s:
            return bool(_EMAIL_RE.fullmatch(s.replace("_at_", "@", 1)))
        return False

    if entity_type == "CREDIT_CARD":
        digits = re.sub(r"\D", "", s)
        return 12 <= len(digits) <= 19 and _luhn_ok(digits)

    if entity_type == "IBAN_CODE":
        return _iban_ok(s)

    if entity_type == "PHONE_NUMBER":
        digits = re.sub(r"\D", "", s)
        if not (7 <= len(digits) <= 15):
            return False
        return not bool(_DATE_CLOCK_RE.fullmatch(s))

    if entity_type == "IP_ADDRESS":
        return _is_ip(s)

    if entity_type == "PASSWORD":
        # PINs (a PASSWORD via the PIN label) are 4-6 digits — a shorter floor
        # than account/crypto runs so a genuine PIN still redacts.
        return _is_secret_run(s, min_len=4)

    if entity_type in ("ACCOUNT", "CRYPTO_ADDRESS", "ID_DOCUMENT"):
        return _is_secret_run(s)

    if entity_type == "DATE_OF_BIRTH":
        # A birth date only reaches this type when a birth-context cue sits beside
        # it (redactor._detect_pii), so here we only confirm the span is date-
        # SHAPED — numeric ISO/slash, an English month-name date, or a Japanese
        # 年月日 date — and reject prose the model mislabeled DATE.
        return bool(_DOB_DATE_RE.fullmatch(s))

    # Unknown structured type — nothing to validate against; stay conservative so
    # the mint gate fails closed rather than minting an unvalidatable span.
    return False


_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")

# Date shapes accepted for a DATE_OF_BIRTH span (see validate_structured). Covers
# numeric ISO/slash dates, English month-name dates ("March 3, 1990"), and
# Japanese 年月日 dates ("1990年3月3日"). Detection has already required birth
# context, so this is a shape sanity gate, not a calendar validation.
_DOB_MONTHS = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?"
    r"|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)
_DOB_DATE_RE = re.compile(
    rf"""
    (?:
        \d{{3,4}}\s*[-/年]\s*\d{{1,2}}\s*[-/月]\s*\d{{1,2}}\s*日?      # 1990-03-15 · 1990/3/15 · 1990年3月3日
      | \d{{1,2}}\s*[-/]\s*\d{{1,2}}\s*[-/]\s*\d{{2,4}}               # 3/15/1990 · 03-15-90
      | (?:{_DOB_MONTHS})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}   # March 3, 1990
      | \d{{1,2}}(?:st|nd|rd|th)?\s+(?:{_DOB_MONTHS})\.?,?\s+\d{{4}}   # 3 March 1990
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _luhn_ok(digits: str) -> bool:
    """Luhn (mod-10) checksum over a pure-digit string."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _iban_ok(text: str) -> bool:
    """IBAN shape (CC + 2 check digits + BBAN) and mod-97 == 1 checksum."""
    s = re.sub(r"\s+", "", text).upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{1,30}", s):
        return False
    rearranged = s[4:] + s[:4]
    # A=10 .. Z=35 (ord('A')==65, so ord-55).
    converted = "".join(str(ord(c) - 55) if c.isalpha() else c for c in rearranged)
    try:
        return int(converted) % 97 == 1
    except ValueError:
        return False


def _is_ip(s: str) -> bool:
    """IPv4 (octet range checked) or loose IPv6/MAC (hex groups joined by ':')."""
    if _IPV4_RE.fullmatch(s):
        return all(p.isdigit() and int(p) <= 255 for p in s.split("."))
    return bool(_IPV6_RE.fullmatch(s))


def _is_secret_run(s: str, min_len: int = 6) -> bool:
    """True for an account/password/crypto/id run: len>=min_len, no spaces, has a
    digit, alnum + secret punctuation only, and not a date/time.

    The digit requirement is what distinguishes a real account/secret from prose
    ("django", "USER.md"); the charset requirement drops temperature ranges
    ("18–29°C") whose degree sign and en-dash are outside it. ``min_len`` is 6 for
    account/crypto/id runs and 4 for PASSWORD (PINs).
    """
    if len(s) < min_len:
        return False
    if any(ch.isspace() for ch in s):
        return False
    if _DATE_CLOCK_RE.fullmatch(s):
        return False
    if not _SECRET_RUN_RE.fullmatch(s):
        return False
    return any(ch.isdigit() for ch in s)
