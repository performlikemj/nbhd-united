"""Regression coverage for the encrypted-column-predicate CI guard.

The guard (``scripts/check_encrypted_column_predicates.py``) exists so a
future PR that adds a raw DB value-predicate against a column slated for
Phase 2-4 encryption-at-rest (CONTINUITY_encryption-phase1.md §1 PR6) fails
at PR time instead of shipping a query that silently stops matching real
content once that column becomes ciphertext.

These tests pin: (1) a synthetic violation against a registered
``(model, column)`` pair is caught by name/line/model, (2) the real repo's
known pre-existing sites are allowlisted and the guard is green on `main`,
(3) an inline ``# noqa: encrypted-predicate`` suppresses a hit, and (4) two
non-hits the guard must NOT flag: a same-named column on an unregistered
model (model-scoping), and a Python-side (non-queryset) read of the column.
"""

from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

_GUARD_PATH = Path(settings.BASE_DIR) / "scripts" / "check_encrypted_column_predicates.py"


def _load_guard():
    spec = importlib.util.spec_from_file_location("check_encrypted_column_predicates", _GUARD_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _load_guard()


def _write_fixture(tmp_dir: str, relpath: str, content: str) -> Path:
    path = Path(tmp_dir) / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


# Fixture source is built via .format() rather than spelled out literally in
# this file's own text. This test file lives under apps/ and IS itself part
# of the real-repo scan (RepoStateTests below): the guard is a raw-text
# scanner with no notion of "inside a string literal", so a fixture written
# as a literal contiguous "Lesson.objects.filter(...text=...)" string would
# make the guard flag its OWN test file when scanning the real repo. Building
# it from placeholders sidesteps that self-reference without weakening what
# gets scanned inside the temp fixture (the .format() output is the same
# real, contiguous source text the guard has to detect there).
_VIOLATION_TEMPLATE = (
    "from apps.lessons.models import {model}\n"
    "\n"
    "def find_dupes(tenant, needle):\n"
    "    return {model}.objects.{call}(tenant=tenant, {col}=needle)\n"
)


class SyntheticViolationTests(SimpleTestCase):
    """Pure detection logic against a throwaway fake repo tree — none of
    these paths/lines can ever collide with the real allowlist."""

    def test_synthetic_violation_is_caught(self):
        content = _VIOLATION_TEMPLATE.format(model="Lesson", call="filter", col="text")
        with tempfile.TemporaryDirectory() as tmp:
            _write_fixture(tmp, "apps/testapp/probe.py", content)
            errors = guard.find_predicate_violations(Path(tmp))
        self.assertTrue(errors, "a raw predicate on a registered (model, column) must be flagged")
        joined = " ".join(errors)
        self.assertIn("apps/testapp/probe.py:4", joined)
        self.assertIn("Lesson.text", joined)

    def test_noqa_line_passes(self):
        content = _VIOLATION_TEMPLATE.format(model="Lesson", call="filter", col="text")
        content = content.replace("needle)\n", "needle)  " + guard._NOQA_MARKER + "\n")
        with tempfile.TemporaryDirectory() as tmp:
            _write_fixture(tmp, "apps/testapp/probe.py", content)
            errors = guard.find_predicate_violations(Path(tmp))
        self.assertEqual(errors, [], f"a noqa'd line must not be flagged: {errors}")

    def test_allowlisted_site_passes(self):
        """The in-script allowlist is load-bearing: adding a (path, line,
        column) entry suppresses that exact hit and nothing else."""
        content = _VIOLATION_TEMPLATE.format(model="Lesson", call="filter", col="text")
        with tempfile.TemporaryDirectory() as tmp:
            _write_fixture(tmp, "apps/testapp/probe.py", content)
            # Confirm it's a real hit before allowlisting it.
            errors_before = guard.find_predicate_violations(Path(tmp))
            self.assertTrue(errors_before)

            entry = ("apps/testapp/probe.py", 4, "text")
            guard._ALLOWLISTED_SITES.add(entry)
            try:
                errors_after = guard.find_predicate_violations(Path(tmp))
            finally:
                guard._ALLOWLISTED_SITES.discard(entry)
            self.assertEqual(errors_after, [], f"allowlisted site must pass: {errors_after}")

    def test_unregistered_model_with_same_column_name_is_not_flagged(self):
        """Model-scoping: `title` is also a Task/Goal column, neither of
        which is registered for encryption. Mirrors the real
        apps/journal/lifecycle_views.py Task/Goal `title__icontains` sites."""
        content = (
            "from apps.journal.models import {model}\n"
            "\n"
            "def search(tenant, q):\n"
            "    return {model}.objects.filter(tenant=tenant, {col}__icontains=q)\n"
        ).format(model="Task", col="title")
        with tempfile.TemporaryDirectory() as tmp:
            _write_fixture(tmp, "apps/testapp/probe.py", content)
            errors = guard.find_predicate_violations(Path(tmp))
        self.assertEqual(errors, [], f"unregistered model must not be flagged: {errors}")

    def test_python_side_read_is_not_flagged(self):
        """Attribute access / truthy-check code (no .filter/.exclude/Q) must
        never be flagged, even though the column name appears verbatim."""
        content = "def clean(msg):\n    return (msg.{col} or '').strip()\n".format(col="reply_text")
        with tempfile.TemporaryDirectory() as tmp:
            _write_fixture(tmp, "apps/testapp/probe.py", content)
            errors = guard.find_predicate_violations(Path(tmp))
        self.assertEqual(errors, [], f"non-queryset attribute access must not be flagged: {errors}")


class RepoStateTests(SimpleTestCase):
    """The real repo must be clean — this is the assertion CI runs."""

    def test_repo_has_no_unallowlisted_predicates(self):
        errors = guard.find_predicate_violations()
        self.assertEqual(errors, [], f"encrypted-column predicate guard found new violations: {errors}")

    def test_known_preexisting_sites_are_allowlisted(self):
        """Regression pin: the allowlist must still name the real sites the
        guard's introducing PR found (grepped against `main`), not have
        silently emptied out."""
        expected = {
            ("apps/router/push_views.py", 290, "reply_text"),
            ("apps/router/management/commands/audit_proactive_sync.py", 63, "parsed_items"),
            ("apps/lessons/agent_context.py", 91, "galaxy_note"),
            ("apps/journal/extraction.py", 235, "text"),
            ("apps/orchestrator/grounding_probe.py", 85, "markdown"),
            ("apps/journal/migrations/0020_cleanup_nan_daily_stubs.py", 31, "markdown"),
            ("apps/pii/arbiter.py", 356, "pii_entity_map"),
            ("apps/pii/junk_sweep.py", 297, "pii_entity_map"),
            ("apps/pii/management/commands/denylist_degenerate_pii.py", 77, "pii_entity_map"),
            ("apps/tenants/migrations/0109_seed_pii_type_counters.py", 61, "pii_entity_map"),
        }
        self.assertEqual(expected, guard._ALLOWLISTED_SITES)
