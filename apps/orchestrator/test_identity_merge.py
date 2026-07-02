"""Tests for the sentinel-split SOUL.md/IDENTITY.md merge (``identity_merge``)
and the managed-region renders (``personas.render_soul_managed`` /
``render_identity_managed``).

The three-case merge is the riskiest piece of PR-2: a wrong branch can either
wipe an agent's grown identity or freeze the platform baseline. These tests pin
each case, the legacy-render recognition that decides case 1 vs case 3, and the
byte-for-byte preservation of a hand-authored custom soul (the Kiho tenant).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from django.test import TestCase

from apps.orchestrator import identity_merge as im
from apps.orchestrator.personas import PERSONAS, render_identity_managed, render_soul_managed

# A hand-authored custom soul (Kiho-style) — distinct headers, must NEVER be
# mistaken for a platform render.
KIHO_SOUL = """# SOUL.md - Who You Are

## Core Truths

I am Kiho's studio assistant, steeped in ceramics and film photography.

## Boundaries

Never publish to the portfolio without an explicit sign-off.

## Vibe

Quiet, exacting, a little wry.

## Continuity

I remember every glaze test we have ever run together.
"""


def _mj_canary_soul() -> str:
    """MJ-canary-style legacy render: pre-#986 base + persona traits, no markers."""
    return im._LEGACY_SOUL_BASE_OLD + "\n\n## Your Persona\n\n" + im._LEGACY_SOUL_TRAITS_OLD["neighbor"]


class SpliceIdentityFileTest(TestCase):
    def setUp(self):
        self.managed = render_soul_managed("neighbor")
        self.seed = im.growth_seed_line("Neighbor")
        self.kw = dict(
            begin_marker=im.SOUL_BEGIN_MARKER,
            end_marker=im.SOUL_END_MARKER,
            is_legacy_platform=im.is_known_platform_soul,
        )

    def test_case1_empty_writes_managed_plus_seed(self):
        for existing in (None, "", "   \n\n"):
            out = im.splice_identity_file(existing, self.managed, self.seed, **self.kw)
            self.assertTrue(out.startswith(im.SOUL_BEGIN_MARKER))
            self.assertIn(im.SOUL_END_MARKER, out)
            self.assertIn("This space is yours, Neighbor", out)

    def test_case1_legacy_render_is_upgraded_with_seed(self):
        """A recognised legacy platform soul is replaced (case 1), not preserved."""
        out = im.splice_identity_file(_mj_canary_soul(), self.managed, self.seed, **self.kw)
        self.assertTrue(out.startswith(im.SOUL_BEGIN_MARKER))
        self.assertIn("This space is yours, Neighbor", out)
        # The old "Warm but not fake" body must be gone — it was the platform's,
        # not the agent's growth.
        self.assertNotIn("Warm but not fake", out)

    def test_case2_replaces_managed_preserves_growth_verbatim(self):
        growth = "They call me Bird now — inside joke about the balcony pigeons.\n"
        existing = (
            "STALE-BEFORE\n" + im.SOUL_BEGIN_MARKER + "\nstale managed body\n" + im.SOUL_END_MARKER + "\n\n" + growth
        )
        out = im.splice_identity_file(existing, self.managed, self.seed, **self.kw)
        self.assertIn(growth.strip(), out)  # growth preserved verbatim
        self.assertNotIn("stale managed body", out)  # old managed replaced
        self.assertNotIn("STALE-BEFORE", out)  # content above BEGIN dropped
        self.assertIn("genuine companion", out)  # fresh managed present
        self.assertNotIn("This space is yours", out)  # seed NOT re-added over real growth

    def test_case3_custom_soul_preserved_byte_for_byte(self):
        out = im.splice_identity_file(KIHO_SOUL, self.managed, self.seed, **self.kw)
        self.assertTrue(out.startswith(im.SOUL_BEGIN_MARKER))
        # The entire custom soul survives verbatim as growth.
        self.assertIn(KIHO_SOUL.strip(), out)
        self.assertTrue(out.rstrip().endswith(KIHO_SOUL.strip()))
        self.assertNotIn("This space is yours", out)  # no seed over real content

    def test_case3_round_trip_is_idempotent(self):
        """Splicing twice (case3 → case2) keeps the custom soul stable."""
        once = im.splice_identity_file(KIHO_SOUL, self.managed, self.seed, **self.kw)
        twice = im.splice_identity_file(once, self.managed, self.seed, **self.kw)
        self.assertIn(KIHO_SOUL.strip(), twice)
        self.assertEqual(once, twice)


class KnownPlatformRecognitionTest(TestCase):
    def test_mj_canary_recognised(self):
        self.assertTrue(im.is_known_platform_soul(_mj_canary_soul()))

    def test_new_base_current_traits_recognised(self):
        for key, persona in PERSONAS.items():
            render = im._LEGACY_SOUL_BASE_NEW + "\n\n## Your Persona\n\n" + persona["soul_traits"]
            self.assertTrue(im.is_known_platform_soul(render), f"persona {key} new-base render not recognised")

    def test_hardcoded_fallback_recognised(self):
        render = im._hardcoded_fallback_soul(PERSONAS["coach"]["soul_traits"])
        self.assertTrue(im.is_known_platform_soul(render))

    def test_custom_soul_not_recognised(self):
        self.assertFalse(im.is_known_platform_soul(KIHO_SOUL))

    def test_empty_not_recognised(self):
        self.assertFalse(im.is_known_platform_soul(""))
        self.assertFalse(im.is_known_platform_soul(None))

    def test_legacy_identity_recognised(self):
        for key, persona in PERSONAS.items():
            legacy = im._legacy_identity_render(persona["identity"])
            self.assertTrue(im.is_known_platform_identity(legacy), f"persona {key} legacy identity not recognised")

    def test_custom_identity_not_recognised(self):
        self.assertFalse(im.is_known_platform_identity("# Bird\n\nA name we grew into together.\n"))


class RenderManagedTest(TestCase):
    def test_soul_managed_has_markers_and_persona(self):
        out = render_soul_managed("neighbor")
        self.assertTrue(out.startswith(im.SOUL_BEGIN_MARKER))
        self.assertTrue(out.rstrip().endswith(im.SOUL_END_MARKER))
        self.assertIn("genuine companion", out)  # persona soul_traits spliced in
        self.assertNotIn("{{PERSONA_SOUL_TRAITS}}", out)  # placeholder resolved
        self.assertIn(im.SOUL_PRECEDENCE_LINE, out)

    def test_identity_managed_substitutes_placeholders(self):
        out = render_identity_managed("spark")
        self.assertTrue(out.startswith(im.IDENTITY_BEGIN_MARKER))
        self.assertIn("Spark", out)
        self.assertIn("AI creative catalyst", out)  # creature
        self.assertNotIn("{{PERSONA_NAME}}", out)
        self.assertNotIn("{{PERSONA_CREATURE}}", out)

    def test_soul_prompt_extras_land_inside_managed_region(self):
        tenant = MagicMock()
        tenant.user.preferences = {"prompt_extras": {"soul_md": "CANARY_SOUL_RULE"}}
        out = render_soul_managed("neighbor", tenant)
        before_end = out.split(im.SOUL_END_MARKER)[0]
        self.assertIn("CANARY_SOUL_RULE", before_end)

    def test_identity_prompt_extras_land_inside_managed_region(self):
        tenant = MagicMock()
        tenant.user.preferences = {"prompt_extras": {"identity_md": "CANARY_IDENTITY_RULE"}}
        out = render_identity_managed("neighbor", tenant)
        before_end = out.split(im.IDENTITY_END_MARKER)[0]
        self.assertIn("CANARY_IDENTITY_RULE", before_end)
