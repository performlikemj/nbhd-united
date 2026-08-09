"""First-session welcome copy, delivery, idempotency, and entry-path tests."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

from django.db import close_old_connections, connection
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
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
        user = User.objects.create_user(username="welcome-before-active", display_name="Ordered")
        tenant = Tenant.objects.create(user=user, status=Tenant.Status.PENDING)
        original_save = Tenant.save
        observed_active_saves = 0

        def save_with_order_check(instance, *args, **kwargs):
            nonlocal observed_active_saves
            update_fields = kwargs.get("update_fields") or []
            if instance.pk == tenant.pk and instance.status == Tenant.Status.ACTIVE and "status" in update_fields:
                observed_active_saves += 1
                self.assertTrue(
                    AppChatMessage.objects.filter(tenant=tenant, status=AppChatMessage.Status.READY).exists()
                )
                self.assertTrue(ProactiveOutbound.objects.filter(tenant=tenant, channel="app").exists())
            return original_save(instance, *args, **kwargs)

        with (
            _mock_provision_dependencies(),
            patch.object(Tenant, "save", autospec=True, side_effect=save_with_order_check),
        ):
            provision_tenant(str(tenant.id))

        self.assertEqual(observed_active_saves, 1)

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

        provision_tenant(str(tenant.id))

        self.assertFalse(ProactiveOutbound.objects.filter(tenant=tenant).exists())
        tenant.refresh_from_db()
        self.assertNotIn(FIRST_SESSION_WELCOME_KEY, tenant.welcomes_sent)

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
    def test_concurrent_provision_uses_real_row_lock_and_creates_one_greeting(self):
        user = User.objects.create_user(username="welcome-race", display_name="Race")
        tenant = Tenant.objects.create(user=user, status=Tenant.Status.PENDING)
        provision_barrier = threading.Barrier(2)
        welcome_barrier = threading.Barrier(2)
        errors: list[BaseException] = []
        error_lock = threading.Lock()

        def synchronized_config(_tenant):
            provision_barrier.wait(timeout=10)
            return {"gateway": {}}

        def synchronized_welcome(fresh_tenant):
            welcome_barrier.wait(timeout=10)
            return seed_first_session_welcome(fresh_tenant)

        def provision_from_fresh_connection():
            close_old_connections()
            try:
                provision_tenant(str(tenant.pk))
            except BaseException as exc:
                with error_lock:
                    errors.append(exc)
            finally:
                close_old_connections()

        with (
            _mock_provision_dependencies(),
            patch("apps.orchestrator.services.generate_openclaw_config", side_effect=synchronized_config),
            patch(
                "apps.orchestrator.first_session_welcome.seed_first_session_welcome",
                side_effect=synchronized_welcome,
            ),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            futures = [pool.submit(provision_from_fresh_connection) for _ in range(2)]
            for future in futures:
                future.result(timeout=30)

        self.assertEqual(errors, [])
        self.assertEqual(ProactiveOutbound.objects.filter(tenant=tenant).count(), 1)
        self.assertEqual(AppChatMessage.objects.filter(tenant=tenant).count(), 1)
        tenant.refresh_from_db()
        self.assertIn(FIRST_SESSION_WELCOME_KEY, tenant.welcomes_sent)
