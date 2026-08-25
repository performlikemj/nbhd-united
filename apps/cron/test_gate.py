from __future__ import annotations

import inspect
import json
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.actions.models import (
    ActionAuditLog,
    ActionAuditOutcome,
    ActionStatus,
    ActionType,
    CronDispatch,
    CronDispatchState,
    GatePreference,
    PendingAction,
)
from apps.actions.origin import OriginStamp
from apps.actions.services import should_auto_approve
from apps.actions.tasks import expire_stale_pending_actions, replay_stale_cron_dispatches
from apps.actions.views import GateRespondView
from apps.cron.models import CronJob
from apps.cron.services import TypedCronError
from apps.datebook.test_b2a import _command_gate_payload
from apps.datebook.tests import _ready_tenant
from apps.tenants.services import create_tenant
from apps.tenants.test_utils import seed_internal_key

from .gate import (
    CronGateConflict,
    approve_cron_action,
    cron_gate_enabled,
    dispatch_cron_action,
    request_cron_action,
)

RECURRING = {"kind": "cron", "expr": "0 8 * * 2", "tz": "Asia/Tokyo"}


@override_settings(NBHD_INTERNAL_API_KEY="cron-gate-key", NBHD_DISABLE_BACKGROUND_THREADS=True)
class CronGateServiceTests(TestCase):
    def setUp(self):
        self.tenant = seed_internal_key(create_tenant(display_name="Cron Gate", telegram_chat_id=88101))
        self.tenant.postgres_cron_canonical = True
        self.tenant.save(update_fields=["postgres_cron_canonical"])

    def _request(self, request_id="req-1", **overrides):
        values = {
            "cron_request_id": request_id,
            "pattern": "pure_reminder",
            "name": "Tuesday trash",
            "schedule": RECURRING,
            "typed_payload": {"text": "Take out trash"},
            "reason": "A useful weekly reminder",
            "origin_stamp": OriginStamp(),
        }
        values.update(overrides)
        with patch("apps.actions.messaging.send_gate_confirmation", return_value=True):
            return request_cron_action(self.tenant, **values)

    def _approve_without_callback(self, action):
        with self.captureOnCommitCallbacks(execute=False):
            GateRespondView.resolve_action(
                action_id=action.id,
                response_action="approve",
                tenant=self.tenant,
            )
        action.refresh_from_db()
        return CronDispatch.objects.select_related("action", "cron").get(action=action)

    def test_review_always_even_when_global_gate_and_preference_are_off(self):
        self.tenant.gate_all_actions = False
        self.tenant.gate_acknowledged_risk = True
        self.tenant.save(update_fields=["gate_all_actions", "gate_acknowledged_risk"])
        GatePreference.objects.create(
            tenant=self.tenant,
            action_type=ActionType.CRON_CREATE,
            require_confirmation=False,
        )

        result = self._request()

        action = PendingAction.objects.get(pk=result["action_id"])
        self.assertEqual(action.status, ActionStatus.PENDING)
        self.assertFalse(should_auto_approve(self.tenant, ActionType.CRON_CREATE))
        self.assertLess(abs((action.expires_at - action.created_at) - timedelta(hours=72)), timedelta(seconds=1))

    def test_approval_code_does_not_read_origin_or_auto_approval_policy(self):
        source = inspect.getsource(approve_cron_action)
        self.assertNotIn("origin", source)
        self.assertNotIn("should_auto_approve", source)
        self.assertNotIn("GatePreference", source)

    def test_static_validation_rejects_past_at_before_proposal(self):
        with self.assertRaises(TypedCronError) as caught:
            self._request(
                schedule={"kind": "at", "at": (timezone.now() - timedelta(minutes=1)).isoformat()},
            )
        self.assertEqual(caught.exception.code, "at_in_past")
        self.assertFalse(PendingAction.objects.exists())

    def test_idempotency_matrix(self):
        first = self._request(request_id="same-id")
        same = self._request(request_id="same-id")
        self.assertTrue(first["_created"])
        self.assertFalse(same["_created"])
        self.assertEqual(same["action_id"], first["action_id"])

        with self.assertRaises(CronGateConflict) as caught:
            self._request(request_id="same-id", typed_payload={"text": "Different"})
        self.assertEqual(caught.exception.code, "request_id_conflict")

        auto_first = self._request(request_id=None, name="Auto id", typed_payload={"text": "Auto"})
        auto_same = self._request(request_id=None, name="Auto id", typed_payload={"text": "Auto"})
        self.assertEqual(auto_same["action_id"], auto_first["action_id"])
        auto_action = PendingAction.objects.get(pk=auto_first["action_id"])
        self.assertRegex(auto_action.cron_request_id, r"^auto:[0-9a-f]{32}$")

        logical = self._request(request_id="different-id", name="Auto id", typed_payload={"text": "Auto"})
        self.assertFalse(logical["_created"])
        self.assertEqual(logical["action_id"], auto_first["action_id"])

    def test_pii_is_placeholdered_in_review_and_rehydrated_for_owner_execution(self):
        self.tenant.pii_entity_map = {"[PERSON_1]": {"name": "Alice"}}
        self.tenant.save(update_fields=["pii_entity_map"])

        result = self._request(
            request_id="pii",
            name="[PERSON_1] check-in",
            typed_payload={"text": "Message [PERSON_1]"},
            reason="[PERSON_1] asked for it",
        )

        action = PendingAction.objects.get(pk=result["action_id"])
        self.assertIn("[PERSON_1]", str(action.action_payload))
        self.assertIn("[PERSON_1]", action.display_summary)
        self.assertIn("Alice", result["summary"])
        approve_cron_action(action)
        cron = CronJob.objects.get(tenant=self.tenant, name="Alice check-in")
        self.assertIn("Alice", cron.typed_payload["text"])

    def test_name_conflict_at_proposal_and_revalidation_at_approval(self):
        CronJob.objects.create(tenant=self.tenant, name="Taken", enabled=True)
        with self.assertRaises(CronGateConflict) as caught:
            self._request(name="Taken")
        self.assertEqual(caught.exception.code, "name_conflict")

        proposed = self._request(request_id="race", name="Race")
        CronJob.objects.create(tenant=self.tenant, name="Race", enabled=True)
        action = PendingAction.objects.get(pk=proposed["action_id"])
        data = approve_cron_action(action)
        self.assertEqual(data, {"status": "approved", "execution": "failed", "code": "create_failed:name"})
        self.assertTrue(
            ActionAuditLog.objects.filter(
                tenant=self.tenant,
                action_type=ActionType.CRON_CREATE,
                result=ActionAuditOutcome.FAILED,
                detail_code="create_failed:name",
            ).exists()
        )

    def test_at_is_revalidated_as_future_at_approval(self):
        fires_at = timezone.now() + timedelta(minutes=2)
        proposed = self._request(
            request_id="soon",
            name="Soon",
            schedule={"kind": "at", "at": fires_at.isoformat()},
        )
        action = PendingAction.objects.get(pk=proposed["action_id"])
        data = approve_cron_action(action, responded_at=fires_at + timedelta(seconds=1))
        self.assertEqual(data["code"], "create_failed:past")
        self.assertFalse(CronJob.objects.filter(tenant=self.tenant, name="Soon").exists())

    def test_recurring_outbox_defers_qstash_until_commit_then_audits_executed(self):
        action = PendingAction.objects.get(pk=self._request()["action_id"])
        with (
            patch("apps.cron.publish.publish_task") as publisher,
            patch("apps.cron.gateway_client.invoke_gateway_tool") as gateway,
        ):
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                _action, data, response_status = GateRespondView.resolve_action(
                    action_id=action.id,
                    response_action="approve",
                    tenant=self.tenant,
                )
                publisher.assert_not_called()
                gateway.assert_not_called()
            self.assertEqual(response_status, 200)
            self.assertEqual(data["execution"], "queued")
            self.assertTrue(CronDispatch.objects.filter(action=action).exists())
            for callback in callbacks:
                callback()

        publisher.assert_called_once()
        gateway.assert_not_called()
        action.refresh_from_db()
        self.assertEqual(action.resolution_code, "executed")
        self.assertTrue(
            ActionAuditLog.objects.filter(
                action_type=ActionType.CRON_CREATE, result=ActionAuditOutcome.EXECUTED
            ).exists()
        )

    def test_at_outbox_defers_gateway_and_failure_disables_local_name(self):
        action = PendingAction.objects.get(
            pk=self._request(
                request_id="at-fail",
                name="At failure",
                schedule={"kind": "at", "at": (timezone.now() + timedelta(hours=1)).isoformat()},
            )["action_id"]
        )
        with patch("apps.cron.gateway_client.invoke_gateway_tool", side_effect=RuntimeError("offline")) as gateway:
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                GateRespondView.resolve_action(
                    action_id=action.id,
                    response_action="approve",
                    tenant=self.tenant,
                )
                gateway.assert_not_called()
            for callback in callbacks:
                callback()

        action.refresh_from_db()
        cron = CronJob.objects.get(approval_dispatch__action=action)
        self.assertFalse(cron.enabled)
        self.assertEqual(action.resolution_code, "dispatch_failed")
        self.assertFalse(CronJob.objects.filter(tenant=self.tenant, name="At failure", enabled=True).exists())

    def test_crash_window_sweeper_executes_committed_dispatch_exactly_once(self):
        action = PendingAction.objects.get(pk=self._request(request_id="crash-window")["action_id"])
        dispatch = self._approve_without_callback(action)
        CronDispatch.objects.filter(pk=dispatch.pk).update(created_at=timezone.now() - timedelta(minutes=6))

        with (
            patch("apps.cron.signals._enqueue_regen", return_value=True) as enqueue,
            patch("apps.actions.messaging.update_gate_message"),
            patch("apps.datebook.notify.dispatch_datebook_gate_changed"),
        ):
            first = replay_stale_cron_dispatches()
            second = replay_stale_cron_dispatches()

        dispatch.refresh_from_db()
        self.assertEqual(first, "Cron dispatch replayed 1: executed=1 failed=0 queued=0 busy=0")
        self.assertEqual(second, "Cron dispatch replayed 0: executed=0 failed=0 queued=0 busy=0")
        self.assertEqual(dispatch.state, CronDispatchState.EXECUTED)
        self.assertEqual(dispatch.attempts, 1)
        enqueue.assert_called_once_with(str(self.tenant.id))
        self.assertEqual(
            ActionAuditLog.objects.filter(
                tenant=self.tenant,
                result=ActionAuditOutcome.EXECUTED,
                responded_at=action.responded_at,
            ).count(),
            1,
        )

    def test_recent_dispatching_claim_is_a_double_run_guard(self):
        action = PendingAction.objects.get(pk=self._request(request_id="double-run")["action_id"])
        dispatch = self._approve_without_callback(action)
        CronDispatch.objects.filter(pk=dispatch.pk).update(
            state=CronDispatchState.DISPATCHING,
            attempts=1,
            last_attempt_at=timezone.now(),
        )

        with patch("apps.cron.signals._enqueue_regen") as enqueue:
            state = dispatch_cron_action(dispatch.id)

        self.assertEqual(state, "busy")
        enqueue.assert_not_called()

    def test_stale_at_claim_verifies_cron_list_before_readding(self):
        action = PendingAction.objects.get(
            pk=self._request(
                request_id="stale-at",
                name="Stale at",
                schedule={"kind": "at", "at": (timezone.now() + timedelta(hours=1)).isoformat()},
            )["action_id"]
        )
        dispatch = self._approve_without_callback(action)
        CronDispatch.objects.filter(pk=dispatch.pk).update(
            state=CronDispatchState.DISPATCHING,
            attempts=1,
            last_attempt_at=timezone.now() - timedelta(minutes=11),
        )
        with patch(
            "apps.cron.gateway_client.invoke_gateway_tool",
            return_value={"details": {"jobs": [{"id": "already-live", "name": "Stale at"}]}},
        ) as gateway:
            state = dispatch_cron_action(dispatch.id)

        self.assertEqual(state, CronDispatchState.EXECUTED)
        gateway.assert_called_once_with(self.tenant, "cron.list", {"includeDisabled": True})
        dispatch.cron.refresh_from_db()
        self.assertEqual(dispatch.cron.gateway_job_id, "already-live")

    def test_failed_at_verification_retains_attempt_marker(self):
        action = PendingAction.objects.get(
            pk=self._request(
                request_id="stale-at-list-failure",
                name="Unverifiable at",
                schedule={"kind": "at", "at": (timezone.now() + timedelta(hours=1)).isoformat()},
            )["action_id"]
        )
        dispatch = self._approve_without_callback(action)
        CronDispatch.objects.filter(pk=dispatch.pk).update(
            state=CronDispatchState.DISPATCHING,
            attempts=1,
            last_attempt_at=timezone.now() - timedelta(minutes=11),
        )

        with patch(
            "apps.cron.gateway_client.invoke_gateway_tool",
            side_effect=RuntimeError("cron.list unavailable"),
        ) as gateway:
            state = dispatch_cron_action(dispatch.id)

        dispatch.refresh_from_db()
        self.assertEqual(state, CronDispatchState.QUEUED)
        self.assertEqual(dispatch.state, CronDispatchState.DISPATCHING)
        self.assertEqual(dispatch.attempts, 2)
        gateway.assert_called_once_with(self.tenant, "cron.list", {"includeDisabled": True})

        with patch("apps.cron.gateway_client.invoke_gateway_tool") as gateway:
            self.assertEqual(dispatch_cron_action(dispatch.id), "busy")
        gateway.assert_not_called()

    def test_exhausted_dispatch_is_failed_and_disabled_by_sweeper(self):
        action = PendingAction.objects.get(pk=self._request(request_id="exhausted")["action_id"])
        dispatch = self._approve_without_callback(action)
        CronDispatch.objects.filter(pk=dispatch.pk).update(
            attempts=5,
            created_at=timezone.now() - timedelta(minutes=6),
        )

        with (
            patch("apps.cron.signals._enqueue_regen") as enqueue,
            patch("apps.actions.messaging.update_gate_message"),
            patch("apps.datebook.notify.dispatch_datebook_gate_changed"),
        ):
            summary = replay_stale_cron_dispatches()

        dispatch.refresh_from_db()
        dispatch.cron.refresh_from_db()
        action.refresh_from_db()
        enqueue.assert_not_called()
        self.assertEqual(summary, "Cron dispatch replayed 1: executed=0 failed=1 queued=0 busy=0")
        self.assertEqual(dispatch.state, CronDispatchState.FAILED)
        self.assertFalse(dispatch.cron.enabled)
        self.assertEqual(action.resolution_code, "dispatch_failed:exhausted")
        self.assertTrue(
            ActionAuditLog.objects.filter(
                tenant=self.tenant,
                result=ActionAuditOutcome.FAILED,
                detail_code="dispatch_failed:exhausted",
            ).exists()
        )

    def test_recurring_dispatch_retries_enqueue_once_then_executes(self):
        action = PendingAction.objects.get(pk=self._request(request_id="regen-retry")["action_id"])
        with (
            patch("apps.cron.signals._enqueue_regen", side_effect=[False, True]) as enqueue,
            patch("apps.datebook.notify.dispatch_datebook_gate_changed"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            GateRespondView.resolve_action(
                action_id=action.id,
                response_action="approve",
                tenant=self.tenant,
            )

        dispatch = CronDispatch.objects.get(action=action)
        self.assertEqual(dispatch.state, CronDispatchState.EXECUTED)
        self.assertTrue(dispatch.cron.enabled)
        self.assertEqual(enqueue.call_count, 2)

    def test_recurring_dispatch_disables_after_two_enqueue_failures(self):
        action = PendingAction.objects.get(pk=self._request(request_id="regen-failed")["action_id"])
        with (
            patch("apps.cron.signals._enqueue_regen", return_value=False) as enqueue,
            patch("apps.datebook.notify.dispatch_datebook_gate_changed"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            GateRespondView.resolve_action(
                action_id=action.id,
                response_action="approve",
                tenant=self.tenant,
            )

        dispatch = CronDispatch.objects.get(action=action)
        action.refresh_from_db()
        self.assertEqual(dispatch.state, CronDispatchState.FAILED)
        self.assertFalse(dispatch.cron.enabled)
        self.assertEqual(action.resolution_code, "dispatch_failed")
        self.assertEqual(enqueue.call_count, 2)

    def test_expiry_sweeper_uses_typed_cron_transition(self):
        action = PendingAction.objects.create(
            tenant=self.tenant,
            action_type=ActionType.CRON_CREATE,
            action_payload={},
            display_summary="Expired cron",
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        with (
            patch("apps.actions.messaging.update_gate_message"),
            patch("apps.datebook.notify.dispatch_datebook_gate_changed") as changed,
            self.captureOnCommitCallbacks(execute=True),
        ):
            summary = expire_stale_pending_actions()

        action.refresh_from_db()
        self.assertEqual(summary, "Expired 1 actions")
        self.assertEqual(action.status, ActionStatus.EXPIRED)
        self.assertIsNotNone(action.responded_at)
        self.assertEqual(action.resolution_code, ActionStatus.EXPIRED)
        self.assertTrue(
            ActionAuditLog.objects.filter(
                tenant=self.tenant,
                result=ActionAuditOutcome.EXPIRED,
                detail_code=ActionStatus.EXPIRED,
            ).exists()
        )
        changed.assert_called_once_with(self.tenant.id)

    def test_replay_sweeper_is_registered_every_five_minutes(self):
        from apps.cron.management.commands.register_system_crons import SYSTEM_CRONS
        from apps.cron.views import TASK_MAP

        self.assertEqual(
            TASK_MAP["replay_cron_dispatches"],
            "apps.actions.tasks.replay_stale_cron_dispatches",
        )
        self.assertIn(
            (
                "replay-cron-dispatches",
                "*/5 * * * *",
                "/api/cron/trigger/replay_cron_dispatches/",
            ),
            SYSTEM_CRONS,
        )

    def test_no_channel_keeps_pending_for_72_hours(self):
        self.tenant.user.telegram_chat_id = None
        self.tenant.user.line_user_id = None
        self.tenant.user.save(update_fields=["telegram_chat_id", "line_user_id"])
        result = request_cron_action(
            self.tenant,
            cron_request_id="no-channel",
            pattern="pure_reminder",
            name="No channel",
            schedule=RECURRING,
            typed_payload={"text": "Still reviewable"},
            reason="",
            origin_stamp=OriginStamp(),
        )
        action = PendingAction.objects.get(pk=result["action_id"])
        self.assertEqual(action.status, ActionStatus.PENDING)
        self.assertEqual(action.delivery_state, "no_channel")
        self.assertGreater(action.expires_at, timezone.now() + timedelta(hours=71))


@override_settings(NBHD_INTERNAL_API_KEY="cron-gate-key", NBHD_DISABLE_BACKGROUND_THREADS=True)
class CronGateRuntimeAndConsumerTests(TestCase):
    def setUp(self):
        self.tenant = seed_internal_key(_ready_tenant(88201))
        self.tenant.postgres_cron_canonical = True
        self.tenant.save(update_fields=["postgres_cron_canonical"])
        self.url = f"/api/v1/integrations/runtime/{self.tenant.id}/crons/pure_reminder/"
        self.headers = {
            "HTTP_X_NBHD_INTERNAL_KEY": self.tenant.internal_api_key,
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }

    def _body(self, **changes):
        body = {
            "name": "Runtime reminder",
            "schedule": RECURRING,
            "text": "Take out trash",
            "reason": "Weekly routine",
            "cron_request_id": "tool-call-1",
        }
        body.update(changes)
        return body

    def test_nonlisted_tenant_preserves_byte_identical_201(self):
        with (
            override_settings(CRON_GATE_TENANT_IDS="00000000-0000-0000-0000-000000000000"),
            patch("apps.cron.gateway_client.invoke_gateway_tool") as gateway,
        ):
            response = self.client.post(
                self.url,
                data=self._body(),
                content_type="application/json",
                **self.headers,
            )
        self.assertEqual(response.status_code, 201, response.content)
        cron = CronJob.objects.get(tenant=self.tenant, name="Runtime reminder")
        expected = {
            "tenant_id": str(self.tenant.id),
            "cron": {
                "id": str(cron.id),
                "name": "Runtime reminder",
                "pattern": "pure_reminder",
                "schedule": RECURRING,
                "managed": True,
                "gateway_job_id": None,
            },
        }
        self.assertEqual(response.content, json.dumps(expected, separators=(",", ":")).encode())
        gateway.assert_not_called()

    def test_allowlist_on_http_contract_duplicate_and_conflict(self):
        with (
            override_settings(CRON_GATE_TENANT_IDS=str(self.tenant.id)),
            patch("apps.actions.messaging.send_gate_confirmation", return_value=True),
        ):
            first = self.client.post(
                self.url,
                data=self._body(origin_kind="cron"),
                content_type="application/json",
                **self.headers,
            )
            same = self.client.post(
                self.url,
                data=self._body(),
                content_type="application/json",
                **self.headers,
            )
            conflict = self.client.post(
                self.url,
                data=self._body(text="Different"),
                content_type="application/json",
                **self.headers,
            )
        expected_keys = {"state", "action_id", "expires_at", "summary"}
        self.assertEqual(first.status_code, 202, first.content)
        self.assertEqual(set(first.json()), expected_keys)
        self.assertEqual(first.json()["state"], "pending_approval")
        self.assertEqual(PendingAction.objects.get(pk=first.json()["action_id"]).origin_kind, "unknown")
        self.assertEqual(same.status_code, 200, same.content)
        self.assertEqual(same.json(), first.json())
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json(), {"error": "request_id_conflict"})
        self.assertFalse(CronJob.objects.filter(name="Runtime reminder").exists())

    def test_cron_gate_helper_unset_and_empty_fail_closed(self):
        with override_settings(CRON_GATE_TENANT_IDS=None):
            self.assertFalse(cron_gate_enabled(self.tenant))
        with override_settings(CRON_GATE_TENANT_IDS=""):
            self.assertFalse(cron_gate_enabled(self.tenant))

    def test_cron_gate_helper_single_id_only_opens_that_tenant(self):
        other = create_tenant(display_name="Other Cron Gate", telegram_chat_id=88202)
        with override_settings(CRON_GATE_TENANT_IDS=str(self.tenant.id)):
            self.assertTrue(cron_gate_enabled(self.tenant))
            self.assertFalse(cron_gate_enabled(other))

    def test_cron_gate_helper_wildcard_opens_every_tenant(self):
        other = create_tenant(display_name="Other Cron Gate", telegram_chat_id=88203)
        with override_settings(CRON_GATE_TENANT_IDS="*"):
            self.assertTrue(cron_gate_enabled(self.tenant))
            self.assertTrue(cron_gate_enabled(other))

    def test_cron_gate_helper_wildcard_with_id_opens_every_tenant(self):
        other = create_tenant(display_name="Other Cron Gate", telegram_chat_id=88204)
        with override_settings(CRON_GATE_TENANT_IDS=f"*,{self.tenant.id}"):
            self.assertTrue(cron_gate_enabled(self.tenant))
            self.assertTrue(cron_gate_enabled(other))

    def test_cron_gate_helper_tolerates_whitespace_empty_entries_and_uuid_case(self):
        upper_tenant_id = str(self.tenant.id).upper()
        with override_settings(CRON_GATE_TENANT_IDS=f" , {upper_tenant_id} , , "):
            self.assertTrue(cron_gate_enabled(self.tenant))

    def test_poll_expiry_uses_typed_cron_transition(self):
        action = PendingAction.objects.create(
            tenant=self.tenant,
            action_type=ActionType.CRON_CREATE,
            action_payload={},
            display_summary="Expired polled cron",
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        poll_url = f"/api/v1/internal/runtime/{self.tenant.id}/gate/{action.id}/poll/"

        with (
            patch("apps.actions.messaging.update_gate_message"),
            patch("apps.datebook.notify.dispatch_datebook_gate_changed") as changed,
            self.captureOnCommitCallbacks(execute=True),
        ):
            response = self.client.get(
                poll_url,
                HTTP_X_INTERNAL_KEY="cron-gate-key",
                HTTP_X_TENANT_ID=str(self.tenant.id),
            )

        action.refresh_from_db()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data, {"action_id": action.id, "status": ActionStatus.EXPIRED})
        self.assertIsNotNone(action.responded_at)
        self.assertEqual(action.resolution_code, ActionStatus.EXPIRED)
        self.assertTrue(
            ActionAuditLog.objects.filter(
                tenant=self.tenant,
                result=ActionAuditOutcome.EXPIRED,
                detail_code=ActionStatus.EXPIRED,
                responded_at=action.responded_at,
            ).exists()
        )
        changed.assert_called_once_with(self.tenant.id)

    def test_default_list_is_old_shape_and_include_adds_datebook_first_with_origin(self):
        datebook = PendingAction.objects.create(
            tenant=self.tenant,
            action_type=ActionType.CALENDAR_CREATE,
            action_payload=_command_gate_payload("list-datebook"),
            display_summary="Create calendar event",
            datebook_request_id="list-datebook",
            origin_kind="unknown",
        )
        with patch("apps.actions.messaging.send_gate_confirmation", return_value=True):
            cron_id = request_cron_action(
                self.tenant,
                cron_request_id="list-cron",
                pattern="pure_reminder",
                name="List cron",
                schedule=RECURRING,
                typed_payload={"text": "List me"},
                reason="",
                origin_stamp=OriginStamp(kind="cron", cron_name="Morning Briefing", run_id="r1"),
            )["action_id"]
        consumer = APIClient()
        consumer.force_authenticate(user=self.tenant.user)

        old = consumer.get("/api/v1/datebook/gate/pending/")
        included = consumer.get("/api/v1/datebook/gate/pending/?include=cron_create")

        self.assertEqual([row["action_id"] for row in old.data["actions"]], [datebook.id])
        self.assertNotIn("origin", old.data["actions"][0])
        self.assertEqual(
            [row["action_id"] for row in included.data["actions"]],
            [datebook.id, cron_id],
        )
        self.assertEqual(included.data["actions"][0]["origin"], {"kind": "unknown", "cron_name": ""})
        self.assertEqual(
            included.data["actions"][1]["origin"],
            {"kind": "cron", "cron_name": "Morning Briefing"},
        )

    def test_include_caps_cron_rows_at_ten_without_crowding_datebook(self):
        datebook = PendingAction.objects.create(
            tenant=self.tenant,
            action_type=ActionType.REMINDER_CREATE,
            action_payload=_command_gate_payload("cap-datebook"),
            display_summary="Create reminder",
            datebook_request_id="cap-datebook",
        )
        for index in range(12):
            PendingAction.objects.create(
                tenant=self.tenant,
                action_type=ActionType.CRON_CREATE,
                action_payload={"index": index},
                display_summary=f"Cron {index}",
                cron_request_id=f"cap-cron-{index}",
                expires_at=timezone.now() + timedelta(hours=72),
            )
        consumer = APIClient()
        consumer.force_authenticate(user=self.tenant.user)

        response = consumer.get("/api/v1/datebook/gate/pending/?include=cron_create")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["actions"][0]["action_id"], datebook.id)
        self.assertEqual(
            sum(row["action_type"] == ActionType.CRON_CREATE for row in response.data["actions"]),
            10,
        )

    def test_consumer_respond_supports_get_post_and_rejects_cron_destinations(self):
        with patch("apps.actions.messaging.send_gate_confirmation", return_value=True):
            denied_id = request_cron_action(
                self.tenant,
                cron_request_id="respond-get",
                pattern="pure_reminder",
                name="Deny me",
                schedule=RECURRING,
                typed_payload={"text": "x"},
                reason="",
                origin_stamp=OriginStamp(),
            )["action_id"]
            approve_id = request_cron_action(
                self.tenant,
                cron_request_id="respond-post",
                pattern="pure_reminder",
                name="Approve me",
                schedule=RECURRING,
                typed_payload={"text": "y"},
                reason="",
                origin_stamp=OriginStamp(),
            )["action_id"]
        consumer = APIClient()
        consumer.force_authenticate(user=self.tenant.user)
        denied = consumer.get(f"/api/v1/datebook/gate/{denied_id}/respond/?response=deny")
        rejected = consumer.post(
            f"/api/v1/datebook/gate/{approve_id}/respond/",
            {"response": "approve", "set_default": True},
            format="json",
        )
        with patch("apps.cron.publish.publish_task"), self.captureOnCommitCallbacks(execute=True):
            approved = consumer.post(
                f"/api/v1/datebook/gate/{approve_id}/respond/",
                {"response": "approve"},
                format="json",
            )

        self.assertEqual(denied.status_code, 200, denied.data)
        self.assertEqual(set(denied.data), {"status", "execution", "code"})
        self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertEqual(approved.status_code, 200, approved.data)
        self.assertEqual(set(approved.data), {"status", "execution", "code"})
        self.assertIn(approved.data["execution"], {"queued", "executed"})
