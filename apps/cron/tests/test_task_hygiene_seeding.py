"""Seeding, gating and contract-baking for the weekly task-hygiene cron.

This cron is a NEW PROACTIVE SENDER — it messages the user without being
asked. So the tests that matter most here are the ones about who gets it and
whether they can get rid of it, not just the ones about what it says:

  - a tenant outside the canary gate must get NOTHING (fails closed)
  - seeding twice must not produce two crons
  - a tenant who DISABLES it must stay rid of it across config applies
  - a config apply must not reap it (the reaper only knows legacy seed names)

Plus the two structural contracts: the toolsAllow the fire-turn actually
ships with, and the outbound contract the pre_save signal bakes into
``data["description"]`` for the in-container enforcement plugin. Both are read
back off a REAL row built by the real service + signal, never hand-assembled —
a hand-fed literal would happily agree with itself while production diverged.
"""

from __future__ import annotations

import json

from django.test import TestCase, override_settings

from apps.cron.models import CronCreationPath, CronJob, CronJobSource
from apps.cron.services import (
    TASK_HYGIENE_CRON_NAME,
    seed_task_hygiene_cron,
    task_hygiene_enabled,
)
from apps.cron.signals import CRON_CONTRACT_PREFIX
from apps.tenants.models import Tenant, User


def _make_tenant(username: str, *, timezone: str = "Asia/Tokyo") -> Tenant:
    user = User.objects.create_user(username=username, password="x" * 32)
    if timezone:
        user.timezone = timezone
        user.save(update_fields=["timezone"])
    return Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE,
        container_id="oc-test",
        container_fqdn="oc-test.internal.azurecontainerapps.io",
        postgres_cron_canonical=True,
    )


class TaskHygieneGateTests(TestCase):
    def setUp(self):
        self.tenant = _make_tenant("hygiene-gate")

    def test_gate_is_closed_by_default(self):
        """The shipped default. Deploying this code must not start messaging
        anybody — the gate opens by an explicit ops action, per tenant."""
        self.assertFalse(task_hygiene_enabled(self.tenant))

    @override_settings(TASK_HYGIENE_TENANT_IDS="")
    def test_empty_allowlist_means_nobody(self):
        self.assertFalse(task_hygiene_enabled(self.tenant))
        result = seed_task_hygiene_cron(self.tenant)
        self.assertFalse(result["created"])
        self.assertEqual(result["reason"], "not_gated")
        self.assertFalse(CronJob.objects.filter(tenant=self.tenant).exists())

    def test_a_tenant_outside_the_allowlist_gets_nothing(self):
        """The canary case: one tenant is open, another must be untouched."""
        other = _make_tenant("hygiene-outsider")
        with override_settings(TASK_HYGIENE_TENANT_IDS=str(self.tenant.id)):
            self.assertTrue(task_hygiene_enabled(self.tenant))
            self.assertFalse(task_hygiene_enabled(other))

            seed_task_hygiene_cron(self.tenant)
            seed_task_hygiene_cron(other)

        self.assertTrue(CronJob.objects.filter(tenant=self.tenant, name=TASK_HYGIENE_CRON_NAME).exists())
        self.assertFalse(CronJob.objects.filter(tenant=other).exists())

    def test_allowlist_accepts_several_ids_and_tolerates_whitespace(self):
        other = _make_tenant("hygiene-second")
        raw = f" {self.tenant.id} , {other.id} "
        with override_settings(TASK_HYGIENE_TENANT_IDS=raw):
            self.assertTrue(task_hygiene_enabled(self.tenant))
            self.assertTrue(task_hygiene_enabled(other))


class TaskHygieneSeedingTests(TestCase):
    def setUp(self):
        self.tenant = _make_tenant("hygiene-seed")

    def seed_gated(self):
        with override_settings(TASK_HYGIENE_TENANT_IDS=str(self.tenant.id)):
            return seed_task_hygiene_cron(self.tenant)

    def test_seeds_one_typed_system_cron(self):
        result = self.seed_gated()
        self.assertTrue(result["created"])

        cron = CronJob.objects.get(tenant=self.tenant, name=TASK_HYGIENE_CRON_NAME)
        self.assertEqual(cron.pattern, "task_hygiene")
        self.assertEqual(cron.creation_path, CronCreationPath.TYPED)
        self.assertEqual(cron.source, CronJobSource.SYSTEM)
        self.assertTrue(cron.managed)
        self.assertTrue(cron.enabled)

    def test_schedule_is_sunday_evening_in_the_tenants_own_timezone(self):
        self.seed_gated()
        cron = CronJob.objects.get(tenant=self.tenant, name=TASK_HYGIENE_CRON_NAME)
        self.assertEqual(
            cron.data["schedule"],
            {"kind": "cron", "expr": "30 18 * * 0", "tz": "Asia/Tokyo"},
        )

    def test_schedule_avoids_the_top_of_the_hour(self):
        """The heartbeat fires at ``0 {heartbeat_start_hour} * * *``. A tenant
        whose window starts at 18 would collide with a round 18:00 hygiene cron
        every Sunday, so the offset is load-bearing, not cosmetic."""
        self.seed_gated()
        cron = CronJob.objects.get(tenant=self.tenant, name=TASK_HYGIENE_CRON_NAME)
        minute = cron.data["schedule"]["expr"].split()[0]
        self.assertNotEqual(minute, "0")

    def test_timezone_falls_back_to_utc_when_the_user_has_none(self):
        tzless = _make_tenant("hygiene-no-tz", timezone="")
        with override_settings(TASK_HYGIENE_TENANT_IDS=str(tzless.id)):
            seed_task_hygiene_cron(tzless)
        cron = CronJob.objects.get(tenant=tzless, name=TASK_HYGIENE_CRON_NAME)
        self.assertEqual(cron.data["schedule"]["tz"], "UTC")

    def test_seeding_twice_creates_only_one_cron(self):
        self.seed_gated()
        second = self.seed_gated()
        self.assertFalse(second["created"])
        self.assertEqual(second["reason"], "already_exists")
        self.assertEqual(CronJob.objects.filter(tenant=self.tenant, name=TASK_HYGIENE_CRON_NAME).count(), 1)

    def test_a_user_who_disables_it_stays_rid_of_it(self):
        """Opting out must be permanent. An idempotency check that only looked
        for ENABLED rows would recreate this on the next config apply and the
        user could never turn the weekly message off."""
        self.seed_gated()
        cron = CronJob.objects.get(tenant=self.tenant, name=TASK_HYGIENE_CRON_NAME)
        cron.enabled = False
        cron.save(update_fields=["enabled"])

        result = self.seed_gated()

        self.assertFalse(result["created"])
        self.assertEqual(CronJob.objects.filter(tenant=self.tenant, name=TASK_HYGIENE_CRON_NAME).count(), 1)
        cron.refresh_from_db()
        self.assertFalse(cron.enabled)


class TaskHygieneGateConvergesOffTests(TestCase):
    """Removing a tenant from the allowlist is the ROLLBACK LEVER for a
    proactive sender. A gate that only guarded creation would leave every
    already-seeded tenant messaging forever, making the canary ladder one-way —
    so un-gating has to actually stop the sending."""

    def setUp(self):
        self.tenant = _make_tenant("hygiene-converge")

    def _seed_gated(self):
        with override_settings(TASK_HYGIENE_TENANT_IDS=str(self.tenant.id)):
            return seed_task_hygiene_cron(self.tenant)

    def _converge_ungated(self):
        with override_settings(TASK_HYGIENE_TENANT_IDS=""):
            return seed_task_hygiene_cron(self.tenant)

    def test_gated_seeds_it_enabled(self):
        self._seed_gated()
        cron = CronJob.objects.get(tenant=self.tenant, name=TASK_HYGIENE_CRON_NAME)
        self.assertTrue(cron.enabled)

    def test_un_gating_disables_the_existing_cron(self):
        self._seed_gated()
        result = self._converge_ungated()

        self.assertEqual(result["reason"], "disabled_ungated")
        cron = CronJob.objects.get(tenant=self.tenant, name=TASK_HYGIENE_CRON_NAME)
        self.assertFalse(cron.enabled)

    def test_un_gating_disables_rather_than_deletes(self):
        """The row is the audit trail — and deleting would also let a later
        re-gate silently resurrect a sender the operator turned off."""
        self._seed_gated()
        self._converge_ungated()
        self.assertEqual(CronJob.objects.filter(tenant=self.tenant, name=TASK_HYGIENE_CRON_NAME).count(), 1)

    def test_converging_off_twice_is_a_no_op(self):
        self._seed_gated()
        self._converge_ungated()
        second = self._converge_ungated()
        self.assertEqual(second["reason"], "not_gated")

    def test_un_gated_with_no_row_creates_nothing(self):
        result = self._converge_ungated()
        self.assertEqual(result["reason"], "not_gated")
        self.assertFalse(CronJob.objects.filter(tenant=self.tenant).exists())

    def test_re_gating_does_not_silently_resurrect_it(self):
        """After an operator rolls a tenant back, putting the id back in the
        allowlist must not flip the sender on again by itself — the row stays
        disabled until someone deliberately re-enables it."""
        self._seed_gated()
        self._converge_ungated()

        self._seed_gated()

        cron = CronJob.objects.get(tenant=self.tenant, name=TASK_HYGIENE_CRON_NAME)
        self.assertFalse(cron.enabled)

    def test_config_apply_converges_off_through_the_real_call_site(self):
        from apps.orchestrator.services import refresh_system_cron_rows_from_seed

        self._seed_gated()
        with override_settings(TASK_HYGIENE_TENANT_IDS=""):
            refresh_system_cron_rows_from_seed(self.tenant)

        cron = CronJob.objects.get(tenant=self.tenant, name=TASK_HYGIENE_CRON_NAME)
        self.assertFalse(cron.enabled)


class TaskHygieneRenderedTurnTests(TestCase):
    """What the fire-turn actually ships with, read off a real derived row."""

    def setUp(self):
        self.tenant = _make_tenant("hygiene-render")
        with override_settings(TASK_HYGIENE_TENANT_IDS=str(self.tenant.id)):
            seed_task_hygiene_cron(self.tenant)
        self.cron = CronJob.objects.get(tenant=self.tenant, name=TASK_HYGIENE_CRON_NAME)

    def test_toolsallow_on_the_real_row_is_pinned_exactly(self):
        self.assertEqual(
            self.cron.data["payload"]["toolsAllow"],
            [
                "nbhd_task_list",
                "nbhd_task_get",
                "nbhd_goal_list",
                "nbhd_current_status",
                "nbhd_task_complete",
                "nbhd_task_skip",
                "nbhd_task_defer",
                "nbhd_send_to_user",
            ],
        )

    def test_the_real_row_carries_no_way_to_create_or_destroy_a_task(self):
        allow = self.cron.data["payload"]["toolsAllow"]
        for forbidden in ("nbhd_task_create", "nbhd_task_update", "nbhd_task_delete", "cron"):
            self.assertNotIn(forbidden, allow)

    def test_the_rendered_prompt_explains_why_it_cannot_delete(self):
        message = self.cron.data["payload"]["message"]
        self.assertIn("nbhd_task_delete", message)
        self.assertIn("not available in this turn", message)
        self.assertIn("confirmation cannot be obtained", message)

    def test_the_rendered_prompt_demands_the_marker_and_one_message(self):
        message = self.cron.data["payload"]["message"]
        self.assertIn("[block: task_hygiene]", message)
        self.assertIn("EXACTLY ONCE", message)
        self.assertIn("send NOTHING AT ALL", message)

    def test_turn_is_isolated_and_delivers_through_django(self):
        self.assertEqual(self.cron.data["sessionTarget"], "isolated")
        self.assertEqual(self.cron.data["wakeMode"], "next-heartbeat")
        self.assertEqual(self.cron.data["delivery"], {"mode": "none"})

    def test_signal_bakes_the_outbound_contract_for_the_enforcement_plugin(self):
        """The in-container plugin reads this off ``description`` at
        ``cron_changed`` — if the pre_save signal stops baking it, fire-time
        enforcement silently stops and nothing else notices."""
        description = self.cron.data["description"]
        self.assertTrue(description.startswith(CRON_CONTRACT_PREFIX))
        contract = json.loads(description[len(CRON_CONTRACT_PREFIX) :])
        self.assertEqual(contract["v"], 1)
        self.assertEqual(contract["pattern"], "task_hygiene")
        self.assertEqual(contract["check"], {"kind": "marker", "marker": "[block: task_hygiene]"})
        self.assertEqual(contract["on_fail"], {"action": "revise_then_allow", "max_revisions": 1})

    def test_signal_bakes_the_fire_time_caps(self):
        """``limits`` is what the plugin blocks on. If the signal stops passing
        it through, the caps vanish silently: the cron still fires, still sends,
        and nothing enforces one-message-only or the mutation ceiling."""
        contract = json.loads(self.cron.data["description"][len(CRON_CONTRACT_PREFIX) :])
        self.assertEqual(contract["limits"], {"sends": 1, "mutations": 10})

    def test_a_pattern_without_limits_bakes_no_limits_key(self):
        """The signal change must be a pure addition. A pure_reminder row baked
        alongside must come out byte-identical to before — no ``limits`` key at
        all, which is what keeps the plugin's cap logic dormant for it."""
        from apps.cron.models import CronPattern
        from apps.cron.services import create_typed_cron

        reminder = create_typed_cron(
            tenant=self.tenant,
            pattern=CronPattern.PURE_REMINDER,
            typed_payload={"text": "Take out trash"},
            name="trash-tuesday",
            schedule={"kind": "cron", "expr": "0 8 * * 2", "tz": "Asia/Tokyo"},
        )
        contract = json.loads(reminder.data["description"][len(CRON_CONTRACT_PREFIX) :])
        self.assertNotIn("limits", contract)


class TaskHygieneSeedPathWiringTests(TestCase):
    """The two call sites, driven for real rather than asserted by inspection."""

    def setUp(self):
        self.tenant = _make_tenant("hygiene-wiring")

    def test_provisioning_seed_path_creates_it_for_a_gated_tenant(self):
        from apps.orchestrator.services import seed_cron_jobs

        with override_settings(TASK_HYGIENE_TENANT_IDS=str(self.tenant.id)):
            seed_cron_jobs(self.tenant)

        self.assertTrue(CronJob.objects.filter(tenant=self.tenant, name=TASK_HYGIENE_CRON_NAME).exists())

    def test_provisioning_seed_path_skips_an_ungated_tenant(self):
        from apps.orchestrator.services import seed_cron_jobs

        with override_settings(TASK_HYGIENE_TENANT_IDS=""):
            seed_cron_jobs(self.tenant)

        self.assertFalse(CronJob.objects.filter(tenant=self.tenant, name=TASK_HYGIENE_CRON_NAME).exists())

    def test_config_apply_converges_an_existing_tenant_added_to_the_gate(self):
        """The canary is an EXISTING tenant — it was provisioned long before
        this cron existed, so the provisioning path will never run for it
        again. The config-apply refresh is what actually delivers it."""
        from apps.orchestrator.services import refresh_system_cron_rows_from_seed

        with override_settings(TASK_HYGIENE_TENANT_IDS=str(self.tenant.id)):
            refresh_system_cron_rows_from_seed(self.tenant)

        self.assertTrue(CronJob.objects.filter(tenant=self.tenant, name=TASK_HYGIENE_CRON_NAME).exists())

    def test_a_later_config_apply_does_not_reap_it(self):
        """The trap this pattern walks into: the refresh reaper deletes managed
        SYSTEM crons absent from ``build_cron_seed_jobs``, and a typed cron is
        absent from it BY CONSTRUCTION. Without the typed-row exclusion the
        hygiene cron would seed, be deleted, reseed, and fire only sometimes."""
        from apps.orchestrator.services import refresh_system_cron_rows_from_seed

        with override_settings(TASK_HYGIENE_TENANT_IDS=str(self.tenant.id)):
            refresh_system_cron_rows_from_seed(self.tenant)
            first = CronJob.objects.get(tenant=self.tenant, name=TASK_HYGIENE_CRON_NAME)

            refresh_system_cron_rows_from_seed(self.tenant)
            refresh_system_cron_rows_from_seed(self.tenant)

        surviving = CronJob.objects.filter(tenant=self.tenant, name=TASK_HYGIENE_CRON_NAME)
        self.assertEqual(surviving.count(), 1)
        # Same row, not a delete-and-recreate churn cycle.
        self.assertEqual(surviving.first().pk, first.pk)
