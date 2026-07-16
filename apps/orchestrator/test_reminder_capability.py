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

# Budget, measured 2026-07-14 against the real fleet and the six AGENTS.md tenant gates
# in personas.py (site_publishing :660, friends :685, document_ingestion :743,
# email_provenance :776, sautai :801, finance_active :819):
#
#   cap                                   24,000
#   template alone                        15,843
#   MJ (four gates + ~1,459 extras)       22,948   <- shipping today, 1,052 from the cap
#   ALL SIX gates, no extras              23,720   <- only 280 from the cap
#   ALL SIX gates + MJ-weight extras      25,222   <- OVER THE CAP. Silently truncated.
#
# That last line is a live, PRE-EXISTING platform bug. This PR did not cause it; it
# exposed it. No such tenant exists yet — MJ has four gates — but he is two flag flips
# away, and the truncation is silent and eats the NEWEST rule first. It is pinned as a
# KNOWN_GAP sentinel below rather than hidden behind an undercounting fixture, and the
# render-time sentinel in personas.py alarms on it in production.
#
# The ceiling here is therefore the strongest TRUE statement available, not the one we
# wish were true. Do not "fix" a red test by widening it — that is deleting the alarm.
# Fund growth with a trim (the cc1602aa / a5fca659 precedent).
_ALL_GATES_CEILING = 23_800


def _agents_md(tenant=None) -> str:
    return render_workspace_files("neighbor", tenant=tenant)["NBHD_AGENTS_MD"]


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
            # The three MJ does NOT have. Omitting these is how a budget fixture
            # silently undercounts by ~2,270 chars and passes while production burns.
            tenant.site_publishing_enabled = True
            tenant.email_provenance_enabled = True
            tenant.sautai_enabled = True
            fields += ["site_publishing_enabled", "email_provenance_enabled", "sautai_enabled"]
        tenant.save(update_fields=fields)

        if extras:
            prefs = tenant.user.preferences or {}
            prefs["prompt_extras"] = {"agents_md": "x" * extras}
            tenant.user.preferences = prefs
            tenant.user.save(update_fields=["preferences"])
            tenant.user.refresh_from_db()
        return tenant

    def test_all_six_gates_render_stays_under_the_cap(self):
        """Every AGENTS.md tenant gate on, no extras — the whole of what CODE controls.

        Measured 23,720 on 2026-07-14: only 280 chars clear of the cap. The ceiling is
        set just above that on purpose, so the very next gate — or any unfunded growth
        in one — turns this red, which is precisely when the diet is due.
        """
        md = _agents_md(self._tenant(all_gates=True))
        self.assertLess(
            len(md),
            _ALL_GATES_CEILING,
            f"the all-gates AGENTS.md render is {len(md)} chars, over the "
            f"{_ALL_GATES_CEILING} ceiling (cap {BOOTSTRAP_MAX_CHARS}). Something grew "
            "without a funding trim. Do NOT widen the ceiling — production truncates the "
            "TAIL silently, and the tail is always the newest behavioural rule.",
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

    def test_KNOWN_GAP_all_gates_plus_extras_exceeds_the_cap(self):
        """KNOWN_GAP — a live platform bug this PR EXPOSED, and did not cause.

        A tenant with all six gates AND MJ-weight prompt_extras renders ~25,222 chars
        against a 24,000 cap: silently truncated, newest rule first. No such tenant
        exists yet (MJ has four gates), but he is two flag flips from it, and the
        failure mode is invisible by construction.

        Pinned green here so the gap is COUNTED rather than hidden behind an
        undercounting fixture. The render-time sentinel in personas.py alarms on it in
        production in the meantime.

        FLIPS WHEN: the site-publishing (~1,005 chars) and email-provenance (~820) gate
        prose are dieted — ~1,400 chars. That is its own reviewed PR, because that prose
        encodes anti-confabulation incidents and must not be trimmed casually. When it
        lands, this assertion goes RED: delete the sentinel and assert the render fits
        under the cap with real margin.
        """
        md = _agents_md(self._tenant(all_gates=True, extras=1500))
        self.assertGreater(
            len(md),
            BOOTSTRAP_MAX_CHARS,
            f"the all-gates + extras render is now {len(md)} chars, under the "
            f"{BOOTSTRAP_MAX_CHARS} cap — the gate diet has landed. DELETE this KNOWN_GAP "
            "sentinel and replace it with a real under-cap assertion.",
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
