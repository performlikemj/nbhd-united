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

How it works — three detectors:

  A. **ORM value-predicates.** Scans ``apps/**/*.py`` for ``.filter(``,
     ``.exclude(``, and ``Q(`` calls (paren-depth matched, so nested calls
     are each also inspected). Inside each call's argument list, it looks for
     a keyword predicate against a registered column — a value-comparison
     lookup suffix (``__icontains``, ``__gt``, ``__exact``, ...) or bare
     equality (``col=value``). To avoid false positives from same-named
     columns on unrelated models (``title``, ``text``, ``name`` are common —
     see ``Task.title``, ``Goal.title``), a hit only counts when the model
     name appears nearby (within ~25 lines above the call, e.g.
     ``Document.objects.filter(...)`` or, in a data migration,
     ``apps.get_model("journal", "Document")``).
  B. **JSON key-path predicates** (Phase 3). A registered ``JSONField`` column
     (``_JSON_COLUMNS``) seals as one opaque envelope, so ANY ``__``-suffixed
     key-path breaks — not just the scalar lookups. Those columns are scanned
     with a broadened pattern (``col__anything=``) so ``detail_json__key=`` is
     caught too, via ``_json_column_pattern``.
  C. **Admin ``search_fields``** (Phase 3). Staff admin search issues a raw
     ``__icontains`` OR across ``ModelAdmin.search_fields``, so it silently
     breaks once a listed column is ciphertext. ``_scan_admin_search_fields``
     (``admin.py`` only) maps each ``search_fields`` entry to its
     ``@admin.register`` model and flags a registered column named directly, or
     a relation whose terminal segment is a registered column name.

Three escape hatches, by design:

  1. **Predicate allowlist** (``_ALLOWLISTED_SITES``) — known pre-existing
     ORM/JSON predicate sites, each carrying the ladder PR that resolves it.
     Do NOT edit product code to appease this guard; the allowlist is the
     record of "this predicate predates the flip, revisit when the column
     encrypts."
  2. **Admin-search allowlist** (``_ALLOWLISTED_ADMIN_SEARCH_FIELDS``) — same,
     for ``search_fields`` entries.
  3. **Inline** ``# noqa: encrypted-predicate`` on the offending line — for
     a new PR that has a deliberate, reviewed reason to touch a registered
     column before its phase lands.

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
    # Phase 3 — journal group (+ lessons + insights + core, one flag pair) and
    # fuel free-text (its own flag pair). AAD constants live in each app's
    # ``enc_columns.py``; the sidecar columns ship DARK in PR-1.
    # Document.markdown/title are Phase 3 but DEFERRED to the search-coupled 3b
    # sub-phase (blind index) — kept registered so the guard still polices them.
    ("Document", "markdown"): "phase3",
    ("Document", "title"): "phase3",
    # -- journal app --
    ("PendingExtraction", "text"): "phase3",
    ("Goal", "title"): "phase3",
    ("Goal", "description"): "phase3",
    ("Purpose", "statement"): "phase3",
    ("Purpose", "evidence"): "phase3",
    ("Task", "title"): "phase3",
    ("Task", "description"): "phase3",
    ("PendingTaskAction", "evidence"): "phase3",
    ("DocumentChunk", "text"): "phase3",
    ("DocumentIngestion", "original_filename"): "phase3",
    ("DocumentIngestionArtifact", "content_excerpt"): "phase3",
    ("DailyNote", "markdown"): "phase3",
    ("Session", "summary"): "phase3",
    ("Session", "accomplishments"): "phase3",
    ("Session", "blockers"): "phase3",
    ("Session", "next_steps"): "phase3",
    # -- lessons app --
    ("Lesson", "text"): "phase3",
    ("Lesson", "galaxy_note"): "phase3",
    ("Lesson", "context"): "phase3",
    ("TutoringSession", "messages"): "phase3",
    ("TutoringSession", "connections_made"): "phase3",
    ("StarJournalEntry", "text"): "phase3",
    # -- insights app --
    ("AssistantInsight", "statement"): "phase3",
    ("AssistantInsight", "user_responses"): "phase3",
    # -- core app --
    ("CoreProfile", "additional_context"): "phase3",
    ("MeditationSession", "feedback_note"): "phase3",
    ("MeditationSession", "title"): "phase3",
    ("MeditationSession", "theme"): "phase3",
    ("MeditationSession", "manifest"): "phase3",
    ("MeditationSession", "guidance_text"): "phase3",
    # -- fuel app (free-text set only; numeric body-metrics DEFER to 3b) --
    ("Workout", "notes"): "phase3",
    ("Workout", "notes_thread"): "phase3",
    ("Workout", "detail_json"): "phase3",
    ("Workout", "skip_reason"): "phase3",
    ("Workout", "activity"): "phase3",
    ("WorkoutPlan", "notes"): "phase3",
    ("WorkoutPlan", "objective"): "phase3",
    ("WorkoutPlan", "name"): "phase3",
    ("FuelProfile", "additional_context"): "phase3",
    ("FuelProfile", "limitations"): "phase3",
    ("WorkoutTemplate", "name"): "phase3",
    ("WorkoutTemplate", "detail_json"): "phase3",
    ("SleepLog", "notes"): "phase3",
    # Phase 4 — PII map
    ("Tenant", "pii_entity_map"): "phase4",
    ("Tenant", "pii_denylist"): "phase4",
}

# JSONField columns among the registered set. A ``JSONField`` seals as one opaque
# ``json.dumps(...)`` envelope (plan §1.5), so ANY key-path / transform predicate
# against it (``col__somekey=``, ``col__0=``, ``col__contains=``, ``col__has_key=``)
# silently stops matching once the column is ciphertext — not just the scalar
# lookups in ``_VALUE_LOOKUPS``. These columns are scanned with a broadened
# pattern (``col(__\w+)*=``) so JSON key-path predicates are caught too.
_JSON_COLUMNS: set[tuple[str, str]] = {
    ("Purpose", "evidence"),
    ("Session", "accomplishments"),
    ("Session", "blockers"),
    ("Session", "next_steps"),
    ("TutoringSession", "messages"),
    ("TutoringSession", "connections_made"),
    ("AssistantInsight", "user_responses"),
    ("MeditationSession", "manifest"),
    ("Workout", "notes_thread"),
    ("Workout", "detail_json"),
    ("FuelProfile", "limitations"),
    ("WorkoutTemplate", "detail_json"),
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
    # ── Phase 3 pre-existing predicate sites (plan §7.8) ──────────────────────
    # Each is a raw predicate on a column this PR registers for Phase 3. No
    # trivially-equivalent post-encryption rewrite exists (equality/startswith on
    # ciphertext never matches), so each is allowlisted with the ladder PR that
    # resolves it — do NOT weaken the guard to appease it.
    # Goal.title / Task.title — the Siri EntityQuery / Shortcuts ``?q=``
    # disambiguation picker. A real search feature; the read-flip PR
    # (feat/enc-p3-journal-read) rewrites it to a decrypt-and-scan match, not a
    # sidecar boolean.
    ("apps/journal/lifecycle_views.py", 273, "title"),
    ("apps/journal/lifecycle_views.py", 316, "title"),
    # Task.title — one-time legacy Document→typed-model migration dedup, runs on
    # plaintext during migration; resolved by feat/enc-p3-journal-read (or retired
    # with the legacy Document path).
    ("apps/journal/management/commands/migrate_documents_to_typed_models.py", 124, "title"),
    # Lesson.context — dedup management command tags goal-derived lessons by a
    # ``context`` prefix; resolved in feat/enc-p3-journal-read (decrypt-and-scan).
    ("apps/lessons/management/commands/dedup_lessons.py", 51, "context"),
    # WorkoutPlan.name — active-plan idempotency dedup (double-submit returns the
    # existing plan instead of duplicating its calendar). Equality on ``name`` can't
    # match ciphertext; feat/enc-p3-fuel-read reworks dedup to (tenant, start_date,
    # status) + decrypt-and-compare.
    ("apps/fuel/runtime_views.py", 1460, "name"),
    ("apps/fuel/views.py", 1349, "name"),
    # ── Phase 3 pre-existing TEST predicates (assert the plaintext dedup behavior
    # the product sites above implement; updated alongside feat/enc-p3-*-read). ──
    ("apps/fuel/tests.py", 3014, "activity"),
    ("apps/fuel/tests.py", 4319, "name"),
    ("apps/fuel/tests.py", 4333, "name"),
    ("apps/fuel/tests.py", 4348, "name"),
    ("apps/fuel/tests_audit_adv_A33.py", 150, "name"),
    ("apps/journal/test_dedup.py", 193, "title"),
    ("apps/journal/test_dedup.py", 205, "title"),
}

# ---------------------------------------------------------------------------
# Admin-search allowlist: ``ModelAdmin.search_fields`` entries that target a
# registered column (directly, or via a relation whose terminal segment is a
# registered column). Staff admin search issues a raw ``__icontains`` OR across
# these fields, so it silently breaks the moment the column flips to ciphertext
# (plan §7.8). Each is allowlisted with the ladder PR that resolves it — the
# read-flip PR either drops the encrypted field from ``search_fields`` or accepts
# admin search over that column goes dark. Do NOT quietly delete a field to dodge
# the guard.
#
# Entries: (path relative to repo root, 1-indexed line of the search_field, field)
# ---------------------------------------------------------------------------
_ALLOWLISTED_ADMIN_SEARCH_FIELDS: set[tuple[str, int, str]] = {
    # core.MeditationSession — feat/enc-p3-journal-read
    ("apps/core/admin.py", 18, "title"),
    ("apps/core/admin.py", 18, "theme"),
    # insights.AssistantInsight — feat/enc-p3-journal-read
    ("apps/insights/admin.py", 31, "statement"),
    # lessons.Lesson — feat/enc-p3-journal-read
    ("apps/lessons/admin.py", 19, "text"),
    ("apps/lessons/admin.py", 19, "context"),
    # lessons.LessonConnection — search spans from_lesson/to_lesson → Lesson.text
    ("apps/lessons/admin.py", 35, "from_lesson__text"),
    ("apps/lessons/admin.py", 36, "to_lesson__text"),
    # lessons.TutoringSession — messages (own) + star → Lesson.text
    ("apps/lessons/admin.py", 53, "star__text"),
    ("apps/lessons/admin.py", 53, "messages"),
    # lessons.StarJournalEntry — text (own) + star → Lesson.text
    ("apps/lessons/admin.py", 68, "text"),
    ("apps/lessons/admin.py", 68, "star__text"),
    # fuel.Workout / WorkoutTemplate / WorkoutPlan — feat/enc-p3-fuel-read
    ("apps/fuel/admin.py", 20, "activity"),
    ("apps/fuel/admin.py", 43, "name"),
    ("apps/fuel/admin.py", 80, "name"),
}

# Every registered column *name* (across models) — used to flag a spanning
# ``search_fields`` entry (``star__text``) whose terminal segment is an encrypted
# column on the (statically-unresolvable) relation target. Fail-safe: over-flag
# and allowlist, never silently miss.
_ALL_REGISTERED_COLUMN_NAMES: frozenset[str] = frozenset(col for _model, col in ENCRYPTED_COLUMNS)

_NOQA_MARKER = "# noqa: encrypted-predicate"

# ``@admin.register(Model, ...)`` and the ``search_fields = (/[`` opener.
_ADMIN_REGISTER_RE = re.compile(r"@admin\.register\(([^)]*)\)")
_SEARCH_FIELDS_OPENER_RE = re.compile(r"\bsearch_fields\s*=\s*[([]")
_ADMIN_FIELD_LITERAL_RE = re.compile(r"""["']([A-Za-z_][A-Za-z0-9_]*(?:__[A-Za-z0-9_]+)*)["']""")

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


def _json_column_pattern(column: str) -> re.Pattern[str]:
    """Broadened predicate regex for a ``JSONField`` column: bare equality OR any
    ``__``-suffixed key-path / transform / lookup (``col__anything=``, incl.
    ``col__key__nested=``, ``col__0=``, ``col__has_key=``). A JSON column seals as
    one opaque envelope, so EVERY key-path predicate breaks once it is ciphertext
    — not just the scalar lookups in ``_VALUE_LOOKUPS`` (plan §1.5, §7.8)."""
    escaped = re.escape(column)
    return re.compile(rf"(?<![\w.=!<>]){escaped}(?:__\w+)*\s*=(?!=)")


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
            col_re = _json_column_pattern(column) if (model, column) in _JSON_COLUMNS else _column_pattern(column)
            for m in col_re.finditer(span_text):
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


def _scan_admin_search_fields(text: str, relpath: str) -> list[tuple[str, int, str, str, str]]:
    """Return ``(relpath, line_no, admin_model, field, phase)`` for every
    ``ModelAdmin.search_fields`` entry that targets a registered column — bare
    (``"statement"``) on the admin's ``@admin.register`` model, or spanning a
    relation whose terminal segment names a registered column (``"star__text"``).
    Allowlist NOT applied here (pure detection). Runs on ``admin.py`` files only."""
    hits: list[tuple[str, int, str, str, str]] = []

    registers: list[tuple[int, list[str]]] = []
    for rm in _ADMIN_REGISTER_RE.finditer(text):
        models = [t.strip() for t in rm.group(1).split(",") if t.strip()]
        registers.append((_line_number(text, rm.start()) - 1, models))
    registers.sort()

    def _models_before(line_idx: int) -> list[str]:
        chosen: list[str] = []
        for reg_line, models in registers:
            if reg_line < line_idx:
                chosen = models
            else:
                break
        return chosen

    for sf in _SEARCH_FIELDS_OPENER_RE.finditer(text):
        open_idx = sf.end() - 1
        bracket = text[open_idx]
        close = ")" if bracket == "(" else "]"
        depth = 1
        i = open_idx + 1
        n = len(text)
        while i < n and depth:
            if text[i] == bracket:
                depth += 1
            elif text[i] == close:
                depth -= 1
            i += 1
        span = text[open_idx + 1 : i - 1]
        admin_models = _models_before(_line_number(text, sf.start()) - 1)
        if not admin_models:
            continue

        for lm in _ADMIN_FIELD_LITERAL_RE.finditer(span):
            field = lm.group(1)
            line_no = _line_number(text, open_idx + 1 + lm.start())
            if "__" not in field:
                for model in admin_models:
                    if (model, field) in ENCRYPTED_COLUMNS:
                        hits.append((relpath, line_no, model, field, ENCRYPTED_COLUMNS[(model, field)]))
                        break
            else:
                terminal = field.split("__")[-1]
                if terminal in _ALL_REGISTERED_COLUMN_NAMES:
                    phase = next(p for (_m, c), p in ENCRYPTED_COLUMNS.items() if c == terminal)
                    hits.append((relpath, line_no, admin_models[0], field, phase))

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

        if relpath.endswith("admin.py"):
            for relpath_hit, line_no, model, field, phase in _scan_admin_search_fields(text, relpath):
                if (relpath_hit, line_no, field) in _ALLOWLISTED_ADMIN_SEARCH_FIELDS:
                    continue
                errors.append(
                    f"{relpath_hit}:{line_no}: ModelAdmin.search_fields entry "
                    f"'{field}' targets {model}'s encrypted column (registered for "
                    f"{phase} encryption-at-rest). Admin search issues a raw "
                    "__icontains across search_fields, so it silently stops "
                    "matching once the column is ciphertext. Drop the field from "
                    "search_fields when the column flips, or allowlist it in "
                    "_ALLOWLISTED_ADMIN_SEARCH_FIELDS with the resolving ladder PR."
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
        f"({len(_ALLOWLISTED_SITES)} pre-existing predicate site(s) + "
        f"{len(_ALLOWLISTED_ADMIN_SEARCH_FIELDS)} admin search_fields site(s) allowlisted)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
