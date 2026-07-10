#!/usr/bin/env python3
"""CI guard: no raw-buffer egress of a decrypted ``RedactedStr`` value.

Design: ``CONTINUITY_encryption-phase1.md`` §6 / ``docs/encryption-at-rest-
phase1-status.md`` ("Phase 2 preconditions", item 3). Companion to
``scripts/check_encrypted_column_predicates.py`` — same shape (no-DB pure
functions, ``find_*`` core, ``main()`` exits 1 on findings, in-script
allowlist + inline ``# noqa`` escape, one CI step), different seam.

Why this guard exists
---------------------
``apps.crypto.nolog.RedactedStr`` (a ``str`` subclass) is how a decrypted
plaintext travels through the app without leaking into logs: it redacts EVERY
*format* path — ``str()``, ``repr()``, f-strings, ``"%s" %``, ``.format()`` —
showing only ``‹redacted:Nc›``. ``.reveal()`` is the ONE sanctioned way back
to the real string, used at deliberate egress seams (the API response, the
outbound message body).

But because it subclasses ``str``, every *raw-buffer* consumer that reaches
past the format dunders gets the real plaintext, silently:

    json.dumps([x])      # and therefore DRF's JSONRenderer
    x.encode()           # bytes of the raw plaintext
    "sep".join([x])      # str.join calls str methods on each element
    x + y                # str.__add__ returns a bare str
    x[0:6]               # slicing/indexing returns a bare str
    x.upper() / .strip() / .replace() ...   # all return a bare str
    stream.write(x)      # writes the raw plaintext to the buffer

Before the first encrypted column's *reads* flip on (Phase 2), CI must flag
these vectors so a value that should have been ``.reveal()``-ed at a seam
can't slip out through a raw buffer instead.

What it flags (heuristic, AST-based — precise enough to keep findings actionable)
-------------------------------------------------------------------------------
Scoped to ``apps/**/*.py``. Within a single function (or the module top
level), an identifier is treated as a decrypted value when it is:

  * assigned from a ``decrypt(...)`` / ``box.decrypt(...)`` call  -> a scalar
    ``RedactedStr``;
  * assigned from a ``decrypt_bulk(...)`` / ``box.decrypt_bulk(...)`` call  ->
    a ``list[RedactedStr]``;
  * a parameter / variable annotated ``RedactedStr`` (scalar) or
    ``list[RedactedStr]`` (list);
  * a plain alias of one of the above (``y = x``), a ``for`` / comprehension
    target iterating a tracked list, or ``x = results[i]`` off a tracked list.

It then flags that identifier flowing — on the same line or later in the same
function — into any of: ``json.dumps`` / ``json.dump``, ``.encode(``, a ``+``
concat, ``"".join(``, a subscript/slice, a bare-``str``-returning str method
(``.upper``/``.strip``/``.replace``/...), or ``.write(`` — UNLESS the value is
taken through ``.reveal()`` first, which is the sanctioned egress and is never
flagged.

Format paths are deliberately NOT flagged: ``f"{x}"``, ``"%s" % x``,
``str(x)``, ``"{}".format(x)``, ``logger.info("%s", x)`` all route through the
redacting dunders and are safe by construction.

Deliberate blind spots (the contract: this guard NARROWS the seam; the
``.reveal()``-at-egress convention CLOSES it — see the phase-1 status doc)
----------------------------------------------------------------------------
  * No real type inference. Taint is name-based and flow-INSENSITIVE within a
    function: reassigning a tracked name to a clean value later is not
    modeled, and taint does not cross function/lambda/closure boundaries.
  * Framework serializers that reach the same buffer as ``json.dumps`` are NOT
    matched — DRF ``JSONRenderer``, ``JsonResponse``, ``serializer.data``,
    ``f.write`` behind an ORM/file abstraction. Those seams are closed by the
    ``.reveal()`` convention at the serialization boundary, not here.
  * Only the box's own decrypt API is a source: bare ``decrypt``/
    ``decrypt_bulk`` or ``box.``/``crypto.``-qualified. A differently aliased
    import (``from apps.crypto import box as b; b.decrypt(...)``) or a
    non-``json`` alias (``import json as j; j.dumps(...)``) is not tracked.
  * Bare ``RedactedStr("...")`` construction is NOT a source (the crypto
    module's own proof tests build these to *demonstrate* the leak).
  * Tuple-unpacking assignment and class-body annotations are not tracked.

Escape hatch (same idiom as the predicate guard's ``# noqa``):

    something.write(secret)  # noqa: redactedstr-egress — seam: outbound message body

on the flagged line (the line of the leaking identifier) suppresses it.

Usage: ``python scripts/check_redactedstr_egress.py`` — prints a summary and
exits 0 when clean, 1 with named violations otherwise.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
APPS_DIR = REPO_ROOT / "apps"

_NOQA_MARKER = "# noqa: redactedstr-egress"

# ---------------------------------------------------------------------------
# Allowlist: pre-existing raw-egress sites to grandfather.
#
# EMPTY BY DESIGN. Phase 1 ships the crypto substrate dark with ZERO decrypt
# consumers, so there is no legacy raw-egress to record here. When Phase 2
# flips the first encrypted column's reads on, the default answer to a finding
# is ".reveal() at the seam", not an allowlist entry — reserve this set for a
# reviewed, intentional raw-egress that genuinely cannot use .reveal() (and
# say why in a comment, like the predicate guard's allowlist does).
#
# Entries: (path relative to repo root, 1-indexed line of the leak, identifier)
# ---------------------------------------------------------------------------
_ALLOWLISTED_SITES: set[tuple[str, int, str]] = set()

# The box's decrypt API, reached bare or via these module aliases. `AESGCM(
# dek).decrypt(...)` inside apps/crypto is intentionally NOT matched (its
# receiver is not one of these names), so the crypto internals don't self-flag.
_DECRYPT_MODULE_ALIASES = frozenset({"box", "crypto"})
_SCALAR_DECRYPT = "decrypt"
_LIST_DECRYPT = "decrypt_bulk"

# `str` methods that return a BARE str (or list/tuple of bare str), dropping
# the RedactedStr wrapper and its redaction. `.reveal()` is deliberately
# absent — it is the sanctioned egress. Render dunders (__str__/__format__)
# are not methods and stay redacted, so f-strings / %-format / str() are safe.
_RAW_STR_METHODS = frozenset(
    {
        "encode",
        "upper",
        "lower",
        "strip",
        "lstrip",
        "rstrip",
        "replace",
        "title",
        "capitalize",
        "swapcase",
        "casefold",
        "center",
        "ljust",
        "rjust",
        "zfill",
        "expandtabs",
        "translate",
        "split",
        "rsplit",
        "splitlines",
        "partition",
        "rpartition",
        "removeprefix",
        "removesuffix",
    }
)

# Methods whose ARGUMENT list is a raw buffer the value is written into.
_JSON_SINK_ATTRS = frozenset({"dumps", "dump"})

SCALAR = "scalar"
LIST = "list"


# ---------------------------------------------------------------------------
# Source classification
# ---------------------------------------------------------------------------
def _decrypt_call_kind(node: ast.AST) -> str | None:
    """``SCALAR`` if ``node`` is a ``decrypt(...)`` call, ``LIST`` if it's a
    ``decrypt_bulk(...)`` call (bare or ``box.``/``crypto.``-qualified),
    else ``None``."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Name):
        name = func.id
    elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        if func.value.id not in _DECRYPT_MODULE_ALIASES:
            return None
        name = func.attr
    else:
        return None
    if name == _SCALAR_DECRYPT:
        return SCALAR
    if name == _LIST_DECRYPT:
        return LIST
    return None


def _annotation_kind(ann: ast.AST | None) -> str | None:
    """``SCALAR`` for ``RedactedStr`` / ``RedactedStr | None`` /
    ``Optional[RedactedStr]``; ``LIST`` for ``list[RedactedStr]`` and the like;
    else ``None``."""
    if ann is None:
        return None
    if isinstance(ann, ast.Name):
        return SCALAR if ann.id == "RedactedStr" else None
    if isinstance(ann, ast.Attribute):
        return SCALAR if ann.attr == "RedactedStr" else None
    if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
        return SCALAR if ann.value.strip() == "RedactedStr" else None
    if isinstance(ann, ast.BinOp) and isinstance(ann.op, ast.BitOr):
        # `RedactedStr | None`, `list[RedactedStr] | None`
        for side in (ann.left, ann.right):
            kind = _annotation_kind(side)
            if kind is not None:
                return kind
        return None
    if isinstance(ann, ast.Subscript):
        container = ann.value
        inner = _annotation_kind(ann.slice)
        if inner is None:
            return None
        if isinstance(container, ast.Name):
            if container.id in ("list", "List", "Sequence", "Iterable", "tuple", "Tuple"):
                return LIST
            if container.id == "Optional":
                return inner
        if isinstance(container, ast.Attribute) and container.attr in (
            "List",
            "Sequence",
            "Iterable",
            "Tuple",
            "Optional",
        ):
            return LIST if container.attr != "Optional" else inner
        return None
    return None


# ---------------------------------------------------------------------------
# Scope walking
# ---------------------------------------------------------------------------
_NESTED_SCOPE_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _own_nodes(scope: ast.AST) -> list[ast.AST]:
    """All descendants of ``scope`` that belong to *this* lexical scope —
    recursion stops at nested function/lambda/class boundaries (each analyzed
    as its own scope). Comprehensions are treated as same-scope so
    ``[r for r in results]`` is analyzable inline."""
    out: list[ast.AST] = []

    def rec(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _NESTED_SCOPE_TYPES):
                continue
            out.append(child)
            rec(child)

    rec(scope)
    return out


def _iter_scopes(tree: ast.AST):
    yield tree  # module
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _param_taint(scope: ast.AST) -> tuple[set[str], set[str]]:
    scalar: set[str] = set()
    lst: set[str] = set()
    if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return scalar, lst
    args = scope.args
    every = list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
    if args.vararg:
        every.append(args.vararg)
    if args.kwarg:
        every.append(args.kwarg)
    for a in every:
        kind = _annotation_kind(a.annotation)
        if kind == SCALAR:
            scalar.add(a.arg)
        elif kind == LIST:
            lst.add(a.arg)
    return scalar, lst


def _target_names(target: ast.AST) -> list[str]:
    """Simple single-``Name`` targets only (tuple/list unpacking is a
    documented blind spot)."""
    if isinstance(target, ast.Name):
        return [target.id]
    return []


def _value_kind(value: ast.AST, scalar: set[str], lst: set[str]) -> str | None:
    kind = _decrypt_call_kind(value)
    if kind is not None:
        return kind
    if isinstance(value, ast.Name):
        if value.id in scalar:
            return SCALAR
        if value.id in lst:
            return LIST
    # `x = results[i]` off a tracked list -> a scalar element.
    if isinstance(value, ast.Subscript) and isinstance(value.value, ast.Name):
        if value.value.id in lst:
            return SCALAR
    return None


def _iter_yields_scalar(iter_node: ast.AST, lst: set[str]) -> bool:
    """Does iterating ``iter_node`` yield scalar ``RedactedStr`` elements?"""
    if isinstance(iter_node, ast.Name) and iter_node.id in lst:
        return True
    return _decrypt_call_kind(iter_node) == LIST


def _collect_taint(scope: ast.AST, own: list[ast.AST]) -> tuple[set[str], set[str]]:
    """Flow-insensitive fixpoint: which names in this scope hold a scalar
    ``RedactedStr`` vs a ``list[RedactedStr]``."""
    scalar, lst = _param_taint(scope)

    changed = True
    while changed:
        changed = False
        before = (len(scalar), len(lst))
        for node in own:
            if isinstance(node, ast.AnnAssign):
                kind = _annotation_kind(node.annotation)
                if kind is None and node.value is not None:
                    kind = _value_kind(node.value, scalar, lst)
                names = _target_names(node.target)
                _apply(kind, names, scalar, lst)
            elif isinstance(node, ast.Assign):
                kind = _value_kind(node.value, scalar, lst)
                if kind is not None:
                    for tgt in node.targets:
                        _apply(kind, _target_names(tgt), scalar, lst)
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
                if _iter_yields_scalar(node.iter, lst):
                    _apply(SCALAR, _target_names(node.target), scalar, lst)
        if (len(scalar), len(lst)) != before:
            changed = True
    return scalar, lst


def _apply(kind: str | None, names: list[str], scalar: set[str], lst: set[str]) -> None:
    if kind == SCALAR:
        scalar.update(names)
    elif kind == LIST:
        lst.update(names)


# ---------------------------------------------------------------------------
# Sink detection
# ---------------------------------------------------------------------------
def _excluded_name_ids(subtree: ast.AST) -> set[int]:
    """``id()`` of ``Name`` nodes that don't count as raw egress:

    * ``<name>.reveal()`` — the sanctioned egress back to plaintext;
    * a comprehension's ``.iter`` (``[r.reveal() for r in rs]`` iterates the
      list but serializes its already-revealed elements, not the list raw).
    """
    excluded: set[int] = set()
    for n in ast.walk(subtree):
        if isinstance(n, ast.Attribute) and n.attr == "reveal" and isinstance(n.value, ast.Name):
            excluded.add(id(n.value))
        elif isinstance(n, ast.comprehension) and isinstance(n.iter, ast.Name):
            excluded.add(id(n.iter))
    return excluded


def _tainted_names_in(subtree: ast.AST, scalar: set[str], lst: set[str]) -> list[ast.Name]:
    """Tainted ``Name`` nodes *read* (``ast.Load``) inside ``subtree``,
    skipping ``.reveal()``-ed values and comprehension iterators. Store-context
    targets (a comprehension/``for`` bind of ``r``) are not raw uses."""
    excluded = _excluded_name_ids(subtree)
    hits: list[ast.Name] = []
    for n in ast.walk(subtree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and id(n) not in excluded:
            if n.id in scalar or n.id in lst:
                hits.append(n)
    return hits


def _find_sinks(own: list[ast.AST], scalar: set[str], lst: set[str]) -> list[tuple[int, str, str]]:
    """``(lineno, identifier, sink description)`` for every raw-buffer egress
    of a tracked value in this scope."""
    found: list[tuple[int, str, str]] = []

    def scalar_name(node: ast.AST) -> ast.Name | None:
        if isinstance(node, ast.Name) and node.id in scalar:
            return node
        return None

    for node in own:
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                # value.<raw str method>(...)
                if func.attr in _RAW_STR_METHODS:
                    tgt = scalar_name(func.value)
                    if tgt is not None:
                        found.append((tgt.lineno, tgt.id, f".{func.attr}() returns a bare str"))
                        continue
                # json.dumps(...) / json.dump(...)
                if func.attr in _JSON_SINK_ATTRS and isinstance(func.value, ast.Name) and func.value.id == "json":
                    for name in _tainted_names_in(node, scalar, lst):
                        found.append((name.lineno, name.id, f"json.{func.attr}() serializes the raw plaintext"))
                    continue
                # "sep".join(iterable)
                if func.attr == "join":
                    for name in _tainted_names_in(node, scalar, lst):
                        found.append((name.lineno, name.id, "str.join() concatenates raw plaintext"))
                    continue
                # stream.write(value)
                if func.attr == "write":
                    for arg in node.args:
                        for name in _tainted_names_in(arg, scalar, lst):
                            if name.id in scalar:
                                found.append((name.lineno, name.id, ".write() emits the raw plaintext"))
                    continue
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            for operand in (node.left, node.right):
                tgt = scalar_name(operand)
                if tgt is not None:
                    found.append((tgt.lineno, tgt.id, "+ concatenation returns a bare str"))
        elif isinstance(node, ast.Subscript):
            tgt = scalar_name(node.value)
            if tgt is not None:
                found.append((tgt.lineno, tgt.id, "slice/index returns a bare str"))

    return found


# ---------------------------------------------------------------------------
# File / repo drivers
# ---------------------------------------------------------------------------
def _scan_file(text: str, relpath: str) -> list[tuple[str, int, str, str]]:
    """``(relpath, lineno, identifier, sink)`` egress hits in one file, with
    inline ``# noqa`` suppression applied. Allowlist is NOT applied here."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []

    lines = text.splitlines()
    hits: dict[tuple[int, str], tuple[str, int, str, str]] = {}

    for scope in _iter_scopes(tree):
        own = _own_nodes(scope)
        scalar, lst = _collect_taint(scope, own)
        if not scalar and not lst:
            continue
        for lineno, name, sink in _find_sinks(own, scalar, lst):
            line_text = lines[lineno - 1] if 0 < lineno <= len(lines) else ""
            if _NOQA_MARKER in line_text:
                continue
            hits.setdefault((lineno, name), (relpath, lineno, name, sink))

    return list(hits.values())


def find_egress_violations(repo_root: Path = REPO_ROOT) -> list[str]:
    """Pure core — scans ``<repo_root>/apps/**/*.py`` and returns a list of
    human-readable violation strings (empty when clean). Allowlist and inline
    ``# noqa: redactedstr-egress`` are applied here."""
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

        for relpath_hit, lineno, name, sink in _scan_file(text, relpath):
            if (relpath_hit, lineno, name) in _ALLOWLISTED_SITES:
                continue
            errors.append(
                f"{relpath_hit}:{lineno}: '{name}' is a decrypted RedactedStr "
                f"value flowing into a raw buffer ({sink}) — this bypasses "
                "RedactedStr's redaction and emits the real plaintext. Call "
                "`.reveal()` at the deliberate egress seam instead, or if this "
                "IS the sanctioned egress add "
                "`# noqa: redactedstr-egress — seam: <name>` on this line "
                "(CONTINUITY_encryption-phase1.md §6)."
            )

    return errors


def main() -> int:
    errors = find_egress_violations()
    if errors:
        print("RedactedStr raw-buffer egress guard FAILED:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print(
        "OK: no decrypted RedactedStr values flow into a raw buffer "
        "(json.dumps/.encode/concat/join/slice/str-method/.write) without "
        f"`.reveal()` at the seam ({len(_ALLOWLISTED_SITES)} allowlisted site(s))."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
