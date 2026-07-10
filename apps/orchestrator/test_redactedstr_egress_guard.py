"""Regression coverage for the RedactedStr raw-buffer egress CI guard.

The guard (``scripts/check_redactedstr_egress.py``) exists so a future PR that
lets a decrypted ``RedactedStr`` slip through a raw-buffer consumer
(``json.dumps``, ``.encode``, ``+`` concat, ``"".join``, slicing, a bare-str
str method, ``.write``) — instead of ``.reveal()``-ing it at a deliberate
egress seam — fails at PR time. This is the Phase-2 precondition (item 3 in
``docs/encryption-at-rest-phase1-status.md``): before the first encrypted
column's reads flip on, CI narrows this seam.

These tests pin: (1) each raw-buffer vector against a decrypted value is
caught, (2) the sanctioned ``.reveal()`` egress and the redacting format paths
(f-string, ``%s``, ``str()``) are NOT flagged, (3) an inline
``# noqa: redactedstr-egress`` suppresses a hit, (4) the real repo is green
and the allowlist is empty by design, and (5) key blind-spot boundaries hold
(bare ``RedactedStr(...)`` construction and a non-box ``.decrypt`` are not
sources).

Unlike the predicate guard's tests, fixtures here are spelled out literally:
the egress guard is AST-based, so a fixture written as a Python *string
constant* is never parsed as code and cannot make the guard flag this test
file when it scans the real repo (RepoStateTests below).
"""

from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

_GUARD_PATH = Path(settings.BASE_DIR) / "scripts" / "check_redactedstr_egress.py"


def _load_guard():
    spec = importlib.util.spec_from_file_location("check_redactedstr_egress", _GUARD_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _load_guard()


def _write_fixture(tmp_dir: str, content: str, relpath: str = "apps/testapp/probe.py") -> Path:
    path = Path(tmp_dir) / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _run(content: str) -> list[str]:
    with tempfile.TemporaryDirectory() as tmp:
        _write_fixture(tmp, content)
        return guard.find_egress_violations(Path(tmp))


# A decrypted scalar bound from box.decrypt(); the {sink} line is what varies.
_SCALAR_SOURCE = "import json\n\n\ndef leak(tenant, blob):\n    x = box.decrypt(tenant, 't', 'c', blob)\n    {sink}\n"


class RawBufferSinkTests(SimpleTestCase):
    """Every raw-buffer consumer of a decrypted scalar must be flagged, by
    identifier and line."""

    def _assert_flags_x_on_line_6(self, sink: str):
        errors = _run(_SCALAR_SOURCE.format(sink=sink))
        self.assertTrue(errors, f"expected a finding for sink `{sink}`, got none")
        joined = " ".join(errors)
        self.assertIn("apps/testapp/probe.py:6", joined)
        self.assertIn("'x'", joined)

    def test_json_dumps_is_caught(self):
        self._assert_flags_x_on_line_6("return json.dumps([x])")

    def test_encode_is_caught(self):
        self._assert_flags_x_on_line_6("return x.encode('utf-8')")

    def test_concat_is_caught(self):
        self._assert_flags_x_on_line_6("return 'prefix:' + x")

    def test_join_is_caught(self):
        self._assert_flags_x_on_line_6("return ''.join([x])")

    def test_slice_is_caught(self):
        self._assert_flags_x_on_line_6("return x[0:6]")

    def test_str_method_is_caught(self):
        self._assert_flags_x_on_line_6("return x.upper()")

    def test_write_is_caught(self):
        content = "def leak(tenant, blob, stream):\n    x = box.decrypt(tenant, 't', 'c', blob)\n    stream.write(x)\n"
        errors = _run(content)
        self.assertTrue(errors)
        self.assertIn("apps/testapp/probe.py:3", " ".join(errors))
        self.assertIn("'x'", " ".join(errors))


class SourceTrackingTests(SimpleTestCase):
    """The identifier reaches a sink through the tracked provenance forms."""

    def test_bare_decrypt_call_is_a_source(self):
        content = "def leak(tenant, blob):\n    x = decrypt(tenant, 't', 'c', blob)\n    return x.encode()\n"
        self.assertTrue(_run(content))

    def test_decrypt_bulk_json_dumps_is_caught(self):
        content = (
            "import json\n\n\n"
            "def leak(tenant, blobs):\n"
            "    rows = box.decrypt_bulk(tenant, 't', 'c', blobs)\n"
            "    return json.dumps(rows)\n"
        )
        errors = _run(content)
        self.assertTrue(errors)
        self.assertIn("'rows'", " ".join(errors))

    def test_annotated_param_is_a_source(self):
        content = "def render(value: RedactedStr) -> bytes:\n    return value.encode('utf-8')\n"
        errors = _run(content)
        self.assertTrue(errors)
        self.assertIn("'value'", " ".join(errors))

    def test_alias_propagates_taint(self):
        content = (
            "def leak(tenant, blob):\n    x = box.decrypt(tenant, 't', 'c', blob)\n    y = x\n    return y.upper()\n"
        )
        errors = _run(content)
        self.assertTrue(errors)
        self.assertIn("'y'", " ".join(errors))

    def test_bulk_element_indexed_out_is_a_scalar_source(self):
        content = (
            "def leak(tenant, blobs):\n"
            "    rows = box.decrypt_bulk(tenant, 't', 'c', blobs)\n"
            "    first = rows[0]\n"
            "    return first.encode()\n"
        )
        self.assertTrue(_run(content))

    def test_comprehension_over_bulk_without_reveal_flags_the_element(self):
        content = (
            "import json\n\n\n"
            "def leak(tenant, blobs):\n"
            "    rows = box.decrypt_bulk(tenant, 't', 'c', blobs)\n"
            "    return json.dumps([r for r in rows])\n"
        )
        errors = _run(content)
        self.assertTrue(errors)
        # The element `r` leaks; the list `rows` (only the comprehension's
        # iterator) must NOT be double-reported.
        self.assertIn("'r'", " ".join(errors))
        self.assertNotIn("'rows'", " ".join(errors))


class NonViolationTests(SimpleTestCase):
    """Things the guard must NEVER flag — the false-positive floor."""

    def test_reveal_at_egress_is_not_flagged(self):
        content = _SCALAR_SOURCE.format(sink="return json.dumps([x.reveal()])")
        self.assertEqual(_run(content), [])

    def test_reveal_in_comprehension_is_not_flagged(self):
        content = (
            "import json\n\n\n"
            "def egress(tenant, blobs):\n"
            "    rows = box.decrypt_bulk(tenant, 't', 'c', blobs)\n"
            "    return json.dumps([r.reveal() for r in rows])\n"
        )
        self.assertEqual(_run(content), [])

    def test_format_paths_are_not_flagged(self):
        # f-string, %-format, str() and .format() all route through the
        # redacting dunders — safe by construction, must not be flagged.
        for sink in (
            "return f'{x}'",
            "return '%s' % x",
            "return str(x)",
            "return '{}'.format(x)",
        ):
            with self.subTest(sink=sink):
                self.assertEqual(_run(_SCALAR_SOURCE.format(sink=sink)), [])

    def test_bare_redactedstr_construction_is_not_a_source(self):
        # The crypto module's own proof tests construct RedactedStr(...) to
        # DEMONSTRATE the leak; construction is deliberately not a source.
        content = "import json\n\n\ndef proof():\n    x = RedactedStr('secret')\n    return json.dumps([x])\n"
        self.assertEqual(_run(content), [])

    def test_non_box_decrypt_is_not_a_source(self):
        # AESGCM(dek).decrypt(...) inside apps/crypto returns bytes and is not
        # the box API — its receiver isn't `box`/`crypto`, so it must not taint.
        content = (
            "import json\n\n\n"
            "def primitive(dek, nonce, ct, aad):\n"
            "    pt = AESGCM(dek).decrypt(nonce, ct, aad)\n"
            "    return json.dumps([pt])\n"
        )
        self.assertEqual(_run(content), [])

    def test_taint_does_not_cross_functions(self):
        content = (
            "import json\n\n\n"
            "def source(tenant, blob):\n"
            "    x = box.decrypt(tenant, 't', 'c', blob)\n"
            "    return x.reveal()\n\n\n"
            "def other():\n"
            "    x = build_clean_payload()\n"
            "    return json.dumps([x])\n"
        )
        self.assertEqual(_run(content), [])


class NoqaAndAllowlistTests(SimpleTestCase):
    def test_noqa_line_passes(self):
        sink = "return json.dumps([x])  " + guard._NOQA_MARKER + " — seam: test"
        self.assertEqual(_run(_SCALAR_SOURCE.format(sink=sink)), [])

    def test_allowlisted_site_passes(self):
        """The in-script allowlist suppresses that exact (path, line, name) and
        nothing else."""
        content = _SCALAR_SOURCE.format(sink="return json.dumps([x])")
        with tempfile.TemporaryDirectory() as tmp:
            _write_fixture(tmp, content)
            self.assertTrue(guard.find_egress_violations(Path(tmp)))

            entry = ("apps/testapp/probe.py", 6, "x")
            guard._ALLOWLISTED_SITES.add(entry)
            try:
                errors_after = guard.find_egress_violations(Path(tmp))
            finally:
                guard._ALLOWLISTED_SITES.discard(entry)
            self.assertEqual(errors_after, [], f"allowlisted site must pass: {errors_after}")


class RepoStateTests(SimpleTestCase):
    """The real repo must be clean — this is the assertion CI runs."""

    def test_repo_has_no_unallowlisted_egress(self):
        errors = guard.find_egress_violations()
        self.assertEqual(errors, [], f"RedactedStr egress guard found new violations: {errors}")

    def test_allowlist_is_empty_by_design(self):
        """Phase 1 ships with zero decrypt consumers, so there is nothing to
        grandfather — the default answer to a finding is `.reveal()`, not an
        allowlist entry. If this fails, a real raw-egress was allowlisted
        instead of fixed; confirm that was a reviewed, intentional decision."""
        self.assertEqual(guard._ALLOWLISTED_SITES, set())
