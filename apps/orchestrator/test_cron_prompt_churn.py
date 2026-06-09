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


def _oc_stored_payload(desired_job: dict) -> dict:
    """Return the payload as OpenClaw would store it from a cron.add.

    Two normalizations matter for the diff:
    1. ``message`` gets `.trim()` (PR #505).
    2. Top-level ``model`` in the cron def gets folded into
       ``payload.model`` (observed on canary 2026-05-12 Heartbeat:
       _build_heartbeat_cron returns ``"model": HEARTBEAT_MODEL`` at
       the top level, and ``cron.list`` then reports
       ``payload.model = HEARTBEAT_MODEL``).

    Fixtures that simulate "OpenClaw's view of an existing cron" should
    use this helper so the diff isn't fed an artificially-pristine
    payload that diverges from runtime reality.
    """
    payload = dict(desired_job.get("payload", {}))
    if "message" in payload:
        payload["message"] = _oc_normalize(payload["message"])
    top_level_model = desired_job.get("model")
    if top_level_model and "model" not in payload:
        payload["model"] = top_level_model
    return payload


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
        OC's full storage normalization (message trim + top-level
        ``model`` folded into ``payload.model``).
        """
        existing = []
        for desired in build_cron_seed_jobs(self.tenant):
            payload = _oc_stored_payload(desired) if normalize else dict(desired["payload"])
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


class UpdateSystemCronPromptsNonMessageDriftTests(TestCase):
    """Pin the 2026-05-12 canary bug: stale `payload.model` in OpenClaw
    runtime that Django stopped setting. The diff was message-only, so a
    `model = anthropic-cli/...` value left over from BYO setup persisted
    indefinitely and broke every Evening Check-in cron fire at preflight.

    These tests cover:
    - The Layer 1 invariant: Django's `build_cron_seed_jobs` doesn't set
      `payload.model` for any non-Heartbeat system cron (so a future
      regression that re-introduces the field can't slip in).
    - The Layer 2 corrective: `update_system_cron_prompts` recreates a
      cron when only `payload.model` drifts, including when the message
      is user-customized (the user can re-prompt; a stale model can't
      be patched at fire time).
    """

    def setUp(self):
        self.tenant = create_tenant(
            display_name="Payload Drift Test",
            telegram_chat_id=555444333,
        )
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_id = "oc-payload-drift"
        self.tenant.container_fqdn = "oc-payload-drift.internal.example.io"
        self.tenant.save()

    def test_layer1_no_system_cron_sets_payload_model(self):
        """Django's seed payloads must omit `payload.model` for system
        crons that should fall through to `agents.defaults.model.primary`.

        Heartbeat is the only exception — it explicitly pins a cheap
        model (DEEPSEEK_FLASH_MODEL) regardless of the tenant's tier or BYO
        state, and that model is always in the allowlist for every
        tier. If Heartbeat's pinned model ever drifts off the allowlist,
        we'd have the same class of preflight rejection.
        """
        jobs = build_cron_seed_jobs(self.tenant)
        from apps.orchestrator.config_generator import _build_heartbeat_cron

        heartbeat = _build_heartbeat_cron(self.tenant)
        if heartbeat is not None:
            jobs = [*jobs, heartbeat]

        offending = []
        for job in jobs:
            payload = job.get("payload", {})
            if "model" not in payload:
                continue
            if job["name"] == "Heartbeat Check-in":
                # Heartbeat is allowed to pin — but the value must be a
                # canonical openrouter/* model so it lives in every tier's
                # allowlist.
                model = payload["model"]
                self.assertTrue(
                    model.startswith("openrouter/"),
                    f"Heartbeat payload.model {model!r} must be openrouter/* "
                    "to survive in every tier's agents.defaults.models allowlist",
                )
                continue
            offending.append((job["name"], payload["model"]))

        self.assertEqual(
            offending,
            [],
            f"System crons must not pin payload.model — let them fall through to "
            f"agents.defaults.model.primary so model switches (e.g. BYO ↔ OpenRouter) "
            f"take effect immediately. Offenders: {offending}",
        )

    @patch("apps.orchestrator.services.invoke_gateway_tool")
    def test_layer2_recreate_on_stale_model_with_default_message(self, mock_invoke):
        """Canary 2026-05-12 reproduction: OpenClaw runtime has
        `payload.model = anthropic-cli/...` left from BYO setup; Django's
        desired payload has no model field. The diff must trigger
        delete+create, even though the message matches.
        """
        existing = self._existing_jobs_from_seed_with_stale_model()
        mock_invoke.return_value = _list_response(existing)

        update_system_cron_prompts(self.tenant)

        evening_remove = [
            call
            for call in mock_invoke.call_args_list
            if call.args[1] == "cron.remove" and call.args[2].get("jobId") == "job-Evening Check-in"
        ]
        evening_add = [
            call
            for call in mock_invoke.call_args_list
            if call.args[1] == "cron.add" and call.args[2].get("job", {}).get("name") == "Evening Check-in"
        ]
        self.assertEqual(
            len(evening_remove),
            1,
            "Expected cron.remove for Evening Check-in (stale payload.model drift)",
        )
        self.assertEqual(
            len(evening_add),
            1,
            "Expected cron.add for Evening Check-in to repair stale model",
        )
        added_payload = evening_add[0].args[2]["job"]["payload"]
        self.assertNotIn(
            "model",
            added_payload,
            "Re-added Evening Check-in payload must omit `model` so the agent "
            "uses agents.defaults.model.primary at fire time",
        )

    @patch("apps.orchestrator.services.invoke_gateway_tool")
    def test_layer2_recreate_on_stale_model_with_custom_message(self, mock_invoke):
        """Even with a user-customized prompt, stale `payload.model`
        must force recreate — the cron is silently failing preflight
        otherwise, and a custom prompt the user can re-write is more
        recoverable than a cron that never fires.
        """
        existing = self._existing_jobs_from_seed_with_stale_model()
        # Mark Evening Check-in's message as user-customized so it would
        # be skipped under the old (message-only) policy.
        evening_idx = next(i for i, e in enumerate(existing) if e["name"] == "Evening Check-in")
        existing[evening_idx]["payload"]["message"] = (
            "Custom user prompt — totally rewritten, none of the default prefixes."
        )
        mock_invoke.return_value = _list_response(existing)

        update_system_cron_prompts(self.tenant)

        recreate_evening = [
            call
            for call in mock_invoke.call_args_list
            if call.args[1] in ("cron.remove", "cron.add")
            and (
                call.args[2].get("jobId") == "job-Evening Check-in"
                or call.args[2].get("job", {}).get("name") == "Evening Check-in"
            )
        ]
        self.assertEqual(
            len(recreate_evening),
            2,
            "Stale model must force recreate even when the message is custom — "
            "a silently-failing cron is worse than a re-customizable prompt",
        )

    @patch("apps.orchestrator.services.invoke_gateway_tool")
    def test_no_churn_when_heartbeat_model_at_top_level(self, mock_invoke):
        """Heartbeat pins its model at the TOP LEVEL of the cron def
        (see `_build_heartbeat_cron`: `"model": HEARTBEAT_MODEL` outside
        the payload block). OpenClaw normalizes top-level `model` to
        `payload.model` on cron.add. The diff must understand this OR
        a no-op recreate fires for Heartbeat on every sweep — same churn
        class PR #505 was meant to fix, just re-introduced via PR #532.

        Observed on canary 2026-05-12 16:11 UTC: post-PR-#532 first
        sweep recreated Heartbeat alongside the genuinely-drifted Evening
        Check-in / Morning Briefing — wasteful gateway mutations every
        hour that don't change the runtime state. This test pins that
        Heartbeat's top-level `model` IS recognized as matching the
        stored `payload.model`.
        """
        # Use the shared helper that mirrors OC's full storage normalization
        # (message trim + top-level model folded into payload.model).
        existing = []
        for desired in build_cron_seed_jobs(self.tenant):
            existing.append(
                {
                    "id": f"job-{desired['name']}",
                    "name": desired["name"],
                    "schedule": desired["schedule"],
                    "sessionTarget": desired["sessionTarget"],
                    "payload": _oc_stored_payload(desired),
                    "delivery": desired.get("delivery", {}),
                    "enabled": desired.get("enabled", True),
                }
            )
        # Sanity: Heartbeat MUST be in the fixture with payload.model set —
        # otherwise this test is just exercising the regular no-drift path.
        from apps.orchestrator.config_generator import HEARTBEAT_MODEL

        heartbeat_existing = next(e for e in existing if e["name"] == "Heartbeat Check-in")
        self.assertEqual(
            heartbeat_existing["payload"].get("model"),
            HEARTBEAT_MODEL,
            "Fixture sanity: Heartbeat must have payload.model populated "
            "from the top-level cron-def model field via _oc_stored_payload",
        )
        mock_invoke.return_value = _list_response(existing)

        update_system_cron_prompts(self.tenant)

        heartbeat_mutations = [
            call
            for call in mock_invoke.call_args_list
            if call.args[1] in ("cron.remove", "cron.add", "cron.update")
            and (
                call.args[2].get("jobId") == "job-Heartbeat Check-in"
                or call.args[2].get("job", {}).get("name") == "Heartbeat Check-in"
            )
        ]
        self.assertEqual(
            heartbeat_mutations,
            [],
            "Heartbeat must NOT churn just because its model is at top-level "
            "of the cron def — OpenClaw stores top-level model as payload.model, "
            "so existing.payload.model == desired.model. Mutations seen: "
            f"{[(c.args[1], c.args[2]) for c in heartbeat_mutations]}",
        )

    def _existing_jobs_from_seed_with_stale_model(self) -> list[dict]:
        """Build `existing_jobs` where Evening Check-in carries the
        2026-05-12 stale BYO model in its payload. All other crons
        mirror OC's storage normalization exactly (so the diff is
        scoped to Evening Check-in's drift).
        """
        existing = []
        for desired in build_cron_seed_jobs(self.tenant):
            payload = _oc_stored_payload(desired)
            if desired["name"] == "Evening Check-in":
                payload["model"] = "anthropic-cli/claude-sonnet-4-6"
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
