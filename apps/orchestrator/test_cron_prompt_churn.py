"""Regression coverage for the OpenClaw cron-prompt churn bug.

The bug (observed 2026-05-10 on canary post-OC-5.7 transition):
``update_system_cron_prompts`` was recreating all 8 system crons on every
wake. The trigger was a single-byte mismatch in the comparison: OpenClaw
``.trim()``s ``payload.message`` on store (see
``coercePayload`` in ``openclaw-tools-*.js``), but Django's
``_build_cron_message`` returned a string ending with ``\n`` (from the
trailing newline in ``_phase2_sync_block``). Stored without trailing
``\n``, generated with → string equality fails → delete+create cycle for
every system job → 16 gateway mutations per wake → gateway thrash → SIGTERM
right when the scheduled cron tried to fire. See
``project_openclaw_cron_payload_shape.md`` for the full timeline.

These tests pin the contract: whatever ``_build_cron_message`` returns
must equal what OpenClaw stores back (i.e., it must already be in OC's
trimmed normal form). If that breaks, ``update_system_cron_prompts`` will
silently re-introduce the churn.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from apps.orchestrator.config_generator import _build_cron_message, build_cron_seed_jobs
from apps.orchestrator.services import update_system_cron_prompts
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


def _oc_normalize(message: str) -> str:
    """Mirror OpenClaw 2026.5.7's ``coercePayload`` normalization.

    OC strips leading/trailing whitespace via ``normalizeOptionalString``
    (``value?.trim()``). Internal whitespace is preserved. Anything else
    is a contract change in OC and this helper must be re-derived from
    ``openclaw-tools-*.js`` per ``reference_openclaw_source_extraction.md``.
    """
    return message.strip()


def _list_response(jobs: list[dict]) -> dict:
    """Wrap a job list in OpenClaw's ``cron.list`` envelope shape.

    OC 5.7 returns ``{ jobs: [...], total, offset, limit, hasMore,
    nextOffset, deliveryPreviews }`` (see ``server-cron-CM4aws4s.js``
    ``listPage`` and ``server-methods-DStUV8Sh.js`` ``cron.list``).
    Django unwraps via ``list_result.get("details", list_result)`` then
    pulls ``.jobs`` — works for both raw and ``details``-wrapped shapes.
    """
    return {
        "jobs": jobs,
        "total": len(jobs),
        "offset": 0,
        "limit": len(jobs),
        "hasMore": False,
        "nextOffset": None,
        "deliveryPreviews": [],
    }


class CronPromptStableComparisonTests(TestCase):
    """Pin: a freshly-generated message body must equal the OC-stored form.

    If these break, every wake will silently start recreating all 8 system
    crons again — the exact failure mode that took down canary on 2026-05-10.
    """

    def setUp(self):
        self.tenant = create_tenant(
            display_name="Cron Churn Test",
            telegram_chat_id=987654321,
        )
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_id = "oc-churn-test"
        self.tenant.container_fqdn = "oc-churn-test.internal.example.io"
        self.tenant.save()

    def test_build_cron_message_already_in_oc_normalized_form(self):
        """Generated message must equal its OC-trim normalized form.

        If this fails, OC will store a different string than Django generated,
        and the next ``update_system_cron_prompts`` comparison will see a
        mismatch and recreate the cron — kicking off churn.
        """
        # Pick any foreground job — the trailing-newline bug came from the
        # Phase 2 sync block, so foreground crons exercise the failure path.
        jobs = build_cron_seed_jobs(self.tenant)
        morning = next(j for j in jobs if j["name"] == "Morning Briefing")
        generated = morning["payload"]["message"]

        self.assertEqual(
            generated,
            _oc_normalize(generated),
            "Generated cron message has leading/trailing whitespace that "
            "OpenClaw will strip on store. This causes every wake to see "
            "a mismatch and recreate the cron. Strip in _build_cron_message.",
        )

    def test_build_cron_message_stable_across_calls(self):
        """Two calls produce the same body modulo the date preamble.

        Anything else (different Phase 2 wrapping, model name drift, etc.)
        would also surface as churn even with the trim fix.
        """
        message_one = _build_cron_message(
            "Test prompt body.",
            "Morning Briefing",
            foreground=True,
            tenant=self.tenant,
        )
        message_two = _build_cron_message(
            "Test prompt body.",
            "Morning Briefing",
            foreground=True,
            tenant=self.tenant,
        )
        # Strip the date preamble (which legitimately drifts every minute);
        # everything else must be byte-stable.
        self.assertEqual(
            message_one[message_one.find("\n\n") + 2 :],
            message_two[message_two.find("\n\n") + 2 :],
        )


class UpdateSystemCronPromptsNoChurnTests(TestCase):
    """End-to-end: when stored crons match desired, no recreate happens.

    This is the integration-level guard against the canary bug. If
    ``_build_cron_message`` ever drifts from OC's normalized form again,
    these tests will catch it before deploy.
    """

    def setUp(self):
        self.tenant = create_tenant(
            display_name="No Churn Test",
            telegram_chat_id=192837465,
        )
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_id = "oc-no-churn"
        self.tenant.container_fqdn = "oc-no-churn.internal.example.io"
        self.tenant.save()

    def _existing_jobs_from_seed(self, *, normalize: bool = True) -> list[dict]:
        """Build an ``existing_jobs`` fixture from build_cron_seed_jobs.

        Mirrors what OC would return on ``cron.list`` if we'd previously
        created these jobs via ``cron.add``. ``normalize=True`` applies
        the same ``.trim()`` OC does at store time.
        """
        existing = []
        for desired in build_cron_seed_jobs(self.tenant):
            payload = dict(desired["payload"])
            if normalize and "message" in payload:
                payload["message"] = _oc_normalize(payload["message"])
            existing.append(
                {
                    "id": f"job-{desired['name']}",
                    "name": desired["name"],
                    "schedule": desired["schedule"],
                    "sessionTarget": desired["sessionTarget"],
                    "payload": payload,
                    "delivery": desired.get("delivery", {}),
                    "enabled": desired.get("enabled", True),
                }
            )
        return existing

    @patch("apps.orchestrator.services.invoke_gateway_tool")
    def test_no_recreate_when_existing_matches_oc_normalized_form(self, mock_invoke):
        """The canary failure mode in test form.

        If OpenClaw's stored shape and Django's generated shape agree (after
        OC's trim), no cron mutations should fire.
        """
        existing = self._existing_jobs_from_seed(normalize=True)
        mock_invoke.return_value = _list_response(existing)

        result = update_system_cron_prompts(self.tenant)

        # cron.list (1 call) + sync_heartbeat_cron (1 fetch we re-use existing
        # for) — but no cron.remove/add/update calls.
        mutating_calls = [
            call for call in mock_invoke.call_args_list if call.args[1] in ("cron.add", "cron.remove", "cron.update")
        ]
        self.assertEqual(
            mutating_calls,
            [],
            f"Expected zero cron mutations; got: "
            f"{[(c.args[1], c.args[2].get('jobId') or c.args[2].get('job', {}).get('name')) for c in mutating_calls]}",
        )
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["errors"], 0)

    @patch("apps.orchestrator.services.invoke_gateway_tool")
    def test_recreate_when_prompt_genuinely_changed(self, mock_invoke):
        """Sanity: if the stored message body actually differs, do recreate.

        Guards against an over-eager fix that suppresses ALL recreates
        instead of just the spurious ones.
        """
        existing = self._existing_jobs_from_seed(normalize=True)
        # Mutate one cron's message to simulate a genuine prompt drift
        # (e.g., template was updated server-side after this cron was created).
        morning_idx = next(i for i, e in enumerate(existing) if e["name"] == "Morning Briefing")
        existing[morning_idx]["payload"]["message"] = (
            "Current date and time: Friday, May 8, 2026 at 07:00 (UTC)\n"
            "When mentioning future events, ... days from now. "
            "Never say 'tomorrow' unless the math confirms exactly 1 day away.\n\n"
            "Good morning! Create today's morning briefing"
            "  ← old prompt body, missing the new mandatory preamble"
        )
        mock_invoke.return_value = _list_response(existing)

        update_system_cron_prompts(self.tenant)

        recreate_pairs = [
            call.args[1] for call in mock_invoke.call_args_list if call.args[1] in ("cron.remove", "cron.add")
        ]
        # Exactly one recreate cycle for Morning Briefing: 1 remove + 1 add.
        self.assertEqual(
            recreate_pairs.count("cron.remove"),
            1,
            "Expected exactly 1 cron.remove for the genuinely-drifted job",
        )
        self.assertEqual(
            recreate_pairs.count("cron.add"),
            1,
            "Expected exactly 1 cron.add for the genuinely-drifted job",
        )
