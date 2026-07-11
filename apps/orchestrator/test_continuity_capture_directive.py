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

from apps.orchestrator.config_generator import generate_openclaw_config
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

# OpenClaw truncates each bootstrap file's TAIL beyond this many chars.
_BOOTSTRAP_MAX_CHARS = 18000


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
    def test_reflex_survives_finance_truncation(self):
        """A finance tenant's AGENTS.md legitimately exceeds the cap (the ~6KB
        Gravity block is the intended truncation tail). The reflex, appended as
        `agents_md` extras BEFORE the Gravity block, must land above the cut."""
        tenant = create_tenant(display_name="Finance Continuity", telegram_chat_id=912007)
        tenant.finance_enabled = True
        tenant.save(update_fields=["finance_enabled"])
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
        # Reflex is above the truncation cut, and above the Gravity tail.
        self.assertLess(reflex_at, _BOOTSTRAP_MAX_CHARS, "the reflex falls in the truncated tail")
        self.assertLess(reflex_at, gravity_at)
