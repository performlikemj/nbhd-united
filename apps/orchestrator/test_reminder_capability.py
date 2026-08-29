"""Reminder capability grounding in the base AGENTS.md, and the bootstrap-cap sentinel.

THE BUG. ``## What You Can Do`` carries a standing umbrella: "Treat every capability in
this list as something you *can* do: if you don't see the tool already loaded, that means
'go find it via tool search,' never 'I can't.' Never tell the user you're unable to do
something listed here — until you've searched for the tool and actually tried it."

Reminders were NOT in that list. So the assistant obeyed its prompt to the letter — an
unlisted capability falls through to ``## What You Can't Do`` ("Don't pretend — suggest
alternatives instead") — and told users:

    "I don't have the ability to send you a push notification or alarm at a specific time."

…while holding ``nbhd_cron_create_pure_reminder``, loaded, unused. (Behavior eval runs 72
and 79, 2026-07-13.) The fix is ONE bullet in the list, which inherits the umbrella for
free. That is why position matters and is pinned below: a bullet that drifts out of the
list loses the protection that makes it work.

THE SECOND BUG guarded here. Per-tenant blocks (prompt_extras, Neighborhood, sautai,
doc-keep) are appended to the TAIL of AGENTS.md, and OpenClaw truncates at
``bootstrapMaxChars`` SILENTLY and mid-rule — no error, no log, the model just stops
reading. So the NEWEST behavioral rule always dies first, with CI green. That is exactly
how the 2026-07-11 canary truncation happened (470e122e).

The trap this file exists to close: MJ's real tenant renders ~22.9K against the 24K cap;
the eval tenant renders ~16K. A budget fixture shaped like the EVAL tenant would pass
happily while production silently truncated. So the fixture below models the FULL shape —
every flag, plus a realistic tail of prompt extras.
"""

from __future__ import annotations

from django.test import TestCase
from django.test.utils import override_settings

from apps.orchestrator.config_generator import BOOTSTRAP_MAX_CHARS, generate_openclaw_config
from apps.orchestrator.personas import render_workspace_files
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant

_REMINDER_TOOL = "nbhd_cron_create_pure_reminder"
_CANT_DO_HEADING = "## What You Can't Do"

# Budget, measured against the real fleet and every AGENTS.md tenant gate
# in personas.py (site publishing, site editing, situation capture, friends, document keep,
# email provenance, sautai, tour guide, journal shaping, and Gravity):
#
#   runtime cap                           26,000
#   CI truncation-alarm ceiling           25,950 (50-char cap margin)
#   R0 content-growth pin                 23,762
#   template alone                        14,170
#   MJ-shaped gates + 1,500 extras        20,491
#   ALL gates, no extras                  23,762
#   ALL gates + 1,500 extras              25,264 after KSE-2
#
# The ceiling is the truncation alarm, fixed 50 chars below the runtime cap. P5 permits
# raising it only together with that cap, never alone. The separate R0 content pin below
# still requires growth to be funded with a trim (the cc1602aa / a5fca659 precedent).
_ALL_GATES_CEILING = 25_950


def _agents_md(tenant=None) -> str:
    return render_workspace_files("neighbor", tenant=tenant)["NBHD_AGENTS_MD"]


class ConversationalReconcileGateTest(TestCase):
    def test_happened_events_are_material_and_written_before_reply(self):
        md = _agents_md()
        self.assertIn("an interview/meeting/event that happened", md)
        self.assertIn("BEFORE composing the reply, MUST call `nbhd_reconcile_scan", md)
        self.assertIn("then MUST apply its indicated typed write(s)", md)

    def test_routine_updates_do_not_ask_permission_and_are_reported(self):
        md = _agents_md()
        self.assertIn("Do not ask permission for routine state updates", md)
        self.assertIn("ask only when the action is destructive or genuinely ambiguous", md)
        self.assertIn('The reply MUST state what changed (e.g. *"Marked the Optiver interview task done."*)', md)


class ReminderCapabilityListedTest(TestCase):
    def test_the_reminder_tool_is_named_in_the_base_template(self):
        self.assertIn(_REMINDER_TOOL, _agents_md())

    def test_it_sits_INSIDE_the_protected_capability_list(self):
        """Position is the whole mechanism.

        The "never say you can't until you've searched and tried" umbrella applies only
        to things listed under ``## What You Can Do``. A bullet that drifts below
        ``## What You Can't Do`` keeps the words and loses the protection — and would
        also be the first thing truncated. Pin it.
        """
        md = _agents_md()
        tool_at = md.find(_REMINDER_TOOL)
        cant_do_at = md.find(_CANT_DO_HEADING)
        self.assertNotEqual(tool_at, -1)
        self.assertNotEqual(cant_do_at, -1)
        self.assertLess(
            tool_at,
            cant_do_at,
            "the reminder bullet drifted out of the What-You-Can-Do list — it no longer "
            "inherits the 'never say you can't' umbrella that makes it work",
        )

    def test_anti_fabrication_clause_is_present(self):
        """Run 33: with NO cron tool loaded at all, the assistant still said "All set!
        I'll ping you at 3pm" — word-for-word identical to the run where the cron was
        genuinely created. The user cannot tell those apart. The clause must cover the
        tool-ABSENT case, not just the call-failed case.
        """
        md = _agents_md()
        self.assertIn("after the tool returns success THIS turn", md)
        self.assertIn("if the tool can't be found or the call fails", md)


@override_settings(GRAVITY_ENABLED=True)
class MaximalTenantBudgetTest(TestCase):
    """The regression test for the silent-truncation class. It did not exist, and its
    absence is why the 2026-07-11 canary lost its tail with CI green."""

    def _tenant(self, *, all_gates: bool, extras: int = 0) -> Tenant:
        tenant = create_tenant(display_name="Budget", telegram_chat_id=920001)
        # MJ's four (finance_active is derived from finance_enabled + GRAVITY_ENABLED).
        tenant.finance_enabled = True
        tenant.friends_enabled = True
        tenant.friends_agent_propose_enabled = True
        tenant.document_ingestion_enabled = True
        tenant.experimental_typed_crons = True
        fields = [
            "finance_enabled",
            "friends_enabled",
            "friends_agent_propose_enabled",
            "document_ingestion_enabled",
            "experimental_typed_crons",
        ]
        if all_gates:
            # Enable every simultaneously realizable conditional AGENTS.md section.
            tenant.site_publishing_enabled = True
            tenant.site_editor_enabled = True
            tenant.situational_context_enabled = True
            tenant.email_provenance_enabled = True
            tenant.sautai_enabled = True
            tenant.tour_guide_enabled = True
            tenant.tour_guide_manifest_ok = True
            tenant.places_search_manifest_ok = True
            tenant.journal_shaping_enabled = True
            fields += [
                "site_publishing_enabled",
                "site_editor_enabled",
                "situational_context_enabled",
                "email_provenance_enabled",
                "sautai_enabled",
                "tour_guide_enabled",
                "tour_guide_manifest_ok",
                "places_search_manifest_ok",
                "journal_shaping_enabled",
            ]
            # EXCLUDED: Neighborhood's absorb-only branch; propose-enabled is longer and mutually exclusive.
            # EXCLUDED: basic-tool and unverified-doc tour branches; verified places search is the longest variant.
            # EXCLUDED: agents_md/quick_replies prompt extras; tenant-authored and unbounded, covered below.
            # EXCLUDED: channel/privacy/docs/templates conditionals; they render separate files, not AGENTS.md.
        tenant.save(update_fields=fields)

        if extras:
            prefs = tenant.user.preferences or {}
            prefs["prompt_extras"] = {"agents_md": "x" * extras}
            tenant.user.preferences = prefs
            tenant.user.save(update_fields=["preferences"])
            tenant.user.refresh_from_db()
        return tenant

    def test_maximal_render_stays_under_the_cap(self):
        """Every AGENTS.md tenant gate on, no extras — all code-controlled prose."""
        md = _agents_md(self._tenant(all_gates=True))
        for marker in (
            "## Portfolio publish gate",
            "## Website edit gate",
            "## Current location",
            "## Neighborhood — you are BACKSTAGE",
            "## Save with its source attached",
            "## Saving what you learn from an email",
            "## Meal plans (sautai)",
            "## Tour guide",
            "## Journal shaping",
            "## Gravity Observation Mode",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, md)
        self.assertIn("nbhd_places_search", md)
        self.assertLessEqual(
            len(md),
            BOOTSTRAP_MAX_CHARS,
            f"the maximal AGENTS.md render is {len(md)} chars, over the "
            f"{BOOTSTRAP_MAX_CHARS} deterministic-delivery cap",
        )
        self.assertLessEqual(
            len(md),
            _ALL_GATES_CEILING,
            f"the all-gates AGENTS.md render is {len(md)} chars, over the "
            f"{_ALL_GATES_CEILING} ceiling (cap {BOOTSTRAP_MAX_CHARS}). Production "
            "truncates the TAIL silently; fund growth with a trim, or apply P5 by "
            "raising the runtime cap and CI ceiling together, never the ceiling alone.",
        )

    def test_rules_delivery_r0_all_gates_budget(self):
        md = _agents_md(self._tenant(all_gates=True))
        # KSE-5 (2026-08-30): the trimmed Website edit gate is part of the maximal tenant shape.
        self.assertLessEqual(
            len(md),
            23_585,
            "the reviewed all-gates shape, including Website edit, grew beyond its "
            "measured 23,585-char pin; fund further growth with a trim",
        )

    def test_an_mj_shaped_tenant_fits_under_the_cap(self):
        """The shape actually shipping today: MJ's four gates + his ~1.5K of extras."""
        md = _agents_md(self._tenant(all_gates=False, extras=1500))
        self.assertLess(
            len(md),
            BOOTSTRAP_MAX_CHARS,
            f"an MJ-shaped tenant renders {len(md)} chars against a {BOOTSTRAP_MAX_CHARS} "
            "cap — his AGENTS.md tail is being silently truncated in production RIGHT NOW",
        )

    def test_all_gates_plus_extras_fits_under_the_cap(self):
        """Rules-delivery W0 funded 1,500 chars of prompt extras in the maximal shape."""
        md = _agents_md(self._tenant(all_gates=True, extras=1500))
        self.assertLessEqual(
            len(md),
            _ALL_GATES_CEILING,
            f"the measured all-gates + 1,500 extras render is {len(md)} chars, over the "
            f"{_ALL_GATES_CEILING} CI ceiling — it must retain the 50-char margin under "
            f"the {BOOTSTRAP_MAX_CHARS} runtime cap",
        )

    def test_the_reminder_bullet_survives_in_the_full_shape(self):
        # Present in the base template is not enough. It must survive on the tenant whose
        # render actually approaches the cap — otherwise we shipped a rule nobody reads.
        md = _agents_md(self._tenant(all_gates=True))
        self.assertIn(_REMINDER_TOOL, md)
        self.assertLess(md.find(_REMINDER_TOOL), BOOTSTRAP_MAX_CHARS)


class BootstrapCapSentinelTest(TestCase):
    """The render-time alarm. Silent truncation is invisible by definition — this is
    the only thing that makes it visible, per tenant, on every write path."""

    def test_sentinel_errors_when_the_render_approaches_the_cap(self):
        tenant = create_tenant(display_name="Overstuffed", telegram_chat_id=920002)
        prefs = tenant.user.preferences or {}
        prefs["prompt_extras"] = {"agents_md": "x" * BOOTSTRAP_MAX_CHARS}
        tenant.user.preferences = prefs
        tenant.user.save(update_fields=["preferences"])
        tenant.user.refresh_from_db()

        with self.assertLogs("apps.orchestrator.personas", level="ERROR") as logs:
            _agents_md(tenant)
        self.assertTrue(
            any("near bootstrap cap" in line for line in logs.output),
            f"the sentinel did not fire on an over-cap render: {logs.output}",
        )

    def test_sentinel_silent_on_a_normal_render(self):
        tenant = create_tenant(display_name="Normal", telegram_chat_id=920003)
        with self.assertNoLogs("apps.orchestrator.personas", level="ERROR"):
            _agents_md(tenant)


class TypedCronsDefaultTest(TestCase):
    """The prompt promises reminders to every tenant. These two assertions together are
    the guarantee that every tenant can actually keep that promise."""

    def test_new_tenants_get_the_cron_tools_by_default(self):
        """The base AGENTS.md now tells EVERY tenant it can set reminders. A tenant
        without the flag loads no cron-create tools — so it would read that it can set
        reminders and have nothing to do it with, which is the fabrication bug, freshly
        manufactured on every new signup. The default must match what the prompt claims.
        """
        self.assertTrue(Tenant().experimental_typed_crons)

    def test_the_automation_plugin_id_is_env_readable(self):
        """``scripts/openclaw_config_doctor_smoke.sh`` disables this plugin by exporting
        ``OPENCLAW_AUTOMATION_PLUGIN_ID=""`` — the plugin ships in the OpenClaw image but
        CI has no ``/opt/nbhd/plugins`` tree, so the doctor rejects the path.

        That export was a SILENT NO-OP until 2026-07-14: no settings line read the name,
        so ``getattr(settings, "OPENCLAW_AUTOMATION_PLUGIN_ID", "nbhd-automation-tools")``
        in config_generator always won with its hardcoded literal. It hid because the
        tenant flag defaulted False, so the plugin never reached a smoke config at all;
        flipping the default surfaced it instantly as a doctor failure.

        Every sibling plugin ID is env-readable. Pin that this one is too, so a
        smoke/test disable actually disables.
        """
        from django.conf import settings

        self.assertTrue(
            hasattr(settings, "OPENCLAW_AUTOMATION_PLUGIN_ID"),
            "no settings line reads OPENCLAW_AUTOMATION_PLUGIN_ID — the smoke script's "
            "disable is a no-op again and openclaw doctor will reject the config",
        )
        self.assertTrue(hasattr(settings, "OPENCLAW_AUTOMATION_PLUGIN_PATH"))

    def test_the_automation_plugin_actually_loads_on_a_bare_tenant(self):
        """The other half — and the load-bearing half.

        Six ConfigGeneratorTest cases blank ``OPENCLAW_AUTOMATION_PLUGIN_ID`` to keep
        their zero-plugin baseline, which removed the only place this plugin would
        otherwise have appeared in a config assertion. So if the gate at
        ``config_generator.py`` regresses — renamed setting, inverted condition, a
        refactor — every test stays GREEN while the fleet loses its cron-create tools,
        while the base AGENTS.md still promises them to every user.

        That is run 33's failure — the assistant cheerfully saying "All set!" with no
        tool to call — mass-produced across the fleet, with CI reporting success.

        Deliberately NO setting overrides: this pins the REAL default wiring.
        """
        tenant = create_tenant(display_name="Bare", telegram_chat_id=920004)
        plugins = generate_openclaw_config(tenant).get("plugins", {})
        self.assertIn(
            "nbhd-automation-tools",
            plugins.get("entries", {}),
            "the cron-create plugin no longer loads by default — every tenant is now "
            "promised reminders it cannot deliver",
        )
        self.assertIn(
            "/opt/nbhd/plugins/nbhd-automation-tools",
            plugins.get("load", {}).get("paths", []),
        )
