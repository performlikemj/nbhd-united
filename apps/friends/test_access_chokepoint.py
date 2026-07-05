"""Architectural CI chokepoint — the load-bearing guard for the Neighborhood.

Django connects to Postgres as a **BYPASSRLS superuser**, so RLS is not a
tenant backstop today: cross-tenant isolation is 100% the Python filters in
:mod:`apps.friends.access` until the PR8 ``FORCE ROW LEVEL SECURITY``
hardening. One missing edge/tenant filter leaks another user's private data
with no DB net. This test contains that risk to a single audited module by
**failing the build** if any friends module (or a friends runtime view)
reaches a cross-tenant content manager without going through the accessor.

Two rules, enforced by walking each module's AST (robust — not a brittle
string grep):

1. ``SharedLesson`` / ``FriendMessage`` / ``SharedGoal`` / ``LessonShareGrant``
   ``.objects`` may be touched ONLY in ``apps/friends/access.py``.
2. ``Lesson.objects`` may NOT be touched anywhere under ``apps/friends/`` —
   not even in ``access.py`` — because friend paths read the frozen
   ``SharedLesson`` snapshot, never the raw ``Lesson`` corpus.

The same rule (1) covers friends runtime views, which extend
``apps/integrations/runtime_views.py`` (design §3.5).

It is **green now** (those cross-tenant models don't exist yet) and bites
automatically as ``SharedLesson`` etc. land in PR2+ and any new friends code
queries them directly.

Scope: production modules only. Test modules (``tests.py`` / ``test_*.py`` /
``tests/`` packages) and ``migrations/`` are excluded — they legitimately
build fixtures via ``.objects.create`` and are not a cross-tenant read path.
Limitation: matches the model class by name at the ``.objects`` site
(``SharedLesson.objects`` / ``models.SharedLesson.objects``); a deliberately
aliased import (``from .models import SharedLesson as X``) would evade it —
don't do that in a friends module.
"""

from __future__ import annotations

import ast
from pathlib import Path

from django.test import SimpleTestCase

# Cross-tenant, frozen-content models. Manager access allowed ONLY in access.py.
CROSS_TENANT_MODELS = frozenset({"SharedLesson", "FriendMessage", "SharedGoal", "LessonShareGrant"})

# The raw lesson corpus — forbidden anywhere under apps/friends/ (incl. access.py).
RAW_LESSON_MODEL = "Lesson"

REPO_ROOT = Path(__file__).resolve().parents[2]
FRIENDS_DIR = REPO_ROOT / "apps" / "friends"
ACCESS_MODULE = FRIENDS_DIR / "access.py"

# Friends runtime views live here (design §3.5). Scanning the whole module is
# safe: only the four CROSS_TENANT_MODELS trip it, so the file's unrelated
# ``.objects`` usage (Lesson, Tenant, …) is untouched.
RUNTIME_VIEW_FILES = [REPO_ROOT / "apps" / "integrations" / "runtime_views.py"]


def _manager_accesses(source: str) -> set[str]:
    """Return every model name ``M`` for which an ``M.objects`` (or
    ``pkg.M.objects``) attribute access appears in ``source``."""
    found: set[str] = set()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "objects":
            base = node.value
            if isinstance(base, ast.Name):
                found.add(base.id)  # SharedLesson.objects
            elif isinstance(base, ast.Attribute):
                found.add(base.attr)  # models.SharedLesson.objects
    return found


def _is_excluded(rel: Path) -> bool:
    """True for migrations + test modules (not a cross-tenant read path)."""
    if "migrations" in rel.parts:
        return True
    if "tests" in rel.parts:
        return True
    name = rel.name
    return name == "tests.py" or name.startswith("test_")


def _friends_modules() -> list[tuple[Path, Path]]:
    out: list[tuple[Path, Path]] = []
    for path in sorted(FRIENDS_DIR.rglob("*.py")):
        rel = path.relative_to(FRIENDS_DIR)
        if _is_excluded(rel):
            continue
        out.append((path, rel))
    return out


class AccessChokepointTest(SimpleTestCase):
    """Static AST analysis — no DB required."""

    def test_cross_tenant_managers_only_in_access(self):
        offenders: list[tuple[str, str]] = []
        for path, rel in _friends_modules():
            if path == ACCESS_MODULE:
                continue
            for name in sorted(_manager_accesses(path.read_text()) & CROSS_TENANT_MODELS):
                offenders.append((str(rel), f"{name}.objects"))
        self.assertEqual(
            offenders,
            [],
            "Cross-tenant model manager(s) used outside apps/friends/access.py. "
            "Every cross-tenant read MUST route through apps/friends/access.py "
            "(are_neighbors / assert_neighbors / shared_star_qs / "
            "assert_can_write) — Django is BYPASSRLS, so this filter is the only "
            f"thing standing between tenants. Offenders: {offenders}",
        )

    def test_raw_lesson_never_touched_under_friends(self):
        offenders: list[tuple[str, str]] = []
        for _path, rel in _friends_modules():
            if RAW_LESSON_MODEL in _manager_accesses(_path.read_text()):
                offenders.append((str(rel), "Lesson.objects"))
        self.assertEqual(
            offenders,
            [],
            "Lesson.objects referenced under apps/friends/. Friend paths must "
            "read the frozen, PII-scrubbed SharedLesson snapshot — never the raw "
            f"Lesson corpus (which stores real names). Offenders: {offenders}",
        )

    def test_runtime_views_broker_cross_tenant_through_accessor(self):
        offenders: list[tuple[str, str]] = []
        for path in RUNTIME_VIEW_FILES:
            if not path.exists():
                continue
            for name in sorted(_manager_accesses(path.read_text()) & CROSS_TENANT_MODELS):
                offenders.append((str(path.relative_to(REPO_ROOT)), f"{name}.objects"))
        self.assertEqual(
            offenders,
            [],
            "A runtime view queries a cross-tenant friends model directly. "
            "Runtime endpoints are per-tenant-keyed; cross-tenant data must be "
            "brokered by Django through apps/friends/access.py after the edge "
            f"check. Offenders: {offenders}",
        )

    def test_accessor_module_present(self):
        # Guards against the chokepoint quietly passing because access.py was
        # renamed/removed — the exclusion above would then be vacuous.
        self.assertTrue(ACCESS_MODULE.exists(), "apps/friends/access.py is missing")
