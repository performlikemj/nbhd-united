"""Phase 2 canary test: assistant-context-continuity person-capture reflex.

Directive: docs/assistant-context-continuity-directive.md §3 "Phase 2 — P2
capture reflex (behavioral, canary via ``prompt_extras``; no schema, no iOS)"
and D5.

Phase 2 promotes the ``## People & Context`` memory convention into an
in-turn AGENTS.md reflex — but delivers it CANARY-ONLY through the existing,
schema-migration-free ``prompt_extras`` mechanism (``render_workspace_files``
+ ``set_prompt_extras``), exactly as the document-keeping directive staged its
own Phase 1. ``render_workspace_rules()`` takes no tenant argument, so the base
template and the rules files are fleet-wide and un-scopable; the only per-tenant
lever is ``agents_md`` prompt-extras. These tests pin:

1. The canary-scoping contract: a tenant with the ``agents_md`` prompt extra
   set gets the person-capture reflex in its rendered AGENTS.md; a tenant
   without it does not (mirrors ``test_document_ingestion_directive.py`` /
   ``test_personas_workspace_files.py``).
2. Composition: ``set_prompt_extras --section agents_md`` REPLACES the whole
   stored string per call (docs/canary/README.md) — this block must still
   render when concatenated with another ``agents_md``-sharing canary block.
3. The prompt-extras-only change never perturbs ``openclaw.json`` generation
   or trips the write-time validation gate.
4. Budget (critic finding 11, carried over): the reflex lands ABOVE the
   finance-tenant Gravity truncation tail, and a common non-finance canary
   tenant stays under the per-file bootstrap cap.
"""

from __future__ import annotations

import io
import os
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.test.utils import override_settings

from apps.orchestrator.config_generator import BOOTSTRAP_MAX_CHARS, generate_openclaw_config
from apps.orchestrator.config_validator import assert_config_writable
from apps.orchestrator.personas import render_workspace_files
from apps.tenants.services import create_tenant

# Same file the ops recipe feeds to `set_prompt_extras` in production (see
# docs/canary/README.md). Single source of truth so the fixture used here can't
# silently drift from what actually ships on MJ's + the canary tenant.
_GATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "docs",
    "canary",
    "person-context-capture-phase2.agents-extras.md",
)

# OpenClaw truncates each bootstrap file's TAIL beyond bootstrapMaxChars at
# injection time. IMPORTED from config_generator, never re-hardcoded: this
# suite originally pinned a local 18000 and measured only the base TEMPLATE,
# while the canary's RENDERED AGENTS.md (downloaded from the share 2026-07-11)
# was 21,689 chars — the reflex started at char 18,265 and was fully invisible
# in production with these tests green. Assert against the emitted cap AND
# against a render that mirrors the real canary composition (below).
_BOOTSTRAP_MAX_CHARS = BOOTSTRAP_MAX_CHARS

# The OTHER agents_md-sharing extras block live on the real canary tenant (an
# ops-only task-discipline block, never committed to the repo). Prod-measured
# size 2026-07-11. Used to build a faithful length stand-in below.
_TASK_DISCIPLINE_BLOCK_CHARS = 1582


def _task_discipline_stand_in() -> str:
    """Deterministic stand-in for the ops-only task-discipline extras block,
    padded to its prod-measured size so the mirrored-canary render is a
    faithful length model of the real share file."""
    header = "## Task discipline (canary stand-in for the ops-only block)\n\n"
    filler = "Keep tasks honest; close what is done and say so plainly. "
    block = header + filler * ((_TASK_DISCIPLINE_BLOCK_CHARS - len(header)) // len(filler) + 1)
    return block[:_TASK_DISCIPLINE_BLOCK_CHARS]


def _read_gate_text() -> str:
    with open(_GATE_PATH) as fh:
        return fh.read().strip()


def _agents_md(tenant) -> str:
    return render_workspace_files("neighbor", tenant=tenant)["NBHD_AGENTS_MD"]


# Load-bearing phrases from directive §3 Phase 2 / D5: capture-while-fresh,
# the [earlier-from-you] proactive-answer marker, the golden case, the typed
# home (## People & Context via nbhd_memory_update), and the conservative
# durable-facts-only bar.
_LOAD_BEARING_PHRASES = [
    "keep what you hear",
    "while it's fresh",
    "[earlier-from-you",
    "Jasmine",
    "## People & Context",
    "nbhd_memory_update",
    "Durable facts only",
]

# A synthetic stand-in for another block that also has no dedicated section of
# its own and must share `agents_md`. Proves the concat fallback composes
# rather than clobbers (docs/canary/README.md).
_SYNTHETIC_SECOND_BLOCK = (
    "## Some Other Canary Feature (synthetic, for test)\n\n"
    "Marker text proving a second concatenated block survives composition.\n"
)


class ContinuityCaptureCanaryGateTest(TestCase):
    def test_canary_tenant_gets_the_reflex_via_prompt_extras(self):
        tenant = create_tenant(display_name="Canary Continuity", telegram_chat_id=912001)
        # Exercise the real ops recipe, not a hand-set fixture.
        call_command(
            "set_prompt_extras",
            tenant_id=str(tenant.id),
            section="agents_md",
            file=_GATE_PATH,
        )
        tenant.user.refresh_from_db()

        agents_md = _agents_md(tenant)
        for phrase in _LOAD_BEARING_PHRASES:
            self.assertIn(phrase, agents_md, f"missing load-bearing phrase: {phrase!r}")

    def test_non_canary_tenant_does_not_get_the_reflex(self):
        tenant = create_tenant(display_name="Plain Continuity", telegram_chat_id=912002)

        agents_md = _agents_md(tenant)
        # The reflex is canary-only — none of its distinctive phrases leak
        # fleet-wide. Guard against the base template already carrying them.
        for phrase in ("keep what you hear", "## People & Context", "[earlier-from-you"):
            self.assertNotIn(phrase, agents_md, f"unexpected fleet-wide leak: {phrase!r}")

    def test_concatenated_with_a_second_canary_block_still_renders_all_phrases(self):
        """`set_prompt_extras --section agents_md` replaces the whole value per
        call (docs/canary/README.md) — when a second `agents_md`-sharing canary
        block also targets this tenant, the ops recipe concatenates both into
        one `--stdin` call. Pin that both survive (true composition)."""
        tenant = create_tenant(display_name="Concat Continuity", telegram_chat_id=912005)
        combined = _read_gate_text() + "\n\n" + _SYNTHETIC_SECOND_BLOCK

        with patch("sys.stdin", io.StringIO(combined)):
            call_command(
                "set_prompt_extras",
                tenant_id=str(tenant.id),
                section="agents_md",
                stdin=True,
            )
        tenant.user.refresh_from_db()

        agents_md = _agents_md(tenant)
        for phrase in _LOAD_BEARING_PHRASES:
            self.assertIn(phrase, agents_md, f"missing load-bearing phrase after concat: {phrase!r}")
        self.assertIn(
            "Marker text proving a second concatenated block survives composition.",
            agents_md,
        )


class ContinuityCaptureConfigWriteGateTest(TestCase):
    """A prompt-extras-only change must not perturb openclaw.json generation."""

    def test_config_write_gate_passes_for_canary_tenant(self):
        tenant = create_tenant(display_name="Canary Continuity Config", telegram_chat_id=912003)
        call_command(
            "set_prompt_extras",
            tenant_id=str(tenant.id),
            section="agents_md",
            file=_GATE_PATH,
        )
        config = generate_openclaw_config(tenant)
        assert_config_writable(config)  # must not raise

    def test_config_write_gate_passes_for_non_canary_tenant(self):
        tenant = create_tenant(display_name="Plain Continuity Config", telegram_chat_id=912004)
        config = generate_openclaw_config(tenant)
        assert_config_writable(config)  # must not raise


class ContinuityCaptureBudgetTest(TestCase):
    """The reflex must not be the silently-truncated tail (critic finding 11)."""

    def test_non_finance_canary_agents_md_under_bootstrap_budget(self):
        tenant = create_tenant(display_name="Budget Continuity", telegram_chat_id=912006)
        call_command(
            "set_prompt_extras",
            tenant_id=str(tenant.id),
            section="agents_md",
            file=_GATE_PATH,
        )
        tenant.user.refresh_from_db()

        agents_md = _agents_md(tenant)
        # A common (non-finance) canary tenant has no Gravity tail after the
        # reflex, so the whole file must fit under the per-file cap or the
        # reflex itself would be truncated.
        self.assertLess(len(agents_md), _BOOTSTRAP_MAX_CHARS)

    @override_settings(GRAVITY_ENABLED=True)
    def test_finance_friends_propose_render_fits_under_cap_no_truncation(self):
        """The live over-cap shape, now fixed. Before this PR a finance +
        friends-propose tenant rendered ~24.9 KB of AGENTS.md — over the 24 KB
        injection cap — and the ~6 KB Gravity block (ordered last) lost its tail
        to silent truncation on the two real tenants in that shape. This PR
        removed the ~3.4 KB voice-register block from the always-loaded bootstrap
        (it now rides the nbhd_insights_signals tool response) and trimmed the
        observation gate, so the WHOLE file now fits: there is no cut. Assert
        both the ordering invariant (reflex above the Gravity tail) AND — the
        flip from the old "survives the cut" test — that the entire render,
        Gravity tail included, sits under the cap."""
        tenant = create_tenant(display_name="Finance Continuity", telegram_chat_id=912007)
        tenant.finance_enabled = True
        tenant.friends_enabled = True
        tenant.friends_agent_propose_enabled = True
        tenant.save(update_fields=["finance_enabled", "friends_enabled", "friends_agent_propose_enabled"])
        call_command(
            "set_prompt_extras",
            tenant_id=str(tenant.id),
            section="agents_md",
            file=_GATE_PATH,
        )
        tenant.user.refresh_from_db()
        self.assertTrue(tenant.finance_active)  # the truncation-relevant case is live

        agents_md = _agents_md(tenant)
        reflex_at = agents_md.find("keep what you hear")
        gravity_at = agents_md.find("Gravity Observation Mode")
        self.assertNotEqual(reflex_at, -1)
        self.assertNotEqual(gravity_at, -1)
        # Ordering invariant preserved: the reflex still sits above the Gravity tail.
        self.assertLess(reflex_at, gravity_at)
        # The register rules moved to the signals tool response — their old
        # always-loaded signature must be gone from the bootstrap.
        self.assertNotIn("Voice Register Selection", agents_md)
        # The flip: the whole render — Gravity tail and all — now fits the cap.
        # Nothing is truncated, so the Gravity block's END is under the cap too.
        self.assertLess(
            gravity_at,
            _BOOTSTRAP_MAX_CHARS,
            "the Gravity block starts past the cap",
        )
        self.assertLess(
            len(agents_md),
            _BOOTSTRAP_MAX_CHARS,
            f"finance + friends-propose AGENTS.md is {len(agents_md)} chars — past the "
            f"{_BOOTSTRAP_MAX_CHARS} injection cap; the Gravity tail would be silently truncated",
        )

    def test_mirrored_canary_render_with_both_extras_blocks_fits_the_cap(self):
        """The missing case that caught us in production (2026-07-11): the base
        TEMPLATE was measured (16,425 chars, comfortably under the then-18,000
        cap) but the canary's RENDERED file was 21,689 — persona substitutions
        plus the friends/doc conditional blocks plus TWO appended agents_md
        extras blocks (ops-only task-discipline ~1,582 + person-capture reflex
        ~1,310). Everything past the cap was silently invisible at injection,
        including the entire reflex.

        This test renders that composition: a tenant with the friends + doc
        flags on and BOTH extras blocks applied via the documented concat
        `--stdin` recipe. It asserts the extras' END index — not just the base
        template length — sits under the (imported) cap, and that the full
        rendered file fits. If a third agents_md extras block (or base/gate
        growth) pushes the render past the cap, THIS fails — the regression
        this file exists to catch.
        """
        tenant = create_tenant(display_name="Mirrored Canary", telegram_chat_id=912008)
        tenant.friends_enabled = True
        tenant.friends_agent_propose_enabled = True
        tenant.document_ingestion_enabled = True
        tenant.save(
            update_fields=[
                "friends_enabled",
                "friends_agent_propose_enabled",
                "document_ingestion_enabled",
            ]
        )
        combined = _task_discipline_stand_in() + "\n\n" + _read_gate_text()
        with patch("sys.stdin", io.StringIO(combined)):
            call_command(
                "set_prompt_extras",
                tenant_id=str(tenant.id),
                section="agents_md",
                stdin=True,
            )
        tenant.user.refresh_from_db()

        agents_md = _agents_md(tenant)
        # The composition is faithful: both extras blocks and the flag gates
        # actually rendered (otherwise the length assertions test nothing).
        self.assertIn("## Task discipline", agents_md)
        self.assertIn("keep what you hear", agents_md)
        self.assertIn("BACKSTAGE", agents_md)  # friends gate present
        self.assertIn("nbhd_document_keep", agents_md)  # doc-keep tool gate present

        # The reflex is the tail of the extras — its END must sit above the cut.
        reflex_tail = "not a transcript."
        extras_end = agents_md.find(reflex_tail)
        self.assertNotEqual(extras_end, -1)
        extras_end += len(reflex_tail)
        self.assertLess(
            extras_end,
            _BOOTSTRAP_MAX_CHARS,
            f"extras end at {extras_end} — past the {_BOOTSTRAP_MAX_CHARS} injection cap; "
            "everything beyond is silently invisible in production",
        )
        # And the WHOLE rendered file fits — a third extras block or base
        # growth that pushes past the cap must fail here, not in production.
        self.assertLess(
            len(agents_md),
            _BOOTSTRAP_MAX_CHARS,
            f"rendered canary AGENTS.md is {len(agents_md)} chars — past the "
            f"{_BOOTSTRAP_MAX_CHARS} injection cap; the tail is silently dropped",
        )
