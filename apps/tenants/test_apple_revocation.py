"""Deletion and durable-outbox tests for Apple refresh-token revocation."""

from __future__ import annotations

import threading
from datetime import timedelta
from unittest.mock import patch

import httpx
from cryptography.fernet import Fernet
from django.contrib import admin
from django.contrib.admin.models import DELETION, LogEntry
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.db import IntegrityError, connection, connections, transaction
from django.middleware.csrf import get_token
from django.test import Client, RequestFactory, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.billing.services import handle_subscription_deleted
from apps.cron.tasks import (
    _record_apple_revocation_error,
    process_apple_revocation_outbox,
    revoke_apple_token_task,
)
from apps.cron.views import TASK_MAP

from .admin import UserAdmin
from .apple_client import AppleGrant, AppleUnavailable
from .apple_crypto import decrypt_apple_refresh_token, encrypt_apple_refresh_token
from .apple_models import (
    AppleGrant as AppleGrantRecord,
)
from .apple_models import (
    AppleRevocationOutbox,
    ExternalIdentity,
)
from .apple_services import AppleResolutionRejected, link_apple_identity
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
class AppleDeletionOutboxTests(TransactionTestCase):
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
        AppleGrantRecord.objects.create(
            identity=identity,
            client_id=SERVICES_ID,
            refresh_token_encrypted=identity.refresh_token_encrypted,
        )
        return user, identity

    def test_immediate_delete_preserves_outbox_publishes_after_commit_and_deletes_user(self):
        user, identity = self.make_user_with_identity()
        user_id = user.id
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

    def test_delete_copies_one_outbox_row_per_audience_grant(self):
        user, identity = self.make_user_with_identity(email="dual-delete@example.com")
        AppleGrantRecord.objects.create(
            identity=identity,
            client_id=READY_SETTINGS["APPLE_SIWA_BUNDLE_ID"],
            refresh_token_encrypted=encrypt_apple_refresh_token("bundle-delete-refresh"),
        )

        _do_hard_delete(user)

        rows = list(AppleRevocationOutbox.objects.order_by("client_id"))
        self.assertEqual(len(rows), 2)
        self.assertCountEqual(
            [row.client_id for row in rows],
            [SERVICES_ID, READY_SETTINGS["APPLE_SIWA_BUNDLE_ID"]],
        )
        self.assertTrue(all(row.subject_verified for row in rows))
        self.assertEqual(self.publish.call_count, 2)

    def test_publish_failure_never_blocks_user_deletion(self):
        self.publish.side_effect = RuntimeError("qstash down")
        user, _ = self.make_user_with_identity()
        user_id = user.id
        with self.assertLogs("apps.tenants.apple_services", level="WARNING") as logs:
            _do_hard_delete(user)
        self.assertFalse(User.objects.filter(id=user_id).exists())
        outbox = AppleRevocationOutbox.objects.get()
        combined = "\n".join(logs.output)
        self.assertNotIn("delete-refresh-token", combined)
        self.assertNotIn(outbox.token_ciphertext, combined)
        self.assertNotIn(outbox.subject, combined)

    @patch("apps.orchestrator.services.deprovision_tenant")
    def test_webhook_finalized_deletion_uses_same_outbox_path(self, deprovision):
        user, _ = self.make_user_with_identity(email="scheduled@example.com")
        user_id = user.id
        Tenant.objects.create(
            user=user,
            stripe_subscription_id="sub-delete-me",
            pending_deletion=True,
        )
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
            client_id=SERVICES_ID,
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

    def test_primary_outbox_db_failure_rolls_back_savepoint_and_fallback_preserves_grant(self):
        from . import apple_services

        user, identity = self.make_user_with_identity(email="fallback-after-failure@example.com")
        user_id = user.id
        real_copy = apple_services._copy_identity_tokens_to_outbox
        calls = 0

        def fail_primary_insert_once(locked_user, *, using=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                # Raise from a real backend INSERT inside the primary helper's
                # savepoint. The fallback signal must still be able to write
                # after that savepoint rolls back.
                return AppleRevocationOutbox.objects.using(using).create(
                    token_ciphertext=None,
                    subject="intentionally-invalid",
                )
            return real_copy(locked_user, using=using)

        with (
            patch(
                "apps.tenants.apple_services._copy_identity_tokens_to_outbox",
                side_effect=fail_primary_insert_once,
            ),
            self.assertLogs("apps.tenants.views", level="WARNING"),
        ):
            _do_hard_delete(user)

        self.assertFalse(User.objects.filter(id=user_id).exists())
        self.assertFalse(ExternalIdentity.objects.filter(id=identity.id).exists())
        outbox = AppleRevocationOutbox.objects.get()
        self.assertEqual(
            decrypt_apple_refresh_token(outbox.token_ciphertext),
            "delete-refresh-token",
        )
        self.assertEqual(calls, 2)
        self.publish.assert_not_called()

    def test_direct_delete_racing_link_never_loses_a_committed_grant(self):
        user = User.objects.create_user(
            username="delete-link-race",
            email="delete-link-race@example.com",
            password="Password123!",
        )
        user_id = user.id
        delete_has_user_lock = threading.Event()
        release_delete = threading.Event()
        link_insert_attempted = threading.Event()
        delete_errors: list[BaseException] = []
        link_errors: list[BaseException] = []
        link_succeeded: list[bool] = []

        from . import apple_services

        real_fallback = apple_services.write_apple_revocation_outbox_fallback
        real_identity_create = ExternalIdentity.objects.create

        def paused_fallback(locked_user, *, using=None):
            delete_has_user_lock.set()
            if not release_delete.wait(5):
                raise TimeoutError("test did not release direct delete")
            return real_fallback(locked_user, using=using)

        def observed_identity_create(*args, **kwargs):
            link_insert_attempted.set()
            return real_identity_create(*args, **kwargs)

        def delete_worker():
            connections.close_all()
            try:
                User.objects.get(pk=user_id).delete()
            except BaseException as exc:
                delete_errors.append(exc)
            finally:
                connections.close_all()

        def link_worker():
            connections.close_all()
            try:
                link_user = User.objects.get(pk=user_id)
                link_apple_identity(
                    link_user,
                    AppleGrant(
                        subject="delete-link-race-subject",
                        issuer="https://appleid.apple.com",
                        audience=SERVICES_ID,
                        email="delete-link-race@example.com",
                        email_verified=True,
                        email_is_relay=False,
                        refresh_token="delete-link-race-refresh",
                    ),
                )
                link_succeeded.append(True)
            except BaseException as exc:
                link_errors.append(exc)
            finally:
                connections.close_all()

        with (
            patch(
                "apps.tenants.apple_services.write_apple_revocation_outbox_fallback",
                side_effect=paused_fallback,
            ),
            patch(
                "apps.tenants.apple_services.ExternalIdentity.objects.create",
                side_effect=observed_identity_create,
            ),
        ):
            deleter = threading.Thread(target=delete_worker)
            deleter.start()
            self.assertTrue(delete_has_user_lock.wait(5))
            linker = threading.Thread(target=link_worker)
            linker.start()
            try:
                self.assertTrue(link_insert_attempted.wait(5))
            finally:
                release_delete.set()
            deleter.join(5)
            linker.join(5)

        self.assertFalse(deleter.is_alive())
        self.assertFalse(linker.is_alive())
        self.assertEqual(delete_errors, [])
        self.assertFalse(User.objects.filter(id=user_id).exists())
        self.assertFalse(ExternalIdentity.objects.filter(user_id=user_id).exists())
        if link_succeeded:
            outbox = AppleRevocationOutbox.objects.get(
                subject="delete-link-race-subject",
            )
            self.assertEqual(
                decrypt_apple_refresh_token(outbox.token_ciphertext),
                "delete-link-race-refresh",
            )
        else:
            self.assertEqual(len(link_errors), 1)
            self.assertIsInstance(
                link_errors[0],
                (AppleResolutionRejected, IntegrityError),
            )

    def test_hard_delete_rejects_outer_atomic_composition(self):
        user = User.objects.create_user(
            username="atomic-hard-delete",
            email="atomic-hard-delete@example.com",
        )
        with (
            transaction.atomic(),
            self.assertRaisesRegex(
                RuntimeError,
                "outside transaction.atomic",
            ),
        ):
            _do_hard_delete(user)
        self.assertTrue(User.objects.filter(id=user.id).exists())

    def test_tenant_hibernation_always_runs_after_delete_commit(self):
        user = User.objects.create_user(
            username="tenant-on-commit",
            email="tenant-on-commit@example.com",
        )
        tenant = Tenant.objects.create(user=user, container_id="oc-on-commit")
        observed = []

        def hibernate(container_id):
            observed.append((container_id, connection.in_atomic_block))

        with (
            patch(
                "apps.orchestrator.azure_client.hibernate_container_app",
                side_effect=hibernate,
            ),
            transaction.atomic(),
        ):
            tenant.delete()
            self.assertEqual(observed, [])

        self.assertEqual(observed, [("oc-on-commit", False)])

    def test_admin_delete_model_and_queryset_route_each_user_through_helper(self):
        actor = User.objects.create_superuser(
            username="admin-actor",
            email="admin-actor@example.com",
            password="AdminPassword123!",
        )
        user_one = User.objects.create_user(username="admin-one", email="one@example.com")
        user_two = User.objects.create_user(username="admin-two", email="two@example.com")
        model_admin = UserAdmin(User, admin.AdminSite())
        request = RequestFactory().post("/admin/tenants/user/")
        request.user = actor

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
            client_id=SERVICES_ID,
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
        with patch(
            "apps.tenants.apple_client.httpx.post",
            return_value=FakeResponse(400, {"error": "invalid_client"}),
        ):
            result = revoke_apple_token_task(str(row.id))
        self.assertEqual(result, {"status": "retry", "reason": "revoke_invalid_client"})
        row.refresh_from_db()
        self.assertIsNone(row.revoked_at)
        self.assertEqual(row.attempts, 1)
        self.assertEqual(row.last_error, "retry:revoke_invalid_client")
        self.assertEqual(row.consecutive_invalid_client, 1)
        self.assertGreater(row.next_attempt_at, row.last_attempt_at)

    def test_transport_failure_records_error_and_backoff(self):
        row = self.make_outbox()
        with (
            patch(
                "apps.tenants.apple_client.httpx.post",
                side_effect=httpx.ReadTimeout("network down"),
            ),
            self.assertLogs("apps.cron.tasks", level="WARNING") as logs,
        ):
            result = revoke_apple_token_task(str(row.id))
        self.assertEqual(result, {"status": "retry", "reason": "revoke_request_failed"})
        row.refresh_from_db()
        self.assertEqual(row.attempts, 1)
        self.assertEqual(row.last_error, "retry:revoke_request_failed")
        self.assertIsNone(row.revoked_at)
        self.assertIsNotNone(row.next_attempt_at)
        combined = "\n".join(logs.output)
        self.assertIn("auth.apple.revocation.retry", combined)
        self.assertNotIn("handler-refresh", combined)

    def test_older_concurrent_failure_cannot_overwrite_newer_attempt_error(self):
        row = self.make_outbox()

        def older_delivery(_refresh_token, *, client_id):
            AppleRevocationOutbox.objects.filter(id=row.id).update(
                claimed_at=timezone.now() + timedelta(seconds=1),
                last_error="retry:newer_failure",
            )
            raise AppleUnavailable("older_failure")

        with patch(
            "apps.tenants.apple_client.revoke_apple_refresh_token",
            side_effect=older_delivery,
        ):
            result = revoke_apple_token_task(str(row.id))

        self.assertEqual(result, {"status": "stale"})
        row.refresh_from_db()
        self.assertEqual(row.attempts, 0)
        self.assertEqual(row.last_error, "retry:newer_failure")

    def test_error_cas_never_regresses_terminal_state(self):
        row = self.make_outbox()
        AppleRevocationOutbox.objects.filter(id=row.id).update(
            attempts=3,
            last_error="terminal:decrypt_failed",
        )

        stale_updated = _record_apple_revocation_error(
            row.id,
            2,
            "retry:stale_attempt",
        )
        terminal_updated = _record_apple_revocation_error(
            row.id,
            3,
            "retry:same_attempt_after_terminal",
        )

        self.assertEqual(stale_updated, 0)
        self.assertEqual(terminal_updated, 0)
        row.refresh_from_db()
        self.assertEqual(row.last_error, "terminal:decrypt_failed")

    def test_corrupt_or_missing_old_key_ciphertext_becomes_terminal_after_three_attempts(self):
        old_key = Fernet.generate_key().decode()
        with self.settings(APPLE_SIWA_TOKEN_ENC_KEYS=[old_key]):
            ciphertext = encrypt_apple_refresh_token("old-key-token")
        row = AppleRevocationOutbox.objects.create(
            token_ciphertext=ciphertext,
            subject="old-key-subject",
            client_id=SERVICES_ID,
        )

        for _ in range(2):
            self.assertEqual(
                revoke_apple_token_task(str(row.id)),
                {"status": "retry", "reason": "decrypt_failed"},
            )
            AppleRevocationOutbox.objects.filter(id=row.id).update(next_attempt_at=None)
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
            client_id=SERVICES_ID,
        )
        for _ in range(2):
            self.assertEqual(
                revoke_apple_token_task(str(row.id)),
                {"status": "retry", "reason": "decrypt_failed"},
            )
            AppleRevocationOutbox.objects.filter(id=row.id).update(next_attempt_at=None)
        result = revoke_apple_token_task(str(row.id))
        self.assertEqual(result, {"status": "terminal", "reason": "decrypt_failed"})
        row.refresh_from_db()
        self.assertEqual(row.attempts, 3)
        self.assertEqual(row.last_error, "terminal:decrypt_failed")

    def test_fresh_lease_is_skipped_and_ten_minute_lease_is_reclaimed(self):
        row = self.make_outbox()
        AppleRevocationOutbox.objects.filter(id=row.id).update(claimed_at=timezone.now())
        with patch("apps.tenants.apple_client.httpx.post") as apple_post:
            self.assertEqual(revoke_apple_token_task(str(row.id)), {"status": "deferred"})
        apple_post.assert_not_called()

        AppleRevocationOutbox.objects.filter(id=row.id).update(
            claimed_at=timezone.now() - timedelta(minutes=11),
        )
        with patch(
            "apps.tenants.apple_client.httpx.post",
            return_value=FakeResponse(200, {}),
        ):
            result = revoke_apple_token_task(str(row.id))
        self.assertEqual(result["status"], "revoked")

    def test_multifernet_decrypts_with_old_rotation_key(self):
        old_key = Fernet.generate_key().decode()
        new_key = Fernet.generate_key().decode()
        with self.settings(APPLE_SIWA_TOKEN_ENC_KEYS=[old_key]):
            ciphertext = encrypt_apple_refresh_token("rotated-old-token")
        row = AppleRevocationOutbox.objects.create(
            token_ciphertext=ciphertext,
            subject="rotation-subject",
            client_id=SERVICES_ID,
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

    def test_invalid_keyring_claims_nothing(self):
        row = self.make_outbox()
        with (
            self.settings(APPLE_SIWA_TOKEN_ENC_KEYS=["invalid-key"]),
            patch(
                "apps.tenants.apple_models.AppleRevocationOutbox.objects.select_for_update",
            ) as select_for_update,
            self.assertLogs("apps.cron.tasks", level="ERROR"),
        ):
            result = process_apple_revocation_outbox((row.id,))
        self.assertEqual(result, [])
        select_for_update.assert_not_called()
        row.refresh_from_db()
        self.assertIsNone(row.claimed_at)
        self.assertEqual(row.attempts, 0)

    def test_batch_claim_is_capped_at_ten(self):
        rows = [self.make_outbox(token=f"batch-token-{index}") for index in range(12)]
        with patch(
            "apps.tenants.apple_client.revoke_apple_refresh_token",
            return_value=200,
        ) as revoke:
            results = process_apple_revocation_outbox()
        self.assertEqual(len(results), 10)
        self.assertEqual(revoke.call_count, 10)
        self.assertEqual(
            AppleRevocationOutbox.objects.filter(revoked_at__isnull=False).count(),
            10,
        )
        self.assertEqual(
            AppleRevocationOutbox.objects.filter(id__in=[row.id for row in rows], revoked_at__isnull=True).count(),
            2,
        )

    def test_backoff_is_monotonic_and_invalid_client_streak_terminalizes_then_recovers(self):
        row = self.make_outbox()
        with (
            patch("apps.cron.tasks.random.uniform", return_value=0),
            patch(
                "apps.tenants.apple_client.revoke_apple_refresh_token",
                side_effect=AppleUnavailable("revoke_request_failed"),
            ),
        ):
            first = revoke_apple_token_task(str(row.id))
            row.refresh_from_db()
            first_delay = (row.next_attempt_at - row.last_attempt_at).total_seconds()
            AppleRevocationOutbox.objects.filter(id=row.id).update(next_attempt_at=None)
            second = revoke_apple_token_task(str(row.id))
            row.refresh_from_db()
            second_delay = (row.next_attempt_at - row.last_attempt_at).total_seconds()
        self.assertEqual(first["status"], "retry")
        self.assertEqual(second["status"], "retry")
        self.assertEqual((first_delay, second_delay), (60, 120))

        for attempt in range(5):
            AppleRevocationOutbox.objects.filter(id=row.id).update(next_attempt_at=None)
            with patch(
                "apps.tenants.apple_client.revoke_apple_refresh_token",
                side_effect=AppleUnavailable("revoke_invalid_client"),
            ):
                if attempt == 4:
                    with self.assertLogs("apps.cron.tasks", level="WARNING") as logs:
                        result = revoke_apple_token_task(str(row.id))
                else:
                    result = revoke_apple_token_task(str(row.id))
        self.assertEqual(result, {"status": "terminal", "reason": "invalid_client"})
        self.assertIn("auth.apple.revocation.terminal", "\n".join(logs.output))
        row.refresh_from_db()
        self.assertEqual(row.consecutive_invalid_client, 5)
        self.assertEqual(row.last_error, "terminal:invalid_client")
        self.assertIsNone(row.revoked_at)

        AppleRevocationOutbox.objects.filter(id=row.id).update(
            consecutive_invalid_client=0,
            last_error="",
            next_attempt_at=None,
        )
        with patch(
            "apps.tenants.apple_client.revoke_apple_refresh_token",
            return_value=200,
        ):
            recovered = revoke_apple_token_task(str(row.id))
        self.assertEqual(recovered["status"], "revoked")
        row.refresh_from_db()
        self.assertEqual(row.consecutive_invalid_client, 0)

    def test_non_invalid_client_outcome_resets_persisted_streak(self):
        row = self.make_outbox()
        AppleRevocationOutbox.objects.filter(id=row.id).update(consecutive_invalid_client=3)
        with patch(
            "apps.tenants.apple_client.revoke_apple_refresh_token",
            side_effect=AppleUnavailable("revoke_request_failed"),
        ):
            result = revoke_apple_token_task(str(row.id))
        self.assertEqual(result["status"], "retry")
        row.refresh_from_db()
        self.assertEqual(row.consecutive_invalid_client, 0)

    def test_missing_row_is_idempotent_success(self):
        result = revoke_apple_token_task("00000000-0000-0000-0000-000000000000")
        self.assertEqual(result, {"status": "missing"})

    def test_malformed_outbox_id_returns_missing_before_database_lookup(self):
        with patch(
            "apps.tenants.apple_models.AppleRevocationOutbox.objects.select_for_update",
        ) as select_for_update:
            result = revoke_apple_token_task("not-a-uuid")
        self.assertEqual(result, {"status": "missing"})
        select_for_update.assert_not_called()

    def test_task_is_registered(self):
        self.assertEqual(
            TASK_MAP["revoke_apple_token"],
            "apps.cron.tasks.revoke_apple_token_task",
        )
        self.assertEqual(
            TASK_MAP["process_apple_revocation_outbox"],
            "apps.cron.tasks.republish_apple_revocation_outbox_task",
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

    def test_republish_failure_logs_no_subject_token_or_ciphertext(self):
        raw_token = "republish-secret-refresh"
        ciphertext = encrypt_apple_refresh_token(raw_token)
        row = AppleRevocationOutbox.objects.create(
            token_ciphertext=ciphertext,
            subject="republish-secret-subject",
        )
        AppleRevocationOutbox.objects.filter(id=row.id).update(
            created_at=timezone.now() - timedelta(hours=2),
        )

        with (
            patch(
                "apps.cron.publish.publish_task",
                side_effect=RuntimeError("qstash unavailable"),
            ),
            self.assertLogs(
                "apps.tenants.management.commands.process_apple_revocation_outbox",
                level="WARNING",
            ) as logs,
        ):
            call_command("process_apple_revocation_outbox")

        combined = "\n".join(logs.output)
        for secret in (raw_token, ciphertext, row.subject):
            self.assertNotIn(secret, combined)
        self.assertIn(str(row.id), combined)


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

    def _csrf_token(self, client):
        request = RequestFactory().get("/")
        token = get_token(request)
        client.cookies["csrftoken"] = request.META["CSRF_COOKIE"]
        return token

    def test_confirmed_admin_delete_enforces_csrf_logs_after_success_and_redirects(self):
        administrator = User.objects.create_superuser(
            username="admin",
            email="admin@example.com",
            password="AdminPassword123!",
        )
        target = User.objects.create_user(
            username="admin-delete-target",
            email="target@example.com",
        )
        target_id = str(target.id)
        target_repr = str(target)
        Tenant.objects.create(user=target, container_id="oc-admin-delete")
        client = Client(enforce_csrf_checks=True)
        client.force_login(administrator)
        delete_url = reverse("admin:tenants_user_delete", args=[target.id])
        csrf_token = self._csrf_token(client)
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
            patch("apps.cron.publish.publish_task"),
        ):
            response = client.post(
                delete_url,
                {
                    "post": "yes",
                    "csrfmiddlewaretoken": csrf_token,
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("admin:tenants_user_changelist"))
        self.assertEqual(observed, [False])
        self.assertEqual(hibernate_observed, [False])
        self.assertFalse(User.objects.filter(id=target_id).exists())
        log = LogEntry.objects.get(
            user=administrator,
            object_id=target_id,
            action_flag=DELETION,
        )
        self.assertEqual(log.object_repr, target_repr)

    def test_confirmed_admin_delete_failure_writes_no_log(self):
        administrator = User.objects.create_superuser(
            username="failure-admin",
            email="failure-admin@example.com",
            password="AdminPassword123!",
        )
        target = User.objects.create_user(
            username="admin-failure-target",
            email="admin-failure-target@example.com",
        )
        target_id = str(target.id)
        client = Client(enforce_csrf_checks=True, raise_request_exception=False)
        client.force_login(administrator)
        delete_url = reverse("admin:tenants_user_delete", args=[target.id])
        csrf_token = self._csrf_token(client)

        with patch(
            "apps.tenants.views._do_hard_delete",
            side_effect=RuntimeError("simulated hard-delete failure"),
        ):
            response = client.post(
                delete_url,
                {
                    "post": "yes",
                    "csrfmiddlewaretoken": csrf_token,
                },
            )

        self.assertEqual(response.status_code, 500)
        self.assertTrue(User.objects.filter(id=target_id).exists())
        self.assertFalse(
            LogEntry.objects.filter(
                user=administrator,
                object_id=target_id,
                action_flag=DELETION,
            ).exists()
        )

    def test_admin_delete_permission_is_enforced_with_csrf_checks_enabled(self):
        staff = User.objects.create_user(
            username="no-delete-staff",
            email="no-delete-staff@example.com",
            password="StaffPassword123!",
            is_staff=True,
        )
        target = User.objects.create_user(
            username="permission-target",
            email="permission-target@example.com",
        )
        target_id = str(target.id)
        client = Client(enforce_csrf_checks=True)
        client.force_login(staff)
        csrf_token = self._csrf_token(client)

        response = client.post(
            reverse("admin:tenants_user_delete", args=[target.id]),
            {
                "post": "yes",
                "csrfmiddlewaretoken": csrf_token,
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertTrue(User.objects.filter(id=target_id).exists())
        self.assertFalse(
            LogEntry.objects.filter(
                user=staff,
                object_id=target_id,
                action_flag=DELETION,
            ).exists()
        )

    def test_admin_cannot_delete_self_or_write_an_orphaned_log(self):
        administrator = User.objects.create_superuser(
            username="self-delete-admin",
            email="self-delete-admin@example.com",
            password="AdminPassword123!",
        )
        administrator_id = str(administrator.id)
        client = Client(enforce_csrf_checks=True)
        client.force_login(administrator)
        csrf_token = self._csrf_token(client)

        response = client.post(
            reverse("admin:tenants_user_delete", args=[administrator.id]),
            {
                "post": "yes",
                "csrfmiddlewaretoken": csrf_token,
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertTrue(User.objects.filter(id=administrator_id).exists())
        self.assertFalse(
            LogEntry.objects.filter(
                user=administrator,
                object_id=administrator_id,
                action_flag=DELETION,
            ).exists()
        )

    def test_bulk_admin_self_delete_is_rejected_before_any_user_is_deleted(self):
        administrator = User.objects.create_superuser(
            username="bulk-self-admin",
            email="bulk-self-admin@example.com",
            password="AdminPassword123!",
        )
        target = User.objects.create_user(
            username="bulk-self-target",
            email="bulk-self-target@example.com",
        )
        model_admin = UserAdmin(User, admin.AdminSite())
        request = RequestFactory().post("/admin/tenants/user/")
        request.user = administrator

        with (
            patch("apps.tenants.views._do_hard_delete") as hard_delete,
            self.assertRaises(PermissionDenied),
        ):
            model_admin.delete_queryset(
                request,
                User.objects.filter(id__in=[administrator.id, target.id]),
            )

        hard_delete.assert_not_called()
        self.assertTrue(User.objects.filter(id=administrator.id).exists())
        self.assertTrue(User.objects.filter(id=target.id).exists())

    def test_bulk_admin_logs_each_user_only_after_its_delete_succeeds(self):
        from . import views

        administrator = User.objects.create_superuser(
            username="bulk-admin",
            email="bulk-admin@example.com",
            password="AdminPassword123!",
        )
        first = User.objects.create_user(
            username="bulk-a-first",
            email="bulk-a-first@example.com",
        )
        second = User.objects.create_user(
            username="bulk-b-second",
            email="bulk-b-second@example.com",
        )
        first_id = str(first.id)
        second_id = str(second.id)
        model_admin = UserAdmin(User, admin.AdminSite())
        request = RequestFactory().post("/admin/tenants/user/")
        request.user = administrator
        real_hard_delete = views._do_hard_delete

        def delete_first_then_fail(user):
            if user.id == second.id:
                raise RuntimeError("second deletion failed")
            return real_hard_delete(user)

        with (
            patch(
                "apps.tenants.views._do_hard_delete",
                side_effect=delete_first_then_fail,
            ),
            self.assertRaisesRegex(RuntimeError, "second deletion failed"),
        ):
            model_admin.delete_queryset(
                request,
                User.objects.filter(id__in=[first.id, second.id]).order_by("username"),
            )

        self.assertFalse(User.objects.filter(id=first_id).exists())
        self.assertTrue(User.objects.filter(id=second_id).exists())
        self.assertTrue(
            LogEntry.objects.filter(
                user=administrator,
                object_id=first_id,
                action_flag=DELETION,
            ).exists()
        )
        self.assertFalse(
            LogEntry.objects.filter(
                user=administrator,
                object_id=second_id,
                action_flag=DELETION,
            ).exists()
        )
