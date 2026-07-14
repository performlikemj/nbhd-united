#!/usr/bin/env python3
"""CI guard: no raw DB value-predicates against columns slated for
encryption-at-rest.

Design: ``CONTINUITY_encryption-phase1.md`` §1 PR6 /
``docs/encryption-at-rest-directive.md``. Phase 1 ships the crypto substrate
DARK (nothing encrypted yet); Phases 2-4 flip specific ``(model, column)``
pairs to store ``bytea`` ciphertext behind ``apps.crypto.box``. Once a column
flips, a raw Django ORM value-predicate against it
(``.filter(col="x")``, ``.exclude(col__icontains="x")``, ``Q(col__gt="")``)
does not error — it just silently stops matching real content, because the
stored bytes are AES-GCM ciphertext with no relationship to the plaintext.
That's a correctness bug with no traceback, the worst kind.

This static check (no DB, no Django setup) polices the boundary NOW so a
future PR — written by someone who doesn't know column X flipped to
ciphertext in Phase 3 — fails at PR time instead of shipping a query that
quietly returns nothing.

How it works: scans ``apps/**/*.py`` for ``.filter(``, ``.exclude(``, and
``Q(`` calls (paren-depth matched, so nested calls are each also inspected).
Inside each call's argument list, it looks for a keyword predicate against a
registered column — either a value-comparison lookup suffix
(``__icontains``, ``__gt``, ``__exact``, ...) or bare equality
(``col=value``). To avoid false positives from same-named columns on
unrelated models (``title``, ``text``, and ``markdown`` are common field
names — see ``Task.title``, ``Goal.title`` for real examples that must NOT
trigger this), a hit only counts when the model name appears nearby (within
~25 lines above the call, e.g. ``Document.objects.filter(...)`` or, in a
data migration, ``apps.get_model("journal", "Document")``).

Two escape hatches, by design:

  1. **In-script allowlist** (``_ALLOWLISTED_SITES``) — the known
     pre-existing sites, all written before their column was ever a
     candidate for encryption. Do NOT edit product code to appease this
     guard; the allowlist is the record of "this predicate predates the
     flip, revisit when the column encrypts."
  2. **Inline** ``# noqa: encrypted-predicate`` on the offending line — for
     a new PR that has a deliberate, reviewed reason to touch a registered
     column before its phase lands (e.g. cleanup code that only ever
     touches Phase-3 columns while they're still plaintext, during Phase 1).

Known limitation (shared with any regex/paren-depth text scan, not an
AST): a string literal containing an unescaped ``(`` or ``)`` inside a
scanned call can throw off span matching. None of the columns tracked here
have such values today.

Usage: ``python scripts/check_encrypted_column_predicates.py`` — prints a
summary and exits 0 when clean, 1 with named violations otherwise.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
APPS_DIR = REPO_ROOT / "apps"

# ---------------------------------------------------------------------------
# Registry: (model, column) pairs slated for encryption, Phase 2-4
# (CONTINUITY_encryption-phase1.md §3). Nothing is encrypted yet (Phase 1
# ships dark) — this registry is the forward-looking contract, not a
# statement about today's storage.
# ---------------------------------------------------------------------------
ENCRYPTED_COLUMNS: dict[tuple[str, str], str] = {
    # Phase 2 — iOS-surviving chat surface (Telegram/LINE tables excluded;
    # they're deleted at decommission, not encrypted).
    ("AppChatMessage", "reply_text"): "phase2",
    ("AppChatMessage", "user_text"): "phase2",
    ("ChatThread", "title"): "phase2",
    ("ProactiveOutbound", "message_text"): "phase2",
    ("ProactiveOutbound", "parsed_items"): "phase2",
    # Phase 3 — journal (+ blind-index search)
    ("Document", "markdown"): "phase3",
    ("Document", "title"): "phase3",
    ("Lesson", "text"): "phase3",
    ("Lesson", "galaxy_note"): "phase3",
    # Phase 4 — PII map
    ("Tenant", "pii_entity_map"): "phase4",
    ("Tenant", "pii_denylist"): "phase4",
}

# ---------------------------------------------------------------------------
# Allowlist: known pre-existing sites (found by grepping current `main` for
# every registered column inside .filter(/.exclude(/Q( — see the PR that
# introduced this guard for the exact commands). All predate the registry;
# each is a value-existence/substring check on still-plaintext data. Revisit
# each when its column's phase flips — that PR is expected to touch these
# lines anyway (sidecar field, dual-read routing, etc).
#
# Entries: (path relative to repo root, 1-indexed line of the predicate, column)
# ---------------------------------------------------------------------------
_ALLOWLISTED_SITES: set[tuple[str, int, str]] = {
    # AppChatMessage.reply_text — unread-badge count excludes empty replies.
    ("apps/router/push_views.py", 290, "reply_text"),
    # ProactiveOutbound.parsed_items — ops report: count rows with structured
    # extraction vs raw text only.
    ("apps/router/management/commands/audit_proactive_sync.py", 65, "parsed_items"),
    # Lesson.galaxy_note — "active star" query: has a pinned galaxy note.
    ("apps/lessons/agent_context.py", 91, "galaxy_note"),
    # Lesson.text — duplicate-lesson guard before approving a new one.
    ("apps/journal/extraction.py", 235, "text"),
    # Document.markdown — grounding-probe cron: is a topic reachable via
    # literal-phrase match.
    ("apps/orchestrator/grounding_probe.py", 85, "markdown"),
    # Document.markdown — one-time data migration cleaning up NaN-slug stubs
    # by their unrendered {{date}} placeholder body.
    ("apps/journal/migrations/0020_cleanup_nan_daily_stubs.py", 31, "markdown"),
    # Tenant.pii_entity_map — arbiter/junk-sweep/denylist commands and a data
    # migration all use "has any PII map entries" as their candidate filter.
    ("apps/pii/arbiter.py", 356, "pii_entity_map"),
    ("apps/pii/junk_sweep.py", 297, "pii_entity_map"),
    ("apps/pii/management/commands/denylist_degenerate_pii.py", 77, "pii_entity_map"),
    ("apps/tenants/migrations/0109_seed_pii_type_counters.py", 61, "pii_entity_map"),
}

_NOQA_MARKER = "# noqa: encrypted-predicate"

# Django ORM lookup suffixes that compare against the column's *content*.
# Presence-only lookups (isnull, ...) are intentionally excluded — they stay
# valid after a column flips to ciphertext.
_VALUE_LOOKUPS = (
    "icontains",
    "contains",
    "iexact",
    "exact",
    "istartswith",
    "startswith",
    "iendswith",
    "endswith",
    "iregex",
    "regex",
    "gte",
    "gt",
    "lte",
    "lt",
    "range",
    "in",
)

# Matches the opening paren of a .filter(/.exclude(/Q( call. Paren-depth
# matching (below) then finds the span of that call's argument list.
_CALL_OPENER_RE = re.compile(r"\.filter\(|\.exclude\(|(?<![\w.])Q\(")

_MODEL_PATTERN_CACHE: dict[str, re.Pattern[str]] = {}


def _model_pattern(model: str) -> re.Pattern[str]:
    """Regex that fires when ``model`` is the queryset's model, either as a
    literal ``Model.objects`` chain or (data migrations) a historical model
    bound via ``apps.get_model("<app>", "Model")``."""
    cached = _MODEL_PATTERN_CACHE.get(model)
    if cached is not None:
        return cached
    escaped = re.escape(model)
    pattern = re.compile(
        rf"\b{escaped}\.objects\b|apps\.get_model\(\s*[\"'][^\"']+[\"']\s*,\s*[\"']{escaped}[\"']\s*\)"
    )
    _MODEL_PATTERN_CACHE[model] = pattern
    return pattern


def _column_pattern(column: str) -> re.Pattern[str]:
    """Keyword-predicate regex for ``column``: bare equality or a
    value-comparison lookup suffix, as a dict/call keyword (not ``==``, not
    preceded by ``.`` — that's attribute access, e.g. ``msg.reply_text``,
    which Django kwargs syntax can't express anyway)."""
    escaped = re.escape(column)
    suffixes = "|".join(_VALUE_LOOKUPS)
    return re.compile(rf"(?<![\w.=!<>]){escaped}(?:__(?:{suffixes}))?\s*=(?!=)")


def _matching_close_paren(text: str, open_idx: int) -> int:
    """Index just past the ``)`` that closes ``text[open_idx] == '('``.

    Naive char-depth counting (not a tokenizer) — see module docstring's
    "known limitation" note.
    """
    depth = 1
    i = open_idx + 1
    n = len(text)
    while i < n and depth:
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        i += 1
    return i


def _line_number(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _line_text(text: str, pos: int) -> str:
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    if end == -1:
        end = len(text)
    return text[start:end]


def _scan_file(text: str, relpath: str) -> list[tuple[str, int, str, str]]:
    """Return ``(relpath, line_no, model, column)`` violations in one file's
    text (allowlist/noqa NOT applied here — pure detection)."""
    hits: list[tuple[str, int, str, str]] = []
    seen: set[tuple[int, str, str]] = set()
    lines = text.splitlines()

    for opener in _CALL_OPENER_RE.finditer(text):
        open_paren = text.index("(", opener.start())
        args_start = open_paren + 1
        args_end = _matching_close_paren(text, open_paren)
        span_text = text[args_start:args_end]

        call_line_idx = _line_number(text, opener.start()) - 1
        lookback_start = max(0, call_line_idx - 25)
        lookback_text = "\n".join(lines[lookback_start : call_line_idx + 1])

        for (model, column), _phase in ENCRYPTED_COLUMNS.items():
            if not _model_pattern(model).search(lookback_text):
                continue
            for m in _column_pattern(column).finditer(span_text):
                abs_pos = args_start + m.start()
                line_no = _line_number(text, abs_pos)
                if _NOQA_MARKER in _line_text(text, abs_pos):
                    continue
                key = (line_no, model, column)
                if key in seen:
                    continue
                seen.add(key)
                hits.append((relpath, line_no, model, column))

    return hits


def find_predicate_violations(repo_root: Path = REPO_ROOT) -> list[str]:
    """Pure core — scans ``<repo_root>/apps/**/*.py`` and returns a list of
    human-readable violation strings (empty when clean). Allowlist and
    ``# noqa: encrypted-predicate`` are applied here."""
    repo_root = Path(repo_root)
    apps_dir = repo_root / "apps"
    errors: list[str] = []

    if not apps_dir.is_dir():
        return errors

    for py_file in sorted(apps_dir.rglob("*.py")):
        try:
            text = py_file.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        relpath = py_file.relative_to(repo_root).as_posix()

        for relpath_hit, line_no, model, column in _scan_file(text, relpath):
            if (relpath_hit, line_no, column) in _ALLOWLISTED_SITES:
                continue
            phase = ENCRYPTED_COLUMNS[(model, column)]
            errors.append(
                f"{relpath_hit}:{line_no}: raw DB value-predicate against "
                f"{model}.{column} (registered for {phase} encryption-at-rest, "
                "CONTINUITY_encryption-phase1.md §1). Once this column ships "
                "as AES-GCM ciphertext this predicate will silently stop "
                "matching real content instead of erroring. Route through "
                "apps.crypto once the column flips, or if this predicate is "
                "pre-existing/intentional today, add `# noqa: encrypted-predicate` "
                "on this line."
            )

    return errors


def main() -> int:
    errors = find_predicate_violations()
    if errors:
        print("Encrypted-column predicate guard FAILED:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print(
        f"OK: no new raw DB value-predicates against the {len(ENCRYPTED_COLUMNS)} "
        f"column(s) registered for Phase 2-4 encryption-at-rest "
        f"({len(_ALLOWLISTED_SITES)} pre-existing site(s) allowlisted)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
