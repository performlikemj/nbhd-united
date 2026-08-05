"""Drift guard for the shared plugin error-transport helper.

Every nbhd-* plugin that talks to the NBHD runtime used to build its thrown
error with the naive pattern::

    const detail = asTrimmedString(normalized.detail);

DRF and the runtime views answer a bad write with a validation ENVELOPE
(``{error, message, details[]}``) or with top-level field errors
(``{week_rating: ["This field is required."]}``) — neither of which lives
under ``detail``. The naive read therefore discarded the whole body, and the
model saw only ``NBHD runtime error 400: validation_failed``: not enough to
correct the call, so it retried the same broken request.

The fix is a shared ``compactErrorDetail()`` helper. It is deliberately NOT a
shared module: OpenClaw loads each plugin directory standalone, so the helper
is copied verbatim into all 13 plugins. Copies rot. This test is the structural
guard — it pins the canonical text and asserts all 13 copies are byte-identical
to it and to each other, so a hand-edit to one file fails CI instead of
silently regressing that plugin's error transport.

The behavioral half (that the helper actually reaches the model through each
plugin's real ``register()`` → ``execute()`` path) lives in
``runtime/openclaw/plugins/error-transport.test.js``, run by ``node --test``
in the CI workflow. Both halves are needed: this one can't prove the call site
uses the helper correctly, and the node suite can't prove the copies haven't
drifted apart in ways its fixtures don't happen to exercise.
"""

import re
from pathlib import Path

from django.test import SimpleTestCase

_PLUGINS_DIR = Path(__file__).resolve().parents[2] / "runtime/openclaw/plugins"

# Every plugin whose runtime-call function must carry the canonical helper.
PLUGIN_DIRS = [
    "nbhd-fuel-tools",
    "nbhd-finance-tools",
    "nbhd-insights-tools",
    "nbhd-settings-tools",
    "nbhd-sautai-tools",
    "nbhd-friends-tools",
    "nbhd-document-keep",
    "nbhd-automation-tools",
    "nbhd-journal-shaping",
    "nbhd-reddit-tools",
    "nbhd-journal-tools",
    "nbhd-google-tools",
    # Registers nbhd_record_commitment — a model-facing POST write to the
    # runtime, i.e. exactly the shape DRF answers with a validation body.
    "nbhd-agenda-tools",
]

# The pattern the fix removed. Its reappearance anywhere in a plugin means the
# error block regressed to reading only `detail`.
NAIVE_PATTERN = "asTrimmedString(normalized.detail)"

# Marker-slicing regex: from the constant through the final line of
# compactErrorDetail. Anchored on both ends so it cannot silently swallow
# unrelated code if a file is reordered.
HELPER_BLOCK_RE = re.compile(
    r"const TOOL_ERROR_DETAIL_MAX_CHARS = 2000;"
    r".*?"
    r"return clampErrorDetail\(String\(value\)\);\n  \}\n\}",
    re.DOTALL,
)

# The canonical text, pinned. Edit here ONLY together with all 13 plugins.
CANONICAL_HELPER = """const TOOL_ERROR_DETAIL_MAX_CHARS = 2000;

function clampErrorDetail(text) {
  if (text.length <= TOOL_ERROR_DETAIL_MAX_CHARS) return text;
  return `${text.slice(0, TOOL_ERROR_DETAIL_MAX_CHARS)}… [truncated]`;
}

function compactErrorDetail(payload) {
  const normalized = asObject(payload);
  const entries = Object.entries(normalized).filter(([key]) => key !== "error");
  if (entries.length === 0) return "";

  const detail = normalized.detail;
  const detailIsOnlyKey = entries.length === 1 && detail !== undefined;
  if (detailIsOnlyKey && typeof detail === "string") {
    return detail.trim() ? clampErrorDetail(detail.trim()) : "";
  }

  const value = detailIsOnlyKey ? detail : Object.fromEntries(entries);
  if (value === null || (typeof value === "object" && Object.keys(value).length === 0)) return "";

  try {
    return clampErrorDetail(JSON.stringify(value));
  } catch {
    return clampErrorDetail(String(value));
  }
}"""

# The three lines of the throw construction that must be identical everywhere.
# The surrounding `if (!response.ok ...)` condition is deliberately NOT pinned:
# nbhd-settings-tools legitimately adds `&& !allowResponseStatuses.includes(...)`
# for the 429/503 places-search path.
CANONICAL_THROW = (
    "      const detail = compactErrorDetail(normalized);\n"
    '      const detailSuffix = detail ? ` (${detail})` : "";\n'
    "      throw new Error(`NBHD runtime error ${response.status}: "
    "${code}${detailSuffix}`);\n"
)

CANONICAL_CODE_LINE = '      const code = asTrimmedString(normalized.error) || "runtime_request_failed";\n'


def _plugin_path(plugin_dir: str) -> Path:
    return _PLUGINS_DIR / plugin_dir / "index.js"


class PluginErrorTransportDriftTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.sources = {}
        for plugin_dir in PLUGIN_DIRS:
            path = _plugin_path(plugin_dir)
            if path.exists():
                cls.sources[plugin_dir] = path.read_text()

    def test_every_plugin_file_is_present(self):
        for plugin_dir in PLUGIN_DIRS:
            with self.subTest(plugin=plugin_dir):
                self.assertTrue(_plugin_path(plugin_dir).exists(), _plugin_path(plugin_dir))

    def test_helper_function_is_defined(self):
        # (a) the helper exists in every plugin.
        for plugin_dir, src in self.sources.items():
            with self.subTest(plugin=plugin_dir):
                self.assertIn("function compactErrorDetail(", src)
                self.assertIn("function clampErrorDetail(", src)

    def test_helper_is_actually_called_at_the_error_site(self):
        # (b) defining it is not enough — the throw must consume it. A plugin
        # that carries the helper but never calls it ships the old bug.
        for plugin_dir, src in self.sources.items():
            with self.subTest(plugin=plugin_dir):
                self.assertIn("compactErrorDetail(normalized)", src)

    def test_naive_detail_only_pattern_is_gone(self):
        # (c) the regression this whole change exists to prevent.
        for plugin_dir, src in self.sources.items():
            with self.subTest(plugin=plugin_dir):
                self.assertNotIn(
                    NAIVE_PATTERN,
                    src,
                    f"{plugin_dir} regressed to the detail-only error read — the "
                    "validation envelope will be dropped again",
                )

    def test_clamp_constant_is_declared(self):
        # (d) the 2000-char bound the runtime relies on to keep a huge upstream
        # body from flooding the model's context.
        for plugin_dir, src in self.sources.items():
            with self.subTest(plugin=plugin_dir):
                self.assertIn("TOOL_ERROR_DETAIL_MAX_CHARS = 2000", src)

    def test_helper_block_appears_exactly_once(self):
        for plugin_dir, src in self.sources.items():
            with self.subTest(plugin=plugin_dir):
                matches = HELPER_BLOCK_RE.findall(src)
                self.assertEqual(
                    len(matches),
                    1,
                    f"{plugin_dir}: expected exactly one canonical helper block, found {len(matches)}",
                )

    def test_helper_block_is_byte_identical_across_all_plugins(self):
        # (e) the drift guard proper. One copy edited by hand → this fails.
        blocks = {}
        for plugin_dir, src in self.sources.items():
            match = HELPER_BLOCK_RE.search(src)
            self.assertIsNotNone(match, f"{plugin_dir}: canonical helper block not found")
            blocks[plugin_dir] = match.group(0)

        self.assertEqual(len(blocks), len(PLUGIN_DIRS))

        distinct = set(blocks.values())
        if len(distinct) != 1:
            # Name the odd files out so the failure is actionable rather than
            # "13 strings differ somehow".
            baseline = blocks[PLUGIN_DIRS[0]]
            drifted = [name for name, text in blocks.items() if text != baseline]
            self.fail(f"plugin error-transport helper has drifted; these differ from {PLUGIN_DIRS[0]}: {drifted}")

    def test_helper_block_matches_the_pinned_canonical_text(self):
        for plugin_dir, src in self.sources.items():
            with self.subTest(plugin=plugin_dir):
                match = HELPER_BLOCK_RE.search(src)
                self.assertIsNotNone(match, f"{plugin_dir}: helper block not found")
                self.assertEqual(match.group(0), CANONICAL_HELPER)

    def test_throw_construction_is_canonical(self):
        # The helper is useless if the call site assembles the message
        # differently — e.g. the bespoke `[provider_status=...]` suffix
        # nbhd-google-tools used to append instead of serializing the envelope.
        for plugin_dir, src in self.sources.items():
            with self.subTest(plugin=plugin_dir):
                self.assertEqual(
                    src.count(CANONICAL_CODE_LINE),
                    1,
                    f"{plugin_dir}: expected exactly one canonical error-code line",
                )
                self.assertEqual(
                    src.count(CANONICAL_THROW),
                    1,
                    f"{plugin_dir}: expected exactly one canonical throw construction",
                )

    def test_helper_is_defined_before_its_call_site(self):
        # The spec places the constant + both functions immediately above the
        # plugin's runtime-call function. Function declarations hoist, so this
        # is about readability rather than correctness — but a copy that landed
        # somewhere arbitrary is a signal the edit was done by hand.
        for plugin_dir, src in self.sources.items():
            with self.subTest(plugin=plugin_dir):
                match = HELPER_BLOCK_RE.search(src)
                self.assertIsNotNone(match, f"{plugin_dir}: helper block not found")
                self.assertLess(
                    match.end(),
                    src.index("compactErrorDetail(normalized)"),
                    f"{plugin_dir}: helper block should sit above its call site",
                )

    def test_behavioral_suite_covers_the_same_plugin_list(self):
        # The node suite and this drift guard must not disagree about which
        # plugins are in scope; a plugin added to one and not the other is a
        # silent coverage hole.
        suite = (_PLUGINS_DIR / "error-transport.test.js").read_text()
        for plugin_dir in PLUGIN_DIRS:
            with self.subTest(plugin=plugin_dir):
                self.assertIn(f'dir: "{plugin_dir}"', suite)

    def test_plugin_list_is_complete(self):
        # PLUGIN_DIRS used to be a hand-maintained list that CLAIMED to be
        # exhaustive while nbhd-agenda-tools — a model-facing plugin that POSTs
        # to the runtime — sat outside it, still carrying the detail-dropping
        # bug. A hand-maintained list cannot assert its own completeness, so
        # derive the in-scope set from the tree instead: a plugin is in scope
        # when it registers model-facing tools AND calls fetch(). Anything new
        # that matches must be rolled out and listed, not silently skipped.
        discovered = sorted(
            path.parent.name
            for path in _PLUGINS_DIR.glob("*/index.js")
            if "registerTool" in path.read_text() and "fetch(" in path.read_text()
        )
        self.assertEqual(
            discovered,
            sorted(PLUGIN_DIRS),
            "a runtime-calling plugin is missing from PLUGIN_DIRS (or listed but no "
            "longer matches); roll the canonical helper into it and add it to both "
            "this list and error-transport.test.js",
        )
