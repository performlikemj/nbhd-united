"""First-session welcome copy, delivery, idempotency, and entry-path tests."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from datetime import timedelta
from unittest.mock import patch

from django.db import close_old_connections, connection
from django.db.utils import OperationalError
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from apps.orchestrator.first_session_welcome import (
    FIRST_SESSION_DISPLAY_NAME_MAX_LENGTH,
    FIRST_SESSION_WELCOME_CLIENT_MSG_ID,
    FIRST_SESSION_WELCOME_KEY,
    FIRST_SESSION_WELCOME_QUICK_REPLIES,
    FIRST_SESSION_WELCOME_TEMPLATE,
    compose_first_session_welcome,
    seed_first_session_welcome,
)
from apps.orchestrator.services import provision_tenant, repair_stale_tenant_provisioning
from apps.router.models import AppChatMessage, ChatThread, ProactiveOutbound
from apps.tenants.models import Tenant, User


class FirstSessionWelcomeCopyTest(SimpleTestCase):
    def test_named_template_is_exact(self):
        self.assertEqual(
            compose_first_session_welcome("Mika"),
            """Hey Mika — welcome to the neighborhood. I'm your assistant, and this is where we talk.

Your private space is already set up: journal, goals, workouts, money, and more — all yours, all private. The fastest way for me to be genuinely useful is to know you a little. When you have a minute, tell me: what should I call you, and what's the one thing you're most hoping I can help with?

Reply whenever you're ready. I don't rush.""",
        )
        self.assertIn("{display_name}", FIRST_SESSION_WELCOME_TEMPLATE)

    def test_blank_name_drops_prefix(self):
        self.assertEqual(
            compose_first_session_welcome(" \n\t"),
            """Welcome to the neighborhood. I'm your assistant, and this is where we talk.

Your private space is already set up: journal, goals, workouts, money, and more — all yours, all private. The fastest way for me to be genuinely useful is to know you a little. When you have a minute, tell me: what should I call you, and what's the one thing you're most hoping I can help with?

Reply whenever you're ready. I don't rush.""",
        )

    def test_hostile_name_is_single_line_capped_and_not_a_placeholder(self):
        message = compose_first_session_welcome("  [PERSON_1]\nMallory  " + "x" * 200)
        interpolated = message.removeprefix("Hey ").split(" — welcome", 1)[0]

        self.assertNotIn("[PERSON_", message)
        self.assertNotIn("\n", interpolated)
        self.assertLessEqual(len(interpolated), FIRST_SESSION_DISPLAY_NAME_MAX_LENGTH)


@override_settings(NBHD_DISABLE_BACKGROUND_THREADS=True)
class FirstSessionWelcomePersistenceTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="first-session-persistence",
            display_name="Mika",
        )
        self.tenant = Tenant.objects.create(user=self.user, status=Tenant.Status.ACTIVE)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_fresh_tenant_gets_ready_app_assistant_row_through_sync_endpoint(self):
        with (
            patch("apps.router.cron_delivery.resolve_user_channel", side_effect=AssertionError("must not resolve")),
            patch("apps.common.apns.send_push") as send_push,
        ):
            row = seed_first_session_welcome(self.tenant)

        self.assertIsNotNone(row)
        self.assertEqual(row.channel, "app")
        self.assertEqual(row.channel_user_id, str(self.user.id))
        self.assertEqual(row.job_name, "_first_session_welcome")
        self.assertEqual(row.quick_replies, ["Tell you about me"])
        send_push.assert_not_called()

        message = AppChatMessage.objects.get(tenant=self.tenant)
        self.assertEqual(message.client_msg_id, FIRST_SESSION_WELCOME_CLIENT_MSG_ID)
        self.assertEqual(message.user, self.user)
        self.assertEqual(message.user_text, "")
        self.assertEqual(message.reply_text, compose_first_session_welcome("Mika"))
        self.assertEqual(message.status, AppChatMessage.Status.READY)
        self.assertEqual(message.source, AppChatMessage.Source.TENANT)
        self.assertEqual(message.error, "")
        self.assertIsNotNone(message.replied_at)
        self.assertEqual(message.quick_replies, FIRST_SESSION_WELCOME_QUICK_REPLIES)
        self.assertTrue(message.thread.is_main)
        self.assertEqual(message.thread.tenant, self.tenant)
        self.assertEqual(message.thread.user, self.user)
        self.assertEqual(message.thread.title, "Main")

        response = self.client.get("/api/v1/chat/messages/")
        self.assertEqual(response.status_code, 200, response.content)
        greeting = compose_first_session_welcome("Mika")
        self.assertEqual(sum(item["text"] == greeting for item in response.data["messages"]), 1)
        app_rows = [item for item in response.data["messages"] if item["source"] == "app"]
        self.assertEqual(
            app_rows,
            [
                {
                    "id": f"app:{message.id}:1",
                    "client_msg_id": FIRST_SESSION_WELCOME_CLIENT_MSG_ID,
                    "role": "assistant",
                    "text": greeting,
                    "created_at": message.replied_at.isoformat(),
                    "source": "app",
                    "thread_id": str(message.thread_id),
                    "has_image": False,
                    "has_document": False,
                    "quick_replies": FIRST_SESSION_WELCOME_QUICK_REPLIES,
                }
            ],
        )

    def test_reseed_is_idempotent_for_message_thread_and_audit(self):
        first = seed_first_session_welcome(self.tenant)

        self.assertIsNone(seed_first_session_welcome(self.tenant))
        self.assertIsNotNone(first)
        self.assertEqual(AppChatMessage.objects.filter(tenant=self.tenant).count(), 1)
        self.assertEqual(ChatThread.objects.filter(tenant=self.tenant, is_main=True).count(), 1)
        self.assertEqual(ProactiveOutbound.objects.filter(tenant=self.tenant).count(), 1)

    def test_delivery_sets_transaction_local_tenant_before_app_insert(self):
        with CaptureQueriesContext(connection) as queries:
            seed_first_session_welcome(self.tenant)

        sql = [query["sql"] for query in queries]
        guc_index = next(index for index, query in enumerate(sql) if "set_config('app.tenant_id'" in query)
        insert_index = next(index for index, query in enumerate(sql) if 'INSERT INTO "app_chat_messages"' in query)
        self.assertLess(guc_index, insert_index)
        self.assertIn(str(self.tenant.id), sql[guc_index])
        self.assertIn("true", sql[guc_index].lower())

    def test_retry_after_partial_failure_accepts_loss_without_respam(self):
        with patch(
            "apps.router.proactive_context.record_proactive_outbound",
            side_effect=RuntimeError("write failed after stamp"),
        ) as record:
            with self.assertRaisesRegex(RuntimeError, "write failed after stamp"):
                seed_first_session_welcome(self.tenant)
            self.tenant.refresh_from_db()
            self.assertIn(FIRST_SESSION_WELCOME_KEY, self.tenant.welcomes_sent)

            self.assertIsNone(seed_first_session_welcome(self.tenant))

        record.assert_called_once()
        self.assertFalse(ProactiveOutbound.objects.filter(tenant=self.tenant).exists())
        self.assertEqual(AppChatMessage.objects.filter(tenant=self.tenant).count(), 1)

    def test_hostile_name_cannot_corrupt_since_feed_rehydration(self):
        self.user.display_name = "[PERSON_1]\nMallory " + "x" * 200
        self.user.save(update_fields=["display_name"])
        self.tenant.pii_entity_map = {"[PERSON_1]": "CORRUPTED RENDER"}
        self.tenant.save(update_fields=["pii_entity_map"])

        seed_first_session_welcome(self.tenant)
        response = self.client.get("/api/v1/chat/messages/")
        messages = response.data["messages"]
        app_message = next(message for message in messages if message["source"] == "app")

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(app_message["role"], "assistant")
        self.assertEqual(app_message["quick_replies"], ["Tell you about me"])
        self.assertNotIn("[PERSON_", app_message["text"])
        self.assertNotIn("CORRUPTED RENDER", app_message["text"])


@contextmanager
def _mock_provision_dependencies():
    with ExitStack() as stack:
        stack.enter_context(patch("apps.orchestrator.azure_client._is_mock", return_value=True))
        stack.enter_context(patch("apps.orchestrator.services.generate_openclaw_config", return_value={"gateway": {}}))
        stack.enter_context(patch("apps.orchestrator.services.config_to_json", return_value="{}"))
        stack.enter_context(patch("apps.orchestrator.services._audit_and_log"))
        stack.enter_context(
            patch(
                "apps.orchestrator.services.create_managed_identity",
                return_value={
                    "id": "/identities/first",
                    "client_id": "client-first",
                    "principal_id": "principal-first",
                },
            )
        )
        stack.enter_context(patch("apps.crypto.keys.mint_and_wrap_dek"))
        stack.enter_context(patch("apps.orchestrator.services.assign_key_vault_role"))
        stack.enter_context(patch("apps.orchestrator.services.assign_acr_pull_role"))
        stack.enter_context(patch("apps.orchestrator.services.create_tenant_file_share"))
        stack.enter_context(patch("apps.orchestrator.services.register_environment_storage"))
        stack.enter_context(patch("apps.orchestrator.services.upload_config_to_file_share"))
        stack.enter_context(patch("apps.orchestrator.services.render_workspace_files", return_value={}))
        stack.enter_context(
            patch(
                "apps.orchestrator.services.create_container_app",
                return_value={"name": "oc-first", "fqdn": "oc-first.internal"},
            )
        )
        stack.enter_context(patch("apps.tenants.emails.send_welcome_email"))
        stack.enter_context(patch("apps.orchestrator.workspace_envelope.push_user_md"))
        stack.enter_context(patch("apps.router.services.send_telegram_message"))
        stack.enter_context(
            patch("apps.router.cron_delivery.resolve_user_channel", side_effect=AssertionError("must not resolve"))
        )
        send_push = stack.enter_context(patch("apps.common.apns.send_push"))
        publish = stack.enter_context(patch("apps.cron.publish.publish_task"))
        yield publish, send_push


@override_settings(
    NBHD_DISABLE_BACKGROUND_THREADS=True,
    OPENCLAW_CONTAINER_SECRET_BACKEND="env",
    OPENROUTER_PER_TENANT_KEYS_ENABLED=False,
)
class FirstSessionWelcomeProvisioningTest(TestCase):
    def test_greeting_row_exists_before_active_is_observable(self):
        from apps.orchestrator import services as orchestrator_services

        user = User.objects.create_user(username="welcome-before-active", display_name="Ordered")
        tenant = Tenant.objects.create(user=user, status=Tenant.Status.PENDING)
        original_finish = orchestrator_services._finish_provisioning
        observed_active_finishes = 0

        def finish_with_order_check(instance, *, status, **values):
            nonlocal observed_active_finishes
            if instance.pk == tenant.pk and status == Tenant.Status.ACTIVE:
                observed_active_finishes += 1
                self.assertTrue(
                    AppChatMessage.objects.filter(tenant=tenant, status=AppChatMessage.Status.READY).exists()
                )
                self.assertTrue(ProactiveOutbound.objects.filter(tenant=tenant, channel="app").exists())
            return original_finish(instance, status=status, **values)

        with (
            _mock_provision_dependencies(),
            patch(
                "apps.orchestrator.services._finish_provisioning",
                side_effect=finish_with_order_check,
            ),
        ):
            provision_tenant(str(tenant.id))

        self.assertEqual(observed_active_finishes, 1)

    def test_double_provision_creates_exactly_one_greeting(self):
        user = User.objects.create_user(username="double-provision", display_name="Double")
        tenant = Tenant.objects.create(user=user, status=Tenant.Status.PENDING)

        with _mock_provision_dependencies():
            provision_tenant(str(tenant.id))
            provision_tenant(str(tenant.id))

        self.assertEqual(ProactiveOutbound.objects.filter(tenant=tenant).count(), 1)
        self.assertEqual(AppChatMessage.objects.filter(tenant=tenant).count(), 1)
        tenant.refresh_from_db()
        self.assertIn(FIRST_SESSION_WELCOME_KEY, tenant.welcomes_sent)

    def test_app_delivery_failure_never_blocks_active_status(self):
        user = User.objects.create_user(username="welcome-failure", display_name="Failure")
        tenant = Tenant.objects.create(user=user, status=Tenant.Status.PENDING)

        with (
            _mock_provision_dependencies(),
            patch(
                "apps.router.chat_views.create_delivered_app_assistant_message",
                side_effect=RuntimeError("app chat unavailable"),
            ),
        ):
            provision_tenant(str(tenant.id))

        tenant.refresh_from_db()
        self.assertEqual(tenant.status, Tenant.Status.ACTIVE)
        self.assertIn(FIRST_SESSION_WELCOME_KEY, tenant.welcomes_sent)
        self.assertFalse(AppChatMessage.objects.filter(tenant=tenant).exists())
        self.assertFalse(ProactiveOutbound.objects.filter(tenant=tenant).exists())

    def test_suppressed_welcome_activates_without_greeting_or_stamp(self):
        user = User.objects.create_user(username="welcome-suppressed", display_name="Suppressed")
        tenant = Tenant.objects.create(user=user, status=Tenant.Status.PENDING)

        with _mock_provision_dependencies():
            provision_tenant(str(tenant.id), send_first_session_welcome=False)

        tenant.refresh_from_db()
        self.assertEqual(tenant.status, Tenant.Status.ACTIVE)
        self.assertNotIn(FIRST_SESSION_WELCOME_KEY, tenant.welcomes_sent)
        self.assertFalse(ProactiveOutbound.objects.filter(tenant=tenant).exists())

    def test_ensure_tenant_provisioned_entrance_greets(self):
        from apps.tenants.services import ensure_tenant_provisioned

        user = User.objects.create_user(username="ensure-entrance", display_name="Ensure")

        with _mock_provision_dependencies() as (publish, send_push):

            def execute_provision(task_name, *args, **kwargs):
                if task_name == "provision_tenant":
                    kwargs.pop("idempotency_key", None)
                    provision_tenant(*args, **kwargs)

            publish.side_effect = execute_provision
            tenant, created, published = ensure_tenant_provisioned(user)

        self.assertTrue(created)
        self.assertTrue(published)
        self.assertEqual(AppChatMessage.objects.filter(tenant=tenant).count(), 1)
        self.assertEqual(ProactiveOutbound.objects.filter(tenant=tenant, channel="app").count(), 1)
        send_push.assert_not_called()

    def test_stripe_checkout_entrance_greets(self):
        from apps.billing.services import handle_checkout_completed
        from apps.tenants.services import create_tenant

        tenant = create_tenant(display_name="Stripe", telegram_chat_id=898989)

        with _mock_provision_dependencies() as (publish, _send_push):

            def execute_provision(task_name, *args, **kwargs):
                if task_name == "provision_tenant":
                    kwargs.pop("idempotency_key", None)
                    provision_tenant(*args, **kwargs)

            publish.side_effect = execute_provision
            handle_checkout_completed(
                {
                    "metadata": {"user_id": str(tenant.user_id), "tier": "starter"},
                    "customer": "cus_first_session",
                    "subscription": "sub_first_session",
                }
            )

        self.assertEqual(ProactiveOutbound.objects.filter(tenant=tenant, channel="app").count(), 1)
        self.assertEqual(AppChatMessage.objects.filter(tenant=tenant).count(), 1)

    def test_active_tenant_direct_reentry_cannot_greet(self):
        user = User.objects.create_user(username="active-reentry", display_name="Existing")
        tenant = Tenant.objects.create(
            user=user,
            status=Tenant.Status.ACTIVE,
            container_id="oc-existing",
            container_fqdn="oc-existing.internal",
        )

        with patch("apps.cron.publish.publish_task") as publish:
            provision_tenant(str(tenant.id))

        publish.assert_not_called()
        self.assertFalse(ProactiveOutbound.objects.filter(tenant=tenant).exists())
        tenant.refresh_from_db()
        self.assertNotIn(FIRST_SESSION_WELCOME_KEY, tenant.welcomes_sent)

    def test_live_lease_is_not_stolen(self):
        user = User.objects.create_user(username="live-provision-lease", display_name="Live")
        live_lease = timezone.now() - timedelta(minutes=1)
        tenant = Tenant.objects.create(
            user=user,
            status=Tenant.Status.PROVISIONING,
            provision_lease_at=live_lease,
        )

        with (
            _mock_provision_dependencies() as (publish, _send_push),
            patch("apps.orchestrator.services.generate_openclaw_config") as generate_config,
        ):
            provision_tenant(str(tenant.id))

        generate_config.assert_not_called()
        publish.assert_called_once_with(
            "provision_tenant",
            str(tenant.id),
            delay_seconds=publish.call_args.kwargs["delay_seconds"],
            idempotency_key=f"provision-{tenant.id}-lease-{int(live_lease.timestamp())}",
        )
        remaining_plus_buffer = (live_lease + timedelta(minutes=30) - timezone.now()).total_seconds() + 60
        self.assertAlmostEqual(
            publish.call_args.kwargs["delay_seconds"],
            remaining_plus_buffer,
            delta=5,
        )
        tenant.refresh_from_db()
        self.assertEqual(tenant.status, Tenant.Status.PROVISIONING)
        self.assertEqual(tenant.provision_lease_at, live_lease)

    def test_pending_loser_schedules_recovery_for_fresh_live_row(self):
        from django.db.models.query import QuerySet

        user = User.objects.create_user(username="pending-lease-loser", display_name="Pending Loser")
        tenant = Tenant.objects.create(user=user, status=Tenant.Status.PENDING)
        live_lease = timezone.now()
        original_update = QuerySet.update

        def competing_claim_wins(_queryset, **_updates):
            original_update(
                Tenant.objects.filter(pk=tenant.pk),
                status=Tenant.Status.PROVISIONING,
                provision_lease_at=live_lease,
                updated_at=live_lease,
            )
            return 0

        with (
            patch.object(QuerySet, "update", autospec=True, side_effect=competing_claim_wins),
            patch("apps.cron.publish.publish_task") as publish,
        ):
            provision_tenant(str(tenant.id))

        publish.assert_called_once_with(
            "provision_tenant",
            str(tenant.id),
            delay_seconds=publish.call_args.kwargs["delay_seconds"],
            idempotency_key=f"provision-{tenant.id}-lease-{int(live_lease.timestamp())}",
        )
        remaining_plus_buffer = (live_lease + timedelta(minutes=30) - timezone.now()).total_seconds() + 60
        self.assertAlmostEqual(
            publish.call_args.kwargs["delay_seconds"],
            remaining_plus_buffer,
            delta=5,
        )

    def test_live_lease_follow_up_publish_failure_is_best_effort(self):
        user = User.objects.create_user(username="live-lease-publish-failure", display_name="Live Failure")
        live_lease = timezone.now() - timedelta(minutes=1)
        tenant = Tenant.objects.create(
            user=user,
            status=Tenant.Status.PROVISIONING,
            provision_lease_at=live_lease,
        )

        with patch("apps.cron.publish.publish_task", side_effect=RuntimeError("qstash down")) as publish:
            provision_tenant(str(tenant.id))

        publish.assert_called_once()
        tenant.refresh_from_db()
        self.assertEqual(tenant.status, Tenant.Status.PROVISIONING)
        self.assertEqual(tenant.provision_lease_at, live_lease)

    def test_stale_lease_is_reclaimed_and_cleared_on_success(self):
        user = User.objects.create_user(username="stale-provision-lease", display_name="Stale")
        stale_lease = timezone.now() - timedelta(minutes=31)
        tenant = Tenant.objects.create(
            user=user,
            status=Tenant.Status.PROVISIONING,
            provision_lease_at=stale_lease,
        )

        with (
            _mock_provision_dependencies(),
            patch(
                "apps.orchestrator.services.generate_openclaw_config",
                return_value={"gateway": {}},
            ) as generate_config,
        ):
            provision_tenant(str(tenant.id))

        generate_config.assert_called_once_with(tenant)
        tenant.refresh_from_db()
        self.assertEqual(tenant.status, Tenant.Status.ACTIVE)
        self.assertIsNone(tenant.provision_lease_at)

    def test_failure_clears_lease(self):
        user = User.objects.create_user(username="failed-provision-lease", display_name="Failed")
        tenant = Tenant.objects.create(user=user, status=Tenant.Status.PENDING)

        with (
            _mock_provision_dependencies(),
            patch(
                "apps.orchestrator.services.create_managed_identity",
                side_effect=RuntimeError("identity failed"),
            ),
            self.assertRaisesRegex(RuntimeError, "identity failed"),
        ):
            provision_tenant(str(tenant.id))

        tenant.refresh_from_db()
        self.assertEqual(tenant.status, Tenant.Status.PENDING)
        self.assertIsNone(tenant.provision_lease_at)

    def test_renewal_moves_lease_forward(self):
        from apps.orchestrator.services import _renew_provision_lease

        user = User.objects.create_user(username="renew-provision-lease", display_name="Renew")
        prior_lease = timezone.now() - timedelta(minutes=1)
        tenant = Tenant.objects.create(
            user=user,
            status=Tenant.Status.PROVISIONING,
            provision_lease_at=prior_lease,
        )

        _renew_provision_lease(tenant)

        tenant.refresh_from_db()
        self.assertGreater(tenant.provision_lease_at, prior_lease)

    def test_post_azure_db_write_reconnects_and_restores_rls_once(self):
        from apps.orchestrator.services import _retry_provision_db_write

        user = User.objects.create_user(username="provision-db-reconnect", display_name="Reconnect")
        tenant = Tenant.objects.create(user=user, status=Tenant.Status.PROVISIONING)
        attempts = 0

        def operation():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OperationalError("idle connection closed")
            return 1

        with (
            patch("apps.orchestrator.services.connection.close") as close_connection,
            patch("apps.tenants.middleware.set_rls_context") as set_rls_context,
        ):
            result = _retry_provision_db_write(tenant, operation)

        self.assertEqual(result, 1)
        self.assertEqual(attempts, 2)
        close_connection.assert_called_once_with()
        set_rls_context.assert_called_once_with(tenant_id=tenant.id, service_role=True)

    def test_active_tenant_repair_cannot_greet(self):
        user = User.objects.create_user(username="active-repair", display_name="Existing Repair")
        tenant = Tenant.objects.create(user=user, status=Tenant.Status.ACTIVE)

        with _mock_provision_dependencies():
            summary = repair_stale_tenant_provisioning(tenant_id=str(tenant.id))

        self.assertEqual(summary["repaired"], 1)
        self.assertFalse(ProactiveOutbound.objects.filter(tenant=tenant).exists())
        tenant.refresh_from_db()
        self.assertNotIn(FIRST_SESSION_WELCOME_KEY, tenant.welcomes_sent)


@override_settings(
    NBHD_DISABLE_BACKGROUND_THREADS=True,
    OPENCLAW_CONTAINER_SECRET_BACKEND="env",
    OPENROUTER_PER_TENANT_KEYS_ENABLED=False,
)
class FirstSessionWelcomeConcurrencyTest(TransactionTestCase):
    def test_concurrent_provision_claims_once_and_keeps_db_key_equal_to_kv(self):
        user = User.objects.create_user(username="welcome-race", display_name="Race")
        tenant = Tenant.objects.create(user=user, status=Tenant.Status.PENDING)
        errors: list[BaseException] = []
        error_lock = threading.Lock()
        config_calls: list[str] = []
        kv_keys: list[str] = []
        side_effect_lock = threading.Lock()
        winner_in_config = threading.Event()
        release_winner = threading.Event()
        one_run_finished = threading.Event()

        def counted_config(_tenant):
            with side_effect_lock:
                config_calls.append(str(_tenant.id))
            winner_in_config.set()
            if not release_winner.wait(timeout=10):
                raise TimeoutError("concurrent lease loser did not return")
            return {"gateway": {}}

        def capture_kv_key(_tenant_id, internal_api_key):
            with side_effect_lock:
                kv_keys.append(internal_api_key)
            return f"tenant-{_tenant_id}-internal-key"

        def provision_from_fresh_connection():
            close_old_connections()
            try:
                provision_tenant(str(tenant.pk))
            except BaseException as exc:
                with error_lock:
                    errors.append(exc)
            finally:
                close_old_connections()
                one_run_finished.set()

        with (
            override_settings(OPENCLAW_CONTAINER_SECRET_BACKEND="keyvault"),
            _mock_provision_dependencies(),
            patch(
                "apps.orchestrator.services.generate_openclaw_config",
                side_effect=counted_config,
            ),
            patch(
                "apps.orchestrator.services.store_tenant_internal_key_in_key_vault",
                side_effect=capture_kv_key,
            ),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            winner = pool.submit(provision_from_fresh_connection)
            self.assertTrue(winner_in_config.wait(timeout=10))
            loser = pool.submit(provision_from_fresh_connection)
            try:
                self.assertTrue(one_run_finished.wait(timeout=10))
            finally:
                release_winner.set()
            loser.result(timeout=30)
            winner.result(timeout=30)

        self.assertEqual(errors, [])
        self.assertEqual(len(config_calls), 1)
        self.assertEqual(len(kv_keys), 1)
        self.assertEqual(ProactiveOutbound.objects.filter(tenant=tenant).count(), 1)
        self.assertEqual(AppChatMessage.objects.filter(tenant=tenant).count(), 1)
        tenant.refresh_from_db()
        self.assertEqual(tenant.internal_api_key, kv_keys[0])
        self.assertIsNone(tenant.provision_lease_at)
        self.assertIn(FIRST_SESSION_WELCOME_KEY, tenant.welcomes_sent)
