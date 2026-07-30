"""Deletion and durable-outbox tests for Apple refresh-token revocation."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import httpx
from cryptography.fernet import Fernet
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from django.http import HttpResponse
from django.test import RequestFactory, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from apps.billing.services import handle_subscription_deleted
from apps.cron.tasks import revoke_apple_token_task
from apps.cron.views import TASK_MAP

from .admin import UserAdmin
from .apple_crypto import decrypt_apple_refresh_token, encrypt_apple_refresh_token
from .apple_models import AppleRevocationOutbox, ExternalIdentity
from .models import Tenant
from .test_apple_auth import (
    EC_PRIVATE_PEM,
    FERNET_KEY,
    KEY_ID,
    READY_SETTINGS,
    REDIRECT_URI,
    SERVICES_ID,
    TEAM_ID,
    FakeResponse,
)
from .views import _do_hard_delete

User = get_user_model()


@override_settings(**READY_SETTINGS)
class AppleDeletionOutboxTests(TestCase):
    def setUp(self):
        self.publish_patch = patch("apps.cron.publish.publish_task")
        self.publish = self.publish_patch.start()
        self.addCleanup(self.publish_patch.stop)

    def make_user_with_identity(self, *, email="delete@example.com", subject="delete-subject"):
        user = User.objects.create_user(username=email, email=email, password="Password123!")
        identity = ExternalIdentity.objects.create(
            user=user,
            subject=subject,
            audience=SERVICES_ID,
            email_at_auth=email,
            email_verified_at_auth=True,
            refresh_token_encrypted=encrypt_apple_refresh_token("delete-refresh-token"),
            refresh_token_updated_at=timezone.now(),
        )
        return user, identity

    def test_immediate_delete_preserves_outbox_publishes_after_commit_and_deletes_user(self):
        user, identity = self.make_user_with_identity()
        user_id = user.id
        with self.captureOnCommitCallbacks(execute=True):
            _do_hard_delete(user)

        self.assertFalse(User.objects.filter(id=user_id).exists())
        self.assertFalse(ExternalIdentity.objects.filter(id=identity.id).exists())
        outbox = AppleRevocationOutbox.objects.get()
        self.assertEqual(outbox.subject, "delete-subject")
        self.assertEqual(
            decrypt_apple_refresh_token(outbox.token_ciphertext),
            "delete-refresh-token",
        )
        self.publish.assert_called_once_with(
            "revoke_apple_token",
            str(outbox.id),
            idempotency_key=f"apple-revoke-{outbox.id}",
        )

    def test_publish_failure_never_blocks_user_deletion(self):
        self.publish.side_effect = RuntimeError("qstash down")
        user, _ = self.make_user_with_identity()
        user_id = user.id
        with self.captureOnCommitCallbacks(execute=True):
            _do_hard_delete(user)
        self.assertFalse(User.objects.filter(id=user_id).exists())
        self.assertEqual(AppleRevocationOutbox.objects.count(), 1)

    @patch("apps.orchestrator.services.deprovision_tenant")
    def test_webhook_finalized_deletion_uses_same_outbox_path(self, deprovision):
        user, _ = self.make_user_with_identity(email="scheduled@example.com")
        user_id = user.id
        Tenant.objects.create(
            user=user,
            stripe_subscription_id="sub-delete-me",
            pending_deletion=True,
        )
        with self.captureOnCommitCallbacks(execute=True):
            handle_subscription_deleted({"id": "sub-delete-me"})
        self.assertFalse(User.objects.filter(id=user_id).exists())
        self.assertEqual(AppleRevocationOutbox.objects.count(), 1)
        self.assertEqual(self.publish.call_count, 1)
        deprovision.assert_called_once()

    def test_pre_delete_fallback_writes_without_publishing_and_deduplicates(self):
        user, identity = self.make_user_with_identity()
        existing = AppleRevocationOutbox.objects.create(
            token_ciphertext=identity.refresh_token_encrypted,
            subject=identity.subject,
        )
        user.delete()
        self.assertEqual(AppleRevocationOutbox.objects.count(), 1)
        self.assertTrue(AppleRevocationOutbox.objects.filter(id=existing.id).exists())
        self.publish.assert_not_called()

    def test_pre_delete_fallback_handles_direct_queryset_delete(self):
        user, _ = self.make_user_with_identity()
        user_id = user.id
        User.objects.filter(id=user.id).delete()
        self.assertFalse(User.objects.filter(id=user_id).exists())
        self.assertEqual(AppleRevocationOutbox.objects.count(), 1)
        self.publish.assert_not_called()

    def test_admin_delete_model_and_queryset_route_each_user_through_helper(self):
        user_one = User.objects.create_user(username="admin-one", email="one@example.com")
        user_two = User.objects.create_user(username="admin-two", email="two@example.com")
        model_admin = UserAdmin(User, admin.AdminSite())
        request = RequestFactory().post("/admin/tenants/user/")

        with patch("apps.tenants.views._do_hard_delete") as hard_delete:
            model_admin.delete_model(request, user_one)
            hard_delete.assert_called_once_with(user_one)
            hard_delete.reset_mock()
            model_admin.delete_queryset(
                request,
                User.objects.filter(id__in=[user_one.id, user_two.id]),
            )
            self.assertEqual(hard_delete.call_count, 2)
            self.assertCountEqual(
                [call.args[0].id for call in hard_delete.call_args_list],
                [user_one.id, user_two.id],
            )


@override_settings(**READY_SETTINGS)
class AppleRevocationHandlerTests(TransactionTestCase):
    def make_outbox(self, token="handler-refresh"):
        return AppleRevocationOutbox.objects.create(
            token_ciphertext=encrypt_apple_refresh_token(token),
            subject="handler-subject",
        )

    def test_handler_posts_exact_form_outside_transaction_and_is_idempotent(self):
        row = self.make_outbox()

        def apple_post(url, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            self.assertEqual(url, "https://appleid.apple.com/auth/revoke")
            self.assertEqual(kwargs["timeout"], 5)
            self.assertEqual(kwargs["data"]["client_id"], SERVICES_ID)
            self.assertEqual(kwargs["data"]["token"], "handler-refresh")
            self.assertEqual(kwargs["data"]["token_type_hint"], "refresh_token")
            self.assertIn("client_secret", kwargs["data"])
            return FakeResponse(200, {})

        with patch("apps.tenants.apple_client.httpx.post", side_effect=apple_post) as mocked:
            result = revoke_apple_token_task(str(row.id))
            second = revoke_apple_token_task(str(row.id))
        self.assertEqual(result["status"], "revoked")
        self.assertEqual(second["status"], "already_revoked")
        self.assertEqual(mocked.call_count, 1)
        row.refresh_from_db()
        self.assertIsNotNone(row.revoked_at)
        self.assertEqual(row.attempts, 1)

    def test_apple_invalid_token_4xx_is_treated_as_revoked(self):
        row = self.make_outbox()
        with patch(
            "apps.tenants.apple_client.httpx.post",
            return_value=FakeResponse(400, {"error": "invalid_token"}),
        ):
            result = revoke_apple_token_task(str(row.id))
        self.assertEqual(result, {"status": "revoked", "apple_status": 400})
        row.refresh_from_db()
        self.assertIsNotNone(row.revoked_at)
        self.assertEqual(row.last_error, "apple_400_treated_as_revoked")

    def test_apple_invalid_client_4xx_remains_retryable(self):
        row = self.make_outbox()
        with (
            patch(
                "apps.tenants.apple_client.httpx.post",
                return_value=FakeResponse(400, {"error": "invalid_client"}),
            ),
            self.assertRaises(Exception),
        ):
            revoke_apple_token_task(str(row.id))
        row.refresh_from_db()
        self.assertIsNone(row.revoked_at)
        self.assertEqual(row.attempts, 1)
        self.assertEqual(row.last_error, "retry:revoke_invalid_client")

    def test_transport_failure_records_error_and_raises_for_qstash_retry(self):
        row = self.make_outbox()
        with (
            patch(
                "apps.tenants.apple_client.httpx.post",
                side_effect=httpx.ReadTimeout("network down"),
            ),
            self.assertLogs("apps.cron.tasks", level="WARNING") as logs,
            self.assertRaises(Exception),
        ):
            revoke_apple_token_task(str(row.id))
        row.refresh_from_db()
        self.assertEqual(row.attempts, 1)
        self.assertEqual(row.last_error, "retry:revoke_request_failed")
        self.assertIsNone(row.revoked_at)
        combined = "\n".join(logs.output)
        self.assertIn("transport_failed", combined)
        self.assertNotIn("handler-refresh", combined)

    def test_corrupt_or_missing_old_key_ciphertext_becomes_terminal_after_three_attempts(self):
        old_key = Fernet.generate_key().decode()
        with self.settings(APPLE_SIWA_TOKEN_ENC_KEYS=[old_key]):
            ciphertext = encrypt_apple_refresh_token("old-key-token")
        row = AppleRevocationOutbox.objects.create(
            token_ciphertext=ciphertext,
            subject="old-key-subject",
        )

        for _ in range(2):
            with self.assertRaises(RuntimeError):
                revoke_apple_token_task(str(row.id))
        result = revoke_apple_token_task(str(row.id))
        self.assertEqual(result, {"status": "terminal", "reason": "decrypt_failed"})
        row.refresh_from_db()
        last_attempt_at = row.last_attempt_at
        self.assertEqual(row.attempts, 3)
        self.assertEqual(row.last_error, "terminal:decrypt_failed")
        self.assertIsNone(row.revoked_at)
        duplicate = revoke_apple_token_task(str(row.id))
        self.assertEqual(duplicate, {"status": "terminal", "reason": "decrypt_failed"})
        row.refresh_from_db()
        self.assertEqual(row.attempts, 3)
        self.assertEqual(row.last_attempt_at, last_attempt_at)

    def test_genuinely_corrupt_ciphertext_becomes_terminal(self):
        row = AppleRevocationOutbox.objects.create(
            token_ciphertext="not-a-fernet-ciphertext",
            subject="corrupt-subject",
        )
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                revoke_apple_token_task(str(row.id))
        result = revoke_apple_token_task(str(row.id))
        self.assertEqual(result, {"status": "terminal", "reason": "decrypt_failed"})
        row.refresh_from_db()
        self.assertEqual(row.attempts, 3)
        self.assertEqual(row.last_error, "terminal:decrypt_failed")

    def test_multifernet_decrypts_with_old_rotation_key(self):
        old_key = Fernet.generate_key().decode()
        new_key = Fernet.generate_key().decode()
        with self.settings(APPLE_SIWA_TOKEN_ENC_KEYS=[old_key]):
            ciphertext = encrypt_apple_refresh_token("rotated-old-token")
        row = AppleRevocationOutbox.objects.create(
            token_ciphertext=ciphertext,
            subject="rotation-subject",
        )
        with (
            self.settings(APPLE_SIWA_TOKEN_ENC_KEYS=[new_key, old_key]),
            patch(
                "apps.tenants.apple_client.httpx.post",
                return_value=FakeResponse(200, {}),
            ),
        ):
            result = revoke_apple_token_task(str(row.id))
        self.assertEqual(result["status"], "revoked")
        row.refresh_from_db()
        self.assertIsNotNone(row.revoked_at)

    def test_missing_row_is_idempotent_success(self):
        result = revoke_apple_token_task("00000000-0000-0000-0000-000000000000")
        self.assertEqual(result, {"status": "missing"})

    def test_task_is_registered(self):
        self.assertEqual(
            TASK_MAP["revoke_apple_token"],
            "apps.cron.tasks.revoke_apple_token_task",
        )


@override_settings(
    APPLE_SIWA_SERVICES_ID=SERVICES_ID,
    APPLE_SIWA_TEAM_ID=TEAM_ID,
    APPLE_SIWA_KEY_ID=KEY_ID,
    APPLE_SIWA_PRIVATE_KEY=EC_PRIVATE_PEM,
    APPLE_SIWA_REDIRECT_URI=REDIRECT_URI,
    APPLE_SIWA_TOKEN_ENC_KEYS=[FERNET_KEY],
)
class AppleRevocationBackstopCommandTests(TestCase):
    def test_command_republishes_only_stale_nonterminal_unrevoked_rows(self):
        stale = AppleRevocationOutbox.objects.create(
            token_ciphertext="ciphertext-stale",
            subject="stale",
        )
        fresh = AppleRevocationOutbox.objects.create(
            token_ciphertext="ciphertext-fresh",
            subject="fresh",
        )
        terminal = AppleRevocationOutbox.objects.create(
            token_ciphertext="ciphertext-terminal",
            subject="terminal",
            last_error="terminal:decrypt_failed",
        )
        revoked = AppleRevocationOutbox.objects.create(
            token_ciphertext="ciphertext-revoked",
            subject="revoked",
            revoked_at=timezone.now(),
        )
        old = timezone.now() - timedelta(hours=2)
        AppleRevocationOutbox.objects.filter(id__in=[stale.id, terminal.id, revoked.id]).update(created_at=old)

        with patch("apps.cron.publish.publish_task") as publish:
            call_command("process_apple_revocation_outbox")

        publish.assert_called_once_with(
            "revoke_apple_token",
            str(stale.id),
            idempotency_key=f"apple-revoke-{stale.id}",
        )
        self.assertTrue(AppleRevocationOutbox.objects.filter(id=fresh.id).exists())
        self.assertTrue(AppleRevocationOutbox.objects.filter(id=terminal.id).exists())
        self.assertTrue(AppleRevocationOutbox.objects.filter(id=revoked.id).exists())


@override_settings(**READY_SETTINGS)
class AppleAdminAtomicInvariantTests(TransactionTestCase):
    def test_late_container_update_hibernates_only_after_delete_commit(self):
        target = User.objects.create_user(
            username="late-container-target",
            email="late-container@example.com",
        )
        tenant = Tenant.objects.create(user=target, container_id="")
        observed = []

        def deprovision(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            Tenant.objects.filter(id=tenant.id).update(container_id="oc-late")
            raise RuntimeError("deprovision failed after concurrent update")

        def hibernate(container_id):
            observed.append((container_id, connection.in_atomic_block))

        with (
            patch(
                "apps.orchestrator.services.deprovision_tenant",
                side_effect=deprovision,
            ),
            patch(
                "apps.orchestrator.azure_client.hibernate_container_app",
                side_effect=hibernate,
            ),
            patch("apps.cron.publish.publish_task"),
        ):
            _do_hard_delete(target)

        self.assertEqual(observed, [("oc-late", False)])
        self.assertFalse(User.objects.filter(id=target.id).exists())

    def test_confirmed_admin_delete_runs_deprovision_outside_atomic(self):
        administrator = User.objects.create_superuser(
            username="admin",
            email="admin@example.com",
            password="AdminPassword123!",
        )
        target = User.objects.create_user(
            username="admin-delete-target",
            email="target@example.com",
        )
        Tenant.objects.create(user=target, container_id="oc-admin-delete")
        model_admin = UserAdmin(User, admin.AdminSite())
        request = RequestFactory().post(
            f"/admin/tenants/user/{target.id}/delete/",
            {"post": "yes"},
        )
        request.user = administrator
        request._dont_enforce_csrf_checks = True
        observed = []

        def deprovision(*args, **kwargs):
            observed.append(connection.in_atomic_block)
            raise RuntimeError("deprovision failed")

        hibernate_observed = []

        def hibernate(*args, **kwargs):
            hibernate_observed.append(connection.in_atomic_block)

        with (
            patch(
                "apps.orchestrator.services.deprovision_tenant",
                side_effect=deprovision,
            ),
            patch(
                "apps.orchestrator.azure_client.hibernate_container_app",
                side_effect=hibernate,
            ),
            patch.object(
                model_admin,
                "log_deletions",
            ),
            patch.object(
                model_admin,
                "response_delete",
                return_value=HttpResponse("deleted"),
            ),
            patch(
                "apps.cron.publish.publish_task",
            ),
        ):
            response = model_admin.delete_view(request, str(target.id))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(observed, [False])
        self.assertEqual(hibernate_observed, [False])
        self.assertFalse(User.objects.filter(id=target.id).exists())
