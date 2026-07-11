"""Phase 5 gate + budget: email/calendar/Reddit ingestion-provenance AGENTS.md gate.

Directive: docs/assistant-context-continuity-directive.md §3 "Phase 5" + D7/D8.

The gate teaches the agent to PROPOSE before saving anything it learned from a
Gmail/calendar/Reddit read (attacker-controllable text, D8) and to stamp such saves
onto the SAME document-keeping ledger with a ``source_kind`` + ``source_ref``. It is
gated on the NEW ``email_provenance_enabled`` flag, held OFF for canary until the
AGENTS.md budget headroom is resolved and the plugin's ``source_kind`` params ship on
the next image roll.

Budget is the load-bearing test (we shipped a fix TODAY because a 21,689-char rendered
AGENTS.md silently overran the injection cap): the worst-case render — friends + doc +
EMAIL flags plus BOTH ``agents_md`` extras blocks — must fit under the IMPORTED
``BOOTSTRAP_MAX_CHARS`` (never a re-hardcoded literal), or the gate is invisible.
"""

from __future__ import annotations

import io
import os
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from apps.orchestrator.config_generator import BOOTSTRAP_MAX_CHARS, generate_openclaw_config
from apps.orchestrator.config_validator import assert_config_writable
from apps.orchestrator.personas import render_workspace_files
from apps.tenants.services import create_tenant

# Reuse the SAME canary extras fixtures the Phase 2 budget test uses, so the
# worst-case composition here is faithful to the real share file.
_GATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "docs",
    "canary",
    "person-context-capture-phase2.agents-extras.md",
)
_TASK_DISCIPLINE_BLOCK_CHARS = 1582

# A distinctive phrase from the email-provenance gate (personas.py).
_EMAIL_GATE_MARKER = "Saving what you learn from an email"
_EMAIL_GATE_TAIL = "don't say it clears out in a day."


def _task_discipline_stand_in() -> str:
    header = "## Task discipline (canary stand-in for the ops-only block)\n\n"
    filler = "Keep tasks honest; close what is done and say so plainly. "
    block = header + filler * ((_TASK_DISCIPLINE_BLOCK_CHARS - len(header)) // len(filler) + 1)
    return block[:_TASK_DISCIPLINE_BLOCK_CHARS]


def _read_gate_text() -> str:
    with open(_GATE_PATH) as fh:
        return fh.read().strip()


def _agents_md(tenant) -> str:
    return render_workspace_files("neighbor", tenant=tenant)["NBHD_AGENTS_MD"]


class EmailProvenanceGateScopingTest(TestCase):
    def test_flag_on_tenant_gets_the_gate(self):
        tenant = create_tenant(display_name="Email Gate On", telegram_chat_id=941001)
        tenant.document_ingestion_enabled = True
        tenant.email_provenance_enabled = True
        tenant.save(update_fields=["document_ingestion_enabled", "email_provenance_enabled"])
        agents_md = _agents_md(tenant)
        self.assertIn(_EMAIL_GATE_MARKER, agents_md)
        self.assertIn("source_kind", agents_md)
        self.assertIn("gmail:<message-id>", agents_md)

    def test_flag_off_tenant_does_not_get_the_gate(self):
        # The doc-keep flag alone (canary state) must NOT pull in the email gate.
        tenant = create_tenant(display_name="Email Gate Off", telegram_chat_id=941002)
        tenant.document_ingestion_enabled = True
        tenant.save(update_fields=["document_ingestion_enabled"])
        agents_md = _agents_md(tenant)
        self.assertNotIn(_EMAIL_GATE_MARKER, agents_md)

    def test_config_write_gate_passes_for_flag_on_tenant(self):
        tenant = create_tenant(display_name="Email Gate Config", telegram_chat_id=941003)
        tenant.document_ingestion_enabled = True
        tenant.email_provenance_enabled = True
        tenant.save(update_fields=["document_ingestion_enabled", "email_provenance_enabled"])
        assert_config_writable(generate_openclaw_config(tenant))  # must not raise


class EmailProvenanceBudgetTest(TestCase):
    """The gate must fit the injection cap even in the worst composition."""

    def test_worst_case_render_with_email_flag_and_both_extras_fits_the_cap(self):
        tenant = create_tenant(display_name="Email Worst Case", telegram_chat_id=941004)
        tenant.friends_enabled = True
        tenant.friends_agent_propose_enabled = True
        tenant.document_ingestion_enabled = True
        tenant.email_provenance_enabled = True
        tenant.save(
            update_fields=[
                "friends_enabled",
                "friends_agent_propose_enabled",
                "document_ingestion_enabled",
                "email_provenance_enabled",
            ]
        )
        combined = _task_discipline_stand_in() + "\n\n" + _read_gate_text()
        with patch("sys.stdin", io.StringIO(combined)):
            call_command("set_prompt_extras", tenant_id=str(tenant.id), section="agents_md", stdin=True)
        tenant.user.refresh_from_db()

        agents_md = _agents_md(tenant)
        # Composition is faithful — every gate + both extras actually rendered.
        self.assertIn("## Task discipline", agents_md)
        self.assertIn("keep what you hear", agents_md)  # person-capture reflex
        self.assertIn("BACKSTAGE", agents_md)  # friends gate
        self.assertIn("nbhd_document_keep", agents_md)  # doc-keep tool gate
        self.assertIn(_EMAIL_GATE_MARKER, agents_md)  # email-provenance gate

        # The email gate is the tail of the appended blocks — its END must sit
        # under the (imported) cap, or it is silently invisible at injection.
        gate_end = agents_md.find(_EMAIL_GATE_TAIL)
        self.assertNotEqual(gate_end, -1)
        gate_end += len(_EMAIL_GATE_TAIL)
        self.assertLess(
            gate_end,
            BOOTSTRAP_MAX_CHARS,
            f"email gate ends at {gate_end} — past the {BOOTSTRAP_MAX_CHARS} injection cap; "
            "everything beyond is silently invisible in production",
        )
        self.assertLess(
            len(agents_md),
            BOOTSTRAP_MAX_CHARS,
            f"worst-case rendered AGENTS.md is {len(agents_md)} chars — past the {BOOTSTRAP_MAX_CHARS} injection cap",
        )
