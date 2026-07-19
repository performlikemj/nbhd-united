"""Phase 0 gate + budget: the sautai meal-plan AGENTS.md gate.

The gate is a LEAN imperative cue (search the catalog + CALL the sautai tools,
never fabricate a plan) gated on ``tenant.sautai_enabled``. All usage detail —
async latency, "started, push coming", don't-list-meals — rides the tool
RESPONSES (runtime/openclaw/plugins/nbhd-sautai-tools), the #1175 pattern, so it
does not spend always-loaded bootstrap budget.

Budget is the load-bearing test: the sautai gate is appended to the same
always-loaded AGENTS.md that #1175/#1177 dieted down. The worst-case render —
every flag-gated gate (friends + doc + email + **sautai** + tour guide +
Gravity) plus BOTH ``agents_md`` extras blocks — must still fit under the IMPORTED
``BOOTSTRAP_MAX_CHARS`` (never a re-hardcoded literal), or a tail gate is
silently truncated at injection and becomes invisible in production.
"""

from __future__ import annotations

import io
import os
from unittest.mock import patch

from django.test import TestCase

from apps.orchestrator.config_generator import BOOTSTRAP_MAX_CHARS, generate_openclaw_config
from apps.orchestrator.config_validator import assert_config_writable
from apps.orchestrator.personas import render_workspace_files
from apps.tenants.services import create_tenant

# Reuse the SAME canary extras fixture the Phase 2 / email-provenance budget
# tests use, so the worst-case composition is faithful to the real share file.
_GATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "docs",
    "canary",
    "person-context-capture-phase2.agents-extras.md",
)
_TASK_DISCIPLINE_BLOCK_CHARS = 1582

_SAUTAI_GATE_MARKER = "## Meal plans (sautai)"
_TOUR_GUIDE_GATE_MARKER = "## Tour guide"
_TOUR_GUIDE_GATE_TAIL = "recent 📍 message exists."


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


class SautaiGateScopingTest(TestCase):
    def test_flag_on_tenant_gets_the_gate_with_both_tools(self):
        tenant = create_tenant(display_name="Sautai Gate On", telegram_chat_id=942001)
        tenant.sautai_enabled = True
        tenant.save(update_fields=["sautai_enabled"])
        agents_md = _agents_md(tenant)
        self.assertIn(_SAUTAI_GATE_MARKER, agents_md)
        self.assertIn("nbhd_generate_meal_plan", agents_md)
        self.assertIn("nbhd_get_meal_plan", agents_md)
        # Never-fabricate rule is the load-bearing half of the gate.
        self.assertIn("without a successful tool result", agents_md)

    def test_flag_off_tenant_does_not_get_the_gate(self):
        tenant = create_tenant(display_name="Sautai Gate Off", telegram_chat_id=942002)
        self.assertFalse(getattr(tenant, "sautai_enabled", False))
        agents_md = _agents_md(tenant)
        self.assertNotIn(_SAUTAI_GATE_MARKER, agents_md)

    def test_gate_is_lean_no_latency_detail_in_agents_md(self):
        # The async-latency / "push notification" usage detail rides the tool
        # responses, NOT the always-loaded gate (the #1175 pattern). Its absence
        # here is what keeps the always-loaded budget small.
        tenant = create_tenant(display_name="Sautai Gate Lean", telegram_chat_id=942003)
        tenant.sautai_enabled = True
        tenant.save(update_fields=["sautai_enabled"])
        agents_md = _agents_md(tenant)
        self.assertNotIn("30-60 seconds", agents_md)
        self.assertNotIn("push notification", agents_md)

    def test_config_write_gate_passes_for_flag_on_tenant(self):
        tenant = create_tenant(display_name="Sautai Gate Config", telegram_chat_id=942004)
        tenant.sautai_enabled = True
        tenant.save(update_fields=["sautai_enabled"])
        assert_config_writable(generate_openclaw_config(tenant))  # must not raise


class SautaiBudgetTest(TestCase):
    """The sautai gate must not push a realistic AGENTS.md past the cap.

    Two compositions, matching the boundary the #1177 diet + the
    email-provenance budget test already established:

    - the "everything but Gravity" worst case (friends + doc + email + **sautai**
      + tour guide + both ``agents_md`` extras), where the tour-guide gate is
      the tail — the same envelope ``test_email_provenance_directive`` pins,
      now including sautai and tour guide. (Adding the ~6 KB Gravity block on
      TOP of this synthetic extras pile overflows for reasons unrelated to this
      gate, so — like the email test — it is out of the must-fit envelope.)
    - a realistic Gravity power-tenant (finance + doc + **sautai**, no synthetic
      extras) where the ~6 KB Gravity block is the tail — proof that turning
      sautai on for a real Gravity user stays comfortably under the cap.
    """

    def test_worst_case_render_with_gates_and_both_extras_fits_the_cap(self):
        tenant = create_tenant(display_name="Sautai Worst Case", telegram_chat_id=942005)
        tenant.friends_enabled = True
        tenant.friends_agent_propose_enabled = True
        tenant.document_ingestion_enabled = True
        tenant.email_provenance_enabled = True
        tenant.sautai_enabled = True
        tenant.tour_guide_enabled = True
        tenant.tour_guide_manifest_ok = True
        tenant.save(
            update_fields=[
                "friends_enabled",
                "friends_agent_propose_enabled",
                "document_ingestion_enabled",
                "email_provenance_enabled",
                "sautai_enabled",
                "tour_guide_enabled",
                "tour_guide_manifest_ok",
            ]
        )
        combined = _task_discipline_stand_in() + "\n\n" + _read_gate_text()
        with patch("sys.stdin", io.StringIO(combined)):
            from django.core.management import call_command

            call_command("set_prompt_extras", tenant_id=str(tenant.id), section="agents_md", stdin=True)
        tenant.user.refresh_from_db()

        agents_md = _agents_md(tenant)
        # Composition is faithful — the sautai gate actually rendered alongside
        # every other block and both extras.
        self.assertIn("## Task discipline", agents_md)
        self.assertIn("BACKSTAGE", agents_md)  # friends gate
        self.assertIn("nbhd_document_keep", agents_md)  # doc-keep gate
        self.assertIn("Saving what you learn from an email", agents_md)  # email gate
        self.assertIn(_SAUTAI_GATE_MARKER, agents_md)  # sautai gate
        self.assertIn(_TOUR_GUIDE_GATE_MARKER, agents_md)  # tour-guide gate

        # The tour-guide gate is the tail here — its END must be under the (imported)
        # cap, or it is silently invisible at injection.
        gate_end = agents_md.find(_TOUR_GUIDE_GATE_TAIL)
        self.assertNotEqual(gate_end, -1)
        gate_end += len(_TOUR_GUIDE_GATE_TAIL)
        self.assertLess(
            gate_end,
            BOOTSTRAP_MAX_CHARS,
            f"tour-guide gate ends at {gate_end} — past the {BOOTSTRAP_MAX_CHARS} injection cap",
        )
        self.assertLess(
            len(agents_md),
            BOOTSTRAP_MAX_CHARS,
            f"worst-case rendered AGENTS.md is {len(agents_md)} chars — past the {BOOTSTRAP_MAX_CHARS} injection cap",
        )

    def test_realistic_gravity_tenant_with_sautai_fits_the_cap(self):
        tenant = create_tenant(display_name="Sautai Gravity Real", telegram_chat_id=942006)
        tenant.finance_enabled = True  # ~6 KB Gravity block, the tail
        tenant.document_ingestion_enabled = True
        tenant.sautai_enabled = True
        tenant.save(update_fields=["finance_enabled", "document_ingestion_enabled", "sautai_enabled"])

        agents_md = _agents_md(tenant)
        self.assertIn(_SAUTAI_GATE_MARKER, agents_md)
        self.assertIn("## Gravity Observation Mode", agents_md)  # Gravity tail present
        self.assertLess(
            len(agents_md),
            BOOTSTRAP_MAX_CHARS,
            f"Gravity + sautai tenant rendered {len(agents_md)} chars — past the {BOOTSTRAP_MAX_CHARS} injection cap",
        )
