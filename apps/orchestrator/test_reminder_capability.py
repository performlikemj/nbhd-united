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

from apps.orchestrator.config_generator import BOOTSTRAP_MAX_CHARS
from apps.orchestrator.personas import render_workspace_files
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant

_REMINDER_TOOL = "nbhd_cron_create_pure_reminder"
_CANT_DO_HEADING = "## What You Can't Do"

# Budget split, measured 2026-07-14 against the real fleet:
#
#   cap                              24,000
#   template alone                   15,843
#   + every feature flag             21,489   <- the part CODE controls
#   MJ's per-tenant prompt_extras     1,459
#   MJ's actual shipped render       22,948   (1,052 from the cap)
#
# So the code-controlled render must leave room for the extras a real tenant carries.
# This reserve is that room. It is NOT a comfort blanket — widening it to turn a red
# test green is deleting the alarm, not putting out the fire. If the template needs to
# grow, fund it with a trim (the cc1602aa / a5fca659 precedent).
#
# Per-tenant extras are ops data, not code, so CI cannot bound them — the render-time
# sentinel in personas.py is what catches a specific tenant drifting toward the cap.
_TENANT_EXTRAS_RESERVE = 2000


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

    def _all_flags_tenant(self, *, extras: int = 0) -> Tenant:
        tenant = create_tenant(display_name="Maximal", telegram_chat_id=920001)
        tenant.finance_enabled = True
        tenant.friends_enabled = True
        tenant.friends_agent_propose_enabled = True
        tenant.document_ingestion_enabled = True
        tenant.experimental_typed_crons = True
        tenant.save(
            update_fields=[
                "finance_enabled",
                "friends_enabled",
                "friends_agent_propose_enabled",
                "document_ingestion_enabled",
                "experimental_typed_crons",
            ]
        )
        if extras:
            prefs = tenant.user.preferences or {}
            prefs["prompt_extras"] = {"agents_md": "x" * extras}
            tenant.user.preferences = prefs
            tenant.user.save(update_fields=["preferences"])
            tenant.user.refresh_from_db()
        return tenant

    def test_code_controlled_render_leaves_room_for_tenant_extras(self):
        """Every flag on, no extras — this is the whole of what the CODE contributes.

        It must stay far enough under the cap that a real tenant's prompt_extras still
        fit. MJ carries ~1,459 chars of them; the reserve is deliberately larger.
        """
        md = _agents_md(self._all_flags_tenant())
        ceiling = BOOTSTRAP_MAX_CHARS - _TENANT_EXTRAS_RESERVE
        self.assertLess(
            len(md),
            ceiling,
            f"the code-controlled AGENTS.md render (every flag, no extras) is {len(md)} "
            f"chars, over the {ceiling} ceiling ({BOOTSTRAP_MAX_CHARS} cap − "
            f"{_TENANT_EXTRAS_RESERVE} reserved for per-tenant extras). Something grew "
            "without a funding trim. Do NOT widen the reserve — production truncates the "
            "TAIL, silently, and the tail is always the newest behavioural rule.",
        )

    def test_an_mj_shaped_tenant_still_fits_under_the_cap(self):
        """The production shape, pinned: every flag plus MJ's real extras weight."""
        md = _agents_md(self._all_flags_tenant(extras=1500))
        self.assertLess(
            len(md),
            BOOTSTRAP_MAX_CHARS,
            f"an MJ-shaped tenant renders {len(md)} chars against a {BOOTSTRAP_MAX_CHARS} "
            "cap — his AGENTS.md tail is being silently truncated in production RIGHT NOW",
        )

    def test_the_reminder_bullet_survives_in_the_full_shape(self):
        # Present in the base template is not enough. It must survive on the tenant whose
        # render actually approaches the cap — otherwise we shipped a rule nobody reads.
        md = _agents_md(self._all_flags_tenant(extras=1500))
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
    def test_new_tenants_get_the_cron_tools_by_default(self):
        """The base AGENTS.md now tells EVERY tenant it can set reminders. A tenant
        without the flag loads no cron-create tools — so it would read that it can set
        reminders and have nothing to do it with, which is the fabrication bug, freshly
        manufactured on every new signup. The default must match what the prompt claims.
        """
        self.assertTrue(Tenant().experimental_typed_crons)
