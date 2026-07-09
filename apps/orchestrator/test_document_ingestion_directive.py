"""Phase 1 canary test: document information-keeping AGENTS.md gate.

Directive: docs/document-information-keeping-directive.md §3 "Phase 1 —
Agreement + routing + honest expiry, CANARY-ONLY via prompt_extras".

Phase 1 ships the behavioral gate ONLY through the existing, schema-migration
-free ``prompt_extras`` mechanism (``apps.orchestrator.personas.render_workspace_files``
+ ``set_prompt_extras`` management command) — no new model, no base-template
edit, no rules file. These tests pin three things:

1. The canary-scoping contract: a tenant with the ``agents_md`` prompt extra
   set gets the gate text in its rendered AGENTS.md; a tenant without it does
   not (mirrors the spirit of ``test_reassert_agents_md.py`` /
   ``test_personas_workspace_files.py``'s finance-gate tests).
2. Composition: ``set_prompt_extras --section agents_md`` REPLACES the whole
   stored string per call (see docs/canary/README.md) — this block must
   still render correctly when concatenated with another canary block, since
   more than one can target the same tenant (the documented ops recipe is
   `cat docs/canary/*.agents-extras.md | set_prompt_extras --stdin`, not one
   `--file` call per block).
3. The prompt-extras-only change doesn't perturb ``openclaw.json`` generation
   or trip the write-time validation gate (``test_openclaw_schema_shape.py``
   discipline) — this is a workspace-file-only change, and should stay one.
"""

from __future__ import annotations

import io
import os
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from apps.orchestrator.config_generator import generate_openclaw_config
from apps.orchestrator.config_validator import assert_config_writable
from apps.orchestrator.personas import render_workspace_files
from apps.tenants.services import create_tenant

# Same file the ops recipe feeds to `set_prompt_extras` in production (see
# docs/canary/README.md for the exact per-tenant invocation). Single source
# of truth so the fixture used here can't silently drift from what actually
# ships.
_GATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "docs",
    "canary",
    "document-ingestion-phase1.agents-extras.md",
)


def _read_gate_text() -> str:
    with open(_GATE_PATH) as fh:
        return fh.read().strip()


# Load-bearing phrases from directive §3 Phase 1's test spec: answer-first,
# "about a day"/expiry, never-save-same-turn, propose-before-save,
# anti-confabulation, show-content-verbatim, never-promise-retention.
_LOAD_BEARING_PHRASES = [
    "Answer first.",
    "about a day",
    "Never save on the same turn the document arrives.",
    "Save ONLY after they reply and agree",
    "Never say something is saved unless the write tool returned success THIS turn.",
    "the *actual text or values* you'd keep",
    "you can't make yourself un-read what you already read",
]

# A synthetic stand-in for another feature's canary block (e.g. the
# concurrently-shipping quick-replies block), used only to prove
# concatenation composes rather than clobbers.
_SYNTHETIC_SECOND_BLOCK = (
    "## Some Other Canary Feature (synthetic, for test)\n\n"
    "Marker text proving a second concatenated block survives composition.\n"
)


class DocumentIngestionCanaryGateTest(TestCase):
    def test_canary_tenant_gets_the_gate_via_prompt_extras(self):
        tenant = create_tenant(display_name="Canary Doc Tenant", telegram_chat_id=910001)
        # Exercise the real ops recipe, not a hand-set fixture: this is the
        # exact command that will be run against MJ's + the canary tenant.
        call_command(
            "set_prompt_extras",
            tenant_id=str(tenant.id),
            section="agents_md",
            file=_GATE_PATH,
        )
        tenant.user.refresh_from_db()

        files = render_workspace_files("neighbor", tenant=tenant)
        agents_md = files["NBHD_AGENTS_MD"]

        for phrase in _LOAD_BEARING_PHRASES:
            self.assertIn(phrase, agents_md, f"missing load-bearing phrase: {phrase!r}")

    def test_non_canary_tenant_does_not_get_the_gate(self):
        tenant = create_tenant(display_name="Plain Doc Tenant", telegram_chat_id=910002)

        files = render_workspace_files("neighbor", tenant=tenant)
        agents_md = files["NBHD_AGENTS_MD"]

        for phrase in _LOAD_BEARING_PHRASES:
            self.assertNotIn(phrase, agents_md, f"unexpected leak of canary phrase: {phrase!r}")

    def test_concatenated_with_a_second_canary_block_still_renders_all_phrases(self):
        """set_prompt_extras --section agents_md replaces the whole value per
        call (docs/canary/README.md) — so when a second canary block also
        targets this tenant, the ops recipe concatenates both files into one
        `--stdin` call rather than running `--file` twice (which would let
        the last call silently clobber the first). This pins that the
        concatenated content still carries every one of THIS block's
        load-bearing phrases, and that the other block's content survives
        alongside it (true composition, not an overwrite)."""
        tenant = create_tenant(display_name="Concat Doc Tenant", telegram_chat_id=910005)
        combined = _read_gate_text() + "\n\n" + _SYNTHETIC_SECOND_BLOCK

        with patch("sys.stdin", io.StringIO(combined)):
            call_command(
                "set_prompt_extras",
                tenant_id=str(tenant.id),
                section="agents_md",
                stdin=True,
            )
        tenant.user.refresh_from_db()

        files = render_workspace_files("neighbor", tenant=tenant)
        agents_md = files["NBHD_AGENTS_MD"]

        for phrase in _LOAD_BEARING_PHRASES:
            self.assertIn(phrase, agents_md, f"missing load-bearing phrase after concat: {phrase!r}")
        self.assertIn(
            "Marker text proving a second concatenated block survives composition.",
            agents_md,
        )


class DocumentIngestionCanaryConfigWriteGateTest(TestCase):
    """A prompt-extras-only change must not perturb openclaw.json generation."""

    def test_config_write_gate_passes_for_canary_tenant(self):
        tenant = create_tenant(display_name="Canary Config Tenant", telegram_chat_id=910003)
        call_command(
            "set_prompt_extras",
            tenant_id=str(tenant.id),
            section="agents_md",
            file=_GATE_PATH,
        )
        config = generate_openclaw_config(tenant)
        assert_config_writable(config)  # must not raise

    def test_config_write_gate_passes_for_non_canary_tenant(self):
        tenant = create_tenant(display_name="Plain Config Tenant", telegram_chat_id=910004)
        config = generate_openclaw_config(tenant)
        assert_config_writable(config)  # must not raise
