"""Three-feature composition check.

Exercises the combination no single branch's own tests cover: one tenant with
Phase 2's ``document_ingestion_enabled`` flag ON + a ``quick_replies_md``
prompt extra (#1090's canary rule) + a Phase-1-style ``agents_md`` extra
(#1089's canary gate) all active at once. Pins that all three instruction
surfaces render together without clobbering each other and that the combined
config still passes the write gate.

Documents one KNOWN benign duplication: a tenant still carrying the Phase 1
canary ``agents_md`` extras after Phase 2 lands renders the document-keeping
gate twice (base template + extras). Ops follow-up: clear the Phase 1
``agents_md`` extras on the canary tenants once Phase 2 deploys.
"""

from __future__ import annotations

import os

from django.core.management import call_command
from django.test import TestCase

from apps.orchestrator.config_generator import generate_openclaw_config
from apps.orchestrator.config_validator import assert_config_writable
from apps.orchestrator.personas import render_workspace_files
from apps.tenants.services import create_tenant

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PHASE1_GATE = os.path.join(_REPO_ROOT, "docs", "canary", "document-ingestion-phase1.agents-extras.md")
_QUICK_REPLIES = os.path.join(_REPO_ROOT, "docs", "prompts", "quick-reply-buttons.md")


class ThreeFeatureCombinedRenderTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Train Tenant", telegram_chat_id=920001)
        self.tenant.document_ingestion_enabled = True
        self.tenant.save(update_fields=["document_ingestion_enabled"])
        call_command(
            "set_prompt_extras",
            tenant_id=str(self.tenant.id),
            section="agents_md",
            file=_PHASE1_GATE,
        )
        call_command(
            "set_prompt_extras",
            tenant_id=str(self.tenant.id),
            section="quick_replies_md",
            file=_QUICK_REPLIES,
        )
        self.tenant.user.refresh_from_db()
        self.md = render_workspace_files("neighbor", tenant=self.tenant)["NBHD_AGENTS_MD"]

    def test_all_three_instruction_blocks_render(self):
        # Phase 2 base gate (fleet-wide) + flag-gated tool block.
        self.assertIn("Never save on the same turn the document arrives.", self.md)
        self.assertIn("nbhd_document_keep", self.md)
        self.assertIn("nbhd_document_forget", self.md)
        # Quick-replies extra (own section, must not be clobbered by agents_md extras).
        self.assertIn("[[quick-replies:", self.md)
        self.assertIn("tapping one sends that exact label back", self.md)
        # Phase 1 canary extras block survives alongside both.
        self.assertIn("## Document keeping (canary)", self.md)

    def test_no_block_clobbers_another_and_tool_block_is_single(self):
        # The flag-gated tool block and the quick-replies rule render exactly once.
        self.assertEqual(self.md.count("nbhd_document_forget"), 1)
        self.assertEqual(self.md.count("## Quick-reply buttons (iOS)"), 1)
        # KNOWN duplication (benign, ops follow-up): base-template gate + the
        # still-set Phase 1 canary extras carry the same load-bearing sentence.
        self.assertEqual(self.md.count("Never save on the same turn the document arrives."), 2)

    def test_combined_config_passes_write_gate(self):
        config = generate_openclaw_config(self.tenant)
        assert_config_writable(config)  # must not raise
        entries = config.get("plugins", {}).get("entries", {})
        self.assertIn("nbhd-document-keep", entries)
