"""Bounded retry coverage for silently dropped app turns and channel recon."""

from __future__ import annotations

import json
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from apps.router.models import AppChatMessage, ChatThread, PendingMessage, RuntimeWriteActivity
from apps.router.pending_queue import (
    drain_pending_messages_for_tenant_task,
    dropped_retry_dedup_id,
    dropped_retry_health_dedup_id,
    retry_dropped_app_turn_task,
)
from apps.router.services import clear_cache, clear_rate_limits
from apps.tenants.models import Tenant, User
from apps.tenants.services import create_tenant


def _chat_response(text: str):
    response = MagicMock()
    response.status_code = 200
    response.is_success = True
    response.json.return_value = {
        "choices": [{"message": {"content": text}}],
        "usage": {},
        "model": "test",
    }
    response.raise_for_status = MagicMock()
    return response


@override_settings(NBHD_INTERNAL_API_KEY="test-key", NBHD_DISABLE_BACKGROUND_THREADS=True)
class DroppedAppTurnRetryTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username=f"drop_retry_{secrets.token_hex(4)}",
            email=f"{secrets.token_hex(4)}@example.com",
        )
        self.tenant = Tenant.objects.create(
            user=self.user,
            status=Tenant.Status.ACTIVE,
            container_fqdn="oc-retry.example.com",
        )
        self.thread = ChatThread.objects.create(
            tenant=self.tenant,
            user=self.user,
            is_main=True,
            title="Main",
        )

    def _pair(
        self,
        client_msg_id: str,
        *,
        phase: str = "thinking",
        partial_text: str = "",
        reply_text: str = "",
        attempts: int = 3,
        partial_seq: int = 0,
    ) -> tuple[AppChatMessage, PendingMessage]:
        turn = AppChatMessage.objects.create(
            tenant=self.tenant,
            user=self.user,
            thread=self.thread,
            client_msg_id=client_msg_id,
            user_text=f"question {client_msg_id}",
            reply_text=reply_text,
            status=AppChatMessage.Status.PENDING,
            phase=phase,
            partial_text=partial_text,
            partial_seq=partial_seq,
        )
        queue_row = PendingMessage.objects.create(
            tenant=self.tenant,
            channel=PendingMessage.Channel.IOS,
            channel_user_id=str(self.thread.id),
            payload={
                "message_text": f"question {client_msg_id}",
                "user_param": f"thread:{self.thread.id}",
                "user_timezone": "UTC",
                "client_msg_id": client_msg_id,
                "thread_id": str(self.thread.id),
            },
            user_text=f"question {client_msg_id}",
            delivery_attempts=attempts,
        )
        return turn, queue_row

    def _drop(self):
        with self.captureOnCommitCallbacks(execute=True):
            return drain_pending_messages_for_tenant_task(
                str(self.tenant.id),
                PendingMessage.Channel.IOS,
                str(self.thread.id),
            )

    @patch("apps.router.pending_queue._schedule_ios_digest_refresh")
    @patch("apps.router.push_views.notify_app_reply_ready")
    @patch("apps.router.push_views.notify_app_reply_error")
    @patch("apps.router.pending_queue.httpx.post")
    @patch("apps.cron.publish.publish_task")
    def test_silent_drop_retries_once_then_late_reply_uses_existing_notify(
        self,
        publish,
        post,
        notify_error,
        notify_ready,
        _digest,
    ):
        turn, original_queue = self._pair("late-success")

        result = self._drop()

        self.assertEqual(result["dropped"], 1)
        turn.refresh_from_db()
        original_queue.refresh_from_db()
        self.assertEqual(turn.status, AppChatMessage.Status.ERROR)
        self.assertEqual(turn.error, "dropped")
        self.assertIsNotNone(turn.retried_at)
        self.assertEqual(original_queue.delivery_status, PendingMessage.Status.FAILED)
        notify_error.assert_not_called()

        retry_calls = [call for call in publish.call_args_list if call.args[0] == "retry_dropped_app_turn"]
        self.assertEqual(len(retry_calls), 1)
        retry_call = retry_calls[0]
        self.assertEqual(retry_call.kwargs["delay_seconds"], 60)
        self.assertEqual(retry_call.kwargs["retries"], 0)
        self.assertNotIn(":", retry_call.kwargs["idempotency_key"])

        publish.reset_mock()
        with (
            patch("apps.router.pending_queue._is_tenant_container_live", return_value=True),
            self.captureOnCommitCallbacks(execute=True),
        ):
            retried = retry_dropped_app_turn_task(str(turn.id))

        self.assertEqual(retried, {"retried": 1})
        turn.refresh_from_db()
        self.assertEqual(turn.status, AppChatMessage.Status.PENDING)
        self.assertEqual(turn.error, "")
        self.assertEqual(turn.partial_seq, 0)
        pending_retry = PendingMessage.objects.get(
            tenant=self.tenant,
            channel=PendingMessage.Channel.IOS,
            delivery_status=PendingMessage.Status.PENDING,
        )
        self.assertEqual(pending_retry.payload["client_msg_id"], turn.client_msg_id)
        drain_calls = [call for call in publish.call_args_list if call.args[0] == "drain_pending_messages_for_tenant"]
        self.assertEqual(len(drain_calls), 1)

        post.return_value = _chat_response("late answer")
        with self.assertLogs("apps.router.pending_queue", level="INFO") as logs:
            delivered = drain_pending_messages_for_tenant_task(
                str(self.tenant.id),
                PendingMessage.Channel.IOS,
                str(self.thread.id),
            )

        self.assertEqual(delivered["delivered"], 1)
        turn.refresh_from_db()
        self.assertEqual(turn.status, AppChatMessage.Status.READY)
        self.assertEqual(turn.reply_text, "late answer")
        notify_ready.assert_called_once_with(self.tenant, ["late-success"], "late answer")
        self.assertTrue(any(record.getMessage().startswith("retry_succeeded ") for record in logs.records))

    @patch("apps.router.push_views.notify_app_reply_error")
    @patch("apps.cron.publish.publish_task")
    def test_retried_turn_dies_permanently_without_second_retry(self, publish, notify_error):
        turn, _ = self._pair("dies-again")
        self._drop()

        publish.reset_mock()
        with (
            patch("apps.router.pending_queue._is_tenant_container_live", return_value=True),
            self.captureOnCommitCallbacks(execute=True),
        ):
            self.assertEqual(retry_dropped_app_turn_task(str(turn.id)), {"retried": 1})

        retry_queue = PendingMessage.objects.get(delivery_status=PendingMessage.Status.PENDING)
        retry_queue.delivery_attempts = 3
        retry_queue.save(update_fields=["delivery_attempts"])
        publish.reset_mock()
        with (
            self.assertLogs("apps.router.pending_queue", level="WARNING") as logs,
            self.captureOnCommitCallbacks(execute=True),
        ):
            result = drain_pending_messages_for_tenant_task(
                str(self.tenant.id),
                PendingMessage.Channel.IOS,
                str(self.thread.id),
            )

        self.assertEqual(result["dropped"], 1)
        turn.refresh_from_db()
        self.assertEqual(turn.status, AppChatMessage.Status.ERROR)
        self.assertEqual(turn.error, "dropped")
        self.assertIsNotNone(turn.retried_at)
        self.assertFalse(any(call.args[0] == "retry_dropped_app_turn" for call in publish.call_args_list))
        self.assertTrue(any(record.getMessage().startswith("retry_exhausted ") for record in logs.records))
        notify_error.assert_called_once_with(self.tenant, ["dies-again"])

    @patch("apps.router.push_views.notify_app_reply_error")
    @patch("apps.cron.publish.publish_task")
    def test_partial_or_existing_reply_never_retries(self, publish, notify_error):
        for client_msg_id, partial_text, reply_text in (
            ("has-partial", "unfinished", ""),
            ("has-reply", "", "already visible"),
        ):
            with self.subTest(client_msg_id=client_msg_id):
                turn, _ = self._pair(client_msg_id, partial_text=partial_text, reply_text=reply_text)
                self._drop()
                turn.refresh_from_db()
                self.assertIsNone(turn.retried_at)
                self.assertFalse(any(call.args[0] == "retry_dropped_app_turn" for call in publish.call_args_list))
                publish.reset_mock()
        self.assertEqual(notify_error.call_count, 2)

    @patch("apps.router.push_views.notify_app_reply_error")
    @patch("apps.cron.publish.publish_task")
    def test_only_exact_thinking_phase_is_narrative_evidence(self, publish, notify_error):
        for phase in ("", "waking", "tool", "composing"):
            with self.subTest(phase=phase):
                turn, _ = self._pair(f"phase-{phase or 'empty'}", phase=phase)
                self._drop()
                turn.refresh_from_db()
                self.assertIsNone(turn.retried_at)
                self.assertFalse(any(call.args[0] == "retry_dropped_app_turn" for call in publish.call_args_list))
                publish.reset_mock()
        self.assertEqual(notify_error.call_count, 4)

    @patch("apps.router.push_views.notify_app_reply_error")
    @patch("apps.cron.publish.publish_task")
    def test_authoritative_runtime_write_during_turn_blocks_retry(self, publish, notify_error):
        turn, _ = self._pair("runtime-write")
        RuntimeWriteActivity.objects.create(
            tenant=self.tenant,
            last_runtime_write_at=turn.created_at + timedelta(microseconds=1),
        )

        self._drop()

        turn.refresh_from_db()
        self.assertIsNone(turn.retried_at)
        self.assertFalse(any(call.args[0] == "retry_dropped_app_turn" for call in publish.call_args_list))
        notify_error.assert_called_once_with(self.tenant, ["runtime-write"])

    @patch("apps.cron.publish.publish_task")
    def test_runtime_write_before_turn_does_not_block_retry(self, publish):
        turn, _ = self._pair("old-runtime-write")
        RuntimeWriteActivity.objects.create(
            tenant=self.tenant,
            last_runtime_write_at=turn.created_at - timedelta(seconds=1),
        )

        self._drop()

        turn.refresh_from_db()
        self.assertIsNotNone(turn.retried_at)
        self.assertTrue(any(call.args[0] == "retry_dropped_app_turn" for call in publish.call_args_list))

    @patch("apps.router.push_views.notify_app_reply_error")
    @patch("apps.cron.publish.publish_task")
    def test_delayed_task_rechecks_authoritative_write_window(self, publish, notify_error):
        turn, _ = self._pair("late-write-record")
        self._drop()
        turn.refresh_from_db()
        RuntimeWriteActivity.objects.create(
            tenant=self.tenant,
            last_runtime_write_at=turn.created_at + timedelta(microseconds=1),
        )

        with patch("apps.router.pending_queue._is_tenant_container_live") as health:
            result = retry_dropped_app_turn_task(str(turn.id))

        self.assertEqual(result, {"retried": 0, "exhausted": "ineligible"})
        health.assert_not_called()
        notify_error.assert_called_once_with(self.tenant, ["late-write-record"])
        self.assertFalse(
            PendingMessage.objects.filter(
                tenant=self.tenant,
                delivery_status=PendingMessage.Status.PENDING,
            ).exists()
        )
        self.assertTrue(any(call.args[0] == "retry_dropped_app_turn" for call in publish.call_args_list))

    @patch("apps.router.push_views.notify_app_reply_error")
    @patch("apps.cron.publish.publish_task")
    def test_older_turn_in_thread_never_retries(self, publish, notify_error):
        older, _ = self._pair("older")
        AppChatMessage.objects.create(
            tenant=self.tenant,
            user=self.user,
            thread=self.thread,
            client_msg_id="newer",
            user_text="newer question",
            status=AppChatMessage.Status.PENDING,
        )

        # Limit the drop to the older row's source queue; the newer app turn has
        # no queue row and is only the thread-recency guard.
        self._drop()

        older.refresh_from_db()
        self.assertIsNone(older.retried_at)
        self.assertFalse(any(call.args[0] == "retry_dropped_app_turn" for call in publish.call_args_list))
        notify_error.assert_called_once_with(self.tenant, ["older"])

    @patch("apps.router.push_views.notify_app_reply_error")
    @patch("apps.cron.publish.publish_task")
    def test_newer_turn_arriving_during_delay_spends_retry_without_requeue(self, publish, notify_error):
        turn, _ = self._pair("delayed-old")
        self._drop()
        AppChatMessage.objects.create(
            tenant=self.tenant,
            user=self.user,
            thread=self.thread,
            client_msg_id="arrived-during-delay",
            user_text="new question",
            status=AppChatMessage.Status.PENDING,
        )

        with patch("apps.router.pending_queue._is_tenant_container_live", return_value=True):
            result = retry_dropped_app_turn_task(str(turn.id))

        self.assertEqual(result, {"retried": 0, "exhausted": "newer_turn"})
        turn.refresh_from_db()
        self.assertEqual(turn.status, AppChatMessage.Status.ERROR)
        self.assertEqual(
            PendingMessage.objects.filter(delivery_status=PendingMessage.Status.PENDING).count(),
            0,
        )
        notify_error.assert_called_once_with(self.tenant, ["delayed-old"])

    @patch("apps.router.push_views.notify_app_reply_error")
    @patch("apps.cron.publish.publish_task")
    def test_unhealthy_container_redefers_once_then_exhausts(self, publish, notify_error):
        turn, _ = self._pair("unhealthy")
        self._drop()
        publish.reset_mock()

        with (
            patch("apps.router.pending_queue._is_tenant_container_live", return_value=False),
            patch("apps.router.inbound_dedup.claim_inbound_event") as claim,
        ):
            first = retry_dropped_app_turn_task(str(turn.id))
            second = retry_dropped_app_turn_task(str(turn.id))

        self.assertEqual(first, {"retried": 0, "deferred": "container_unhealthy"})
        self.assertEqual(second, {"retried": 0, "exhausted": "container_unhealthy"})
        claim.assert_not_called()
        turn.refresh_from_db()
        self.assertIsNotNone(turn.retry_health_deferred_at)
        self.assertEqual(turn.status, AppChatMessage.Status.ERROR)
        self.assertEqual(turn.error, "dropped")
        self.assertEqual(
            PendingMessage.objects.filter(delivery_status=PendingMessage.Status.PENDING).count(),
            0,
        )
        notify_error.assert_called_once_with(self.tenant, ["unhealthy"])
        retry_calls = [call for call in publish.call_args_list if call.args[0] == "retry_dropped_app_turn"]
        self.assertEqual(len(retry_calls), 1)
        self.assertEqual(retry_calls[0].kwargs["delay_seconds"], 120)
        self.assertEqual(retry_calls[0].kwargs["retries"], 0)
        self.assertEqual(retry_calls[0].kwargs["idempotency_key"], dropped_retry_health_dedup_id(turn.id))
        self.assertNotEqual(retry_calls[0].kwargs["idempotency_key"], dropped_retry_dedup_id(turn.id))
        self.assertNotIn(":", retry_calls[0].kwargs["idempotency_key"])

    @patch("apps.cron.publish.publish_task")
    @patch("apps.router.push_views.notify_app_reply_error")
    def test_task_exception_exhausts_and_notifies(self, notify_error, _publish):
        turn, _ = self._pair("raising-task")
        self._drop()

        with (
            patch("apps.router.pending_queue._is_tenant_container_live", return_value=True),
            patch("apps.router.inbound_dedup.claim_inbound_event", return_value=True),
            patch("apps.router.pending_queue.enqueue_message_for_tenant", side_effect=RuntimeError("boom")),
        ):
            result = retry_dropped_app_turn_task(str(turn.id))

        self.assertEqual(result, {"retried": 0, "exhausted": "task_error"})
        notify_error.assert_called_once_with(self.tenant, ["raising-task"])

    @patch("apps.cron.views.verify_qstash_signature", return_value=True)
    @patch("apps.router.pending_queue._is_tenant_container_live", return_value=True)
    @patch("apps.cron.publish.publish_task")
    def test_cron_callback_round_trip_uses_registered_task_map_entry(self, _publish, _health, _signature):
        turn, _ = self._pair("callback-round-trip")
        self._drop()

        response = self.client.post(
            "/api/cron/trigger/retry_dropped_app_turn/",
            data=json.dumps({"args": [str(turn.id)]}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["task_name"], "retry_dropped_app_turn")
        turn.refresh_from_db()
        self.assertEqual(turn.status, AppChatMessage.Status.PENDING)

    @patch("apps.cron.publish.publish_task")
    def test_requeue_preserves_partial_seq_guard(self, _publish):
        turn, _ = self._pair("preserve-seq", partial_seq=7)
        self._drop()

        with patch("apps.router.pending_queue._is_tenant_container_live", return_value=True):
            result = retry_dropped_app_turn_task(str(turn.id))

        self.assertEqual(result, {"retried": 1})
        turn.refresh_from_db()
        self.assertEqual(turn.partial_seq, 7)

    @patch("apps.cron.publish.publish_task")
    def test_claim_inbound_event_blocks_double_submission(self, publish):
        turn, _ = self._pair("dedup")
        self._drop()

        with patch("apps.router.pending_queue._is_tenant_container_live", return_value=True):
            first = retry_dropped_app_turn_task(str(turn.id))
            second = retry_dropped_app_turn_task(str(turn.id))

        self.assertEqual(first, {"retried": 1})
        self.assertEqual(second, {"retried": 0, "duplicate": True})
        self.assertEqual(
            PendingMessage.objects.filter(delivery_status=PendingMessage.Status.PENDING).count(),
            1,
        )

    def test_qstash_dedup_id_has_no_colon_or_whitespace(self):
        value = dropped_retry_dedup_id("56e7f92c-45a5-4478-a773-bf2beec93a9d")
        self.assertNotIn(":", value)
        self.assertFalse(any(char.isspace() for char in value))


@override_settings(NBHD_INTERNAL_API_KEY="test-key", NBHD_DISABLE_BACKGROUND_THREADS=True)
class DroppedAppTurnRetryConcurrencyTest(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username=f"drop_retry_race_{secrets.token_hex(4)}",
            email=f"{secrets.token_hex(4)}@example.com",
        )
        self.tenant = Tenant.objects.create(
            user=self.user,
            status=Tenant.Status.ACTIVE,
            container_fqdn="oc-retry-race.example.com",
        )
        self.thread = ChatThread.objects.create(tenant=self.tenant, user=self.user, is_main=True, title="Main")
        now = timezone.now()
        self.turn = AppChatMessage.objects.create(
            tenant=self.tenant,
            user=self.user,
            thread=self.thread,
            client_msg_id="fail-open-race",
            user_text="question",
            status=AppChatMessage.Status.ERROR,
            error="dropped",
            phase="thinking",
            replied_at=now,
            retried_at=now,
        )
        PendingMessage.objects.create(
            tenant=self.tenant,
            channel=PendingMessage.Channel.IOS,
            channel_user_id=str(self.thread.id),
            payload={
                "message_text": "question",
                "client_msg_id": self.turn.client_msg_id,
                "thread_id": str(self.thread.id),
            },
            user_text="question",
            delivery_status=PendingMessage.Status.FAILED,
            delivered_at=now,
        )

    @patch("apps.router.push_views.notify_app_reply_error")
    @patch("apps.cron.publish.publish_task")
    @patch("apps.router.pending_queue._is_tenant_container_live", return_value=True)
    def test_concurrent_fail_open_claims_still_requeue_once(self, _health, _publish, notify_error):
        claim_barrier = threading.Barrier(2)

        def fail_open_claim(_event_key):
            claim_barrier.wait(timeout=5)
            return True

        def run_task():
            close_old_connections()
            try:
                return retry_dropped_app_turn_task(str(self.turn.id))
            finally:
                close_old_connections()

        with (
            patch("apps.router.inbound_dedup.claim_inbound_event", side_effect=fail_open_claim),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            results = list(pool.map(lambda _index: run_task(), range(2)))

        self.assertCountEqual(results, [{"retried": 1}, {"retried": 0, "duplicate": True}])
        self.assertEqual(
            PendingMessage.objects.filter(
                tenant=self.tenant,
                delivery_status=PendingMessage.Status.PENDING,
            ).count(),
            1,
        )
        notify_error.assert_not_called()


@override_settings(
    NBHD_INTERNAL_API_KEY="test-key",
    NBHD_DISABLE_BACKGROUND_THREADS=True,
    LINE_CHANNEL_ACCESS_TOKEN="line-token",
    TELEGRAM_BOT_TOKEN="telegram-token",
)
class VisibleDropChannelCoverageTest(TestCase):
    def _tenant(self, channel: str) -> tuple[Tenant, str]:
        user = User.objects.create_user(
            username=f"visible_drop_{channel}_{secrets.token_hex(4)}",
            email=f"{secrets.token_hex(4)}@example.com",
            line_user_id="U-visible" if channel == PendingMessage.Channel.LINE else None,
            telegram_chat_id=818181 if channel == PendingMessage.Channel.TELEGRAM else None,
        )
        tenant = Tenant.objects.create(
            user=user,
            status=Tenant.Status.ACTIVE,
            container_fqdn="oc-visible.example.com",
        )
        return tenant, "U-visible" if channel == PendingMessage.Channel.LINE else "818181"

    @patch("apps.cron.publish.publish_task")
    @patch("apps.router.pending_queue._send_apology_for_dropped_pending_message")
    def test_line_and_telegram_drops_are_visible_apologies_not_silent_retries(self, apology, publish):
        for channel in (PendingMessage.Channel.LINE, PendingMessage.Channel.TELEGRAM):
            with self.subTest(channel=channel):
                tenant, channel_user_id = self._tenant(channel)
                PendingMessage.objects.create(
                    tenant=tenant,
                    channel=channel,
                    channel_user_id=channel_user_id,
                    payload={
                        "message_text": "redacted inbound",
                        "user_param": channel_user_id,
                        "user_timezone": "UTC",
                    },
                    user_text="redacted inbound",
                    delivery_attempts=3,
                )

                result = drain_pending_messages_for_tenant_task(
                    str(tenant.id),
                    channel,
                    channel_user_id,
                )

                self.assertEqual(result["dropped"], 1)
                self.assertEqual(apology.call_count, 1)
                self.assertFalse(any(call.args[0] == "retry_dropped_app_turn" for call in publish.call_args_list))
                apology.reset_mock()
                publish.reset_mock()


@override_settings(TELEGRAM_WEBHOOK_SECRET="test-secret", ROUTER_RATE_LIMIT_PER_MINUTE=10)
class TelegramWebhookDropCoverageTest(TestCase):
    def setUp(self):
        clear_cache()
        clear_rate_limits()

    def tearDown(self):
        clear_cache()
        clear_rate_limits()

    @patch("apps.cron.publish.publish_task")
    @patch("apps.router.views.forward_to_openclaw", new_callable=AsyncMock)
    def test_direct_webhook_timeout_has_no_dropped_row_class(self, forward, publish):
        tenant = create_tenant(display_name="Webhook Direct", telegram_chat_id=717171)
        tenant.status = Tenant.Status.ACTIVE
        tenant.container_fqdn = "oc-webhook.example.com"
        tenant.save(update_fields=["status", "container_fqdn", "updated_at"])
        forward.return_value = None

        response = self.client.post(
            "/api/v1/telegram/webhook/",
            data=json.dumps(
                {
                    "update_id": 919191,
                    "message": {"text": "hello", "chat": {"id": 717171}},
                }
            ),
            content_type="application/json",
            HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="test-secret",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"ok")
        self.assertFalse(PendingMessage.objects.filter(tenant=tenant).exists())
        self.assertFalse(AppChatMessage.objects.filter(tenant=tenant).exists())
        self.assertFalse(any(call.args[0] == "retry_dropped_app_turn" for call in publish.call_args_list))
