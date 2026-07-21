"""Owner-facing chat-thread administration and its read-time semantics."""

from __future__ import annotations

import secrets
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.common.tenant_tz import tenant_today
from apps.router.conversation_capture import build_conversation_digest
from apps.router.models import (
    AppChatMessage,
    ChatThread,
    ConversationTurn,
    ProactiveOutbound,
)
from apps.router.thread_recap import build_thread_recap_block
from apps.tenants.models import Tenant, User


def _make_user() -> User:
    token = secrets.token_hex(4)
    return User.objects.create_user(
        username=f"thread_admin_{token}",
        email=f"{token}@example.com",
        preferred_channel="telegram",
    )


def _make_tenant(user: User) -> Tenant:
    return Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE,
        container_fqdn="oc-thread-admin.example.com",
    )


class ChatThreadAdminTest(TestCase):
    def setUp(self):
        cache.clear()
        self.user = _make_user()
        self.tenant = _make_tenant(self.user)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.main = ChatThread.objects.create(
            tenant=self.tenant,
            user=self.user,
            title="Home",
            is_main=True,
        )
        self.other = ChatThread.objects.create(
            tenant=self.tenant,
            user=self.user,
            title="Other",
        )

    def _message(
        self,
        thread: ChatThread,
        *,
        client_msg_id: str,
        user_text: str,
        reply_text: str = "reply",
    ) -> AppChatMessage:
        return AppChatMessage.objects.create(
            tenant=self.tenant,
            user=self.user,
            thread=thread,
            client_msg_id=client_msg_id,
            user_text=user_text,
            reply_text=reply_text,
            status=AppChatMessage.Status.READY,
            replied_at=timezone.now(),
        )

    def _set_main(self, thread: ChatThread):
        return self.client.post(f"/api/v1/chat/threads/{thread.id}/set-main/", {}, format="json")

    def test_set_main_changes_default_message_target(self):
        swapped = self._set_main(self.other)
        self.assertEqual(swapped.status_code, 200, swapped.content)
        self.assertEqual(swapped.data["id"], str(self.other.id))
        self.assertTrue(swapped.data["is_main"])

        def persist_without_enqueue(**kwargs):
            return (
                AppChatMessage.objects.create(
                    tenant=kwargs["tenant"],
                    user=kwargs["user"],
                    thread=kwargs["thread"],
                    client_msg_id=kwargs["client_msg_id"],
                    user_text=kwargs["text"],
                ),
                True,
            )

        with patch(
            "apps.router.chat_views.enqueue_tenant_turn",
            side_effect=persist_without_enqueue,
        ):
            sent = self.client.post(
                "/api/v1/chat/messages/",
                {"text": "new home", "client_msg_id": "default-new-main"},
                format="json",
            )

        self.assertEqual(sent.status_code, 201, sent.content)
        self.assertEqual(sent.data["thread_id"], str(self.other.id))
        self.assertEqual(
            AppChatMessage.objects.get(client_msg_id="default-new-main").thread_id,
            self.other.id,
        )
        self.main.refresh_from_db()
        self.other.refresh_from_db()
        self.assertFalse(self.main.is_main)
        self.assertTrue(self.other.is_main)
        self.assertEqual(ChatThread.objects.filter(tenant=self.tenant, is_main=True).count(), 1)

    def test_set_main_relabels_flat_channel_and_proactive_rows_with_new_main(self):
        ConversationTurn.objects.create(
            tenant=self.tenant,
            channel="telegram",
            channel_user_id="123",
            local_date=tenant_today(self.tenant),
            user_text="channel row",
            reply_text="channel reply",
        )
        ProactiveOutbound.objects.create(
            tenant=self.tenant,
            channel="telegram",
            channel_user_id="123",
            message_text="proactive row",
            job_name="Check-in",
        )

        # Prime the cached old id; the swap must invalidate it.
        before = self.client.get("/api/v1/chat/messages/")
        self.assertEqual(before.status_code, 200, before.content)
        self.assertTrue(before.data["messages"])
        self.assertEqual(
            {row["thread_id"] for row in before.data["messages"]},
            {str(self.main.id)},
        )

        swapped = self._set_main(self.other)
        self.assertEqual(swapped.status_code, 200, swapped.content)
        after = self.client.get("/api/v1/chat/messages/")
        self.assertEqual(after.status_code, 200, after.content)
        relevant = [row for row in after.data["messages"] if row["source"] in {"telegram", "cron"}]
        self.assertTrue(relevant)
        self.assertEqual({row["thread_id"] for row in relevant}, {str(self.other.id)})

    def test_set_main_relabels_digest_old_and_new_thread_rows(self):
        self.tenant.digest_thread_attribution_enabled = True
        self.tenant.save(update_fields=["digest_thread_attribution_enabled"])
        self._message(
            self.main,
            client_msg_id="old-main-digest",
            user_text="old home words",
        )
        self._message(
            self.other,
            client_msg_id="new-main-digest",
            user_text="new home words",
        )

        swapped = self._set_main(self.other)
        self.assertEqual(swapped.status_code, 200, swapped.content)
        digest = build_conversation_digest(self.tenant)
        old_line = next(line for line in digest.splitlines() if "old home words" in line)
        new_line = next(line for line in digest.splitlines() if "new home words" in line)
        self.assertIn('[other chat: "Home"]', old_line)
        self.assertNotIn("[other chat:", new_line)

    def test_delete_removes_thread_rows_from_flat_feed_and_digest_source(self):
        self.tenant.digest_thread_attribution_enabled = True
        self.tenant.save(update_fields=["digest_thread_attribution_enabled"])
        message = self._message(
            self.other,
            client_msg_id="delete-from-sources",
            user_text="remove this source row",
        )
        before_feed = self.client.get("/api/v1/chat/messages/")
        self.assertTrue(any(row["id"].startswith(f"app:{message.id}:") for row in before_feed.data["messages"]))
        self.assertIn("remove this source row", build_conversation_digest(self.tenant))

        deleted = self.client.delete(f"/api/v1/chat/threads/{self.other.id}/")
        self.assertEqual(deleted.status_code, 204, deleted.content)

        after_feed = self.client.get("/api/v1/chat/messages/")
        self.assertFalse(any(row["id"].startswith(f"app:{message.id}:") for row in after_feed.data["messages"]))
        self.assertNotIn("remove this source row", build_conversation_digest(self.tenant))

    def test_delete_main_returns_conflict_and_deletes_nothing(self):
        main_message = self._message(
            self.main,
            client_msg_id="keep-main",
            user_text="keep main row",
        )
        other_message = self._message(
            self.other,
            client_msg_id="keep-other",
            user_text="keep other row",
        )

        rejected = self.client.delete(f"/api/v1/chat/threads/{self.main.id}/")

        self.assertEqual(rejected.status_code, 409, rejected.content)
        self.assertEqual(rejected.data, {"error": "cannot_delete_main"})
        self.assertTrue(ChatThread.objects.filter(pk=self.main.pk).exists())
        self.assertTrue(ChatThread.objects.filter(pk=self.other.pk).exists())
        self.assertTrue(AppChatMessage.objects.filter(pk=main_message.pk).exists())
        self.assertTrue(AppChatMessage.objects.filter(pk=other_message.pk).exists())

    def test_delete_cascades_only_its_messages_and_preserves_survivor_recap(self):
        deleted_message = self._message(
            self.other,
            client_msg_id="cascade-deleted",
            user_text="deleted history",
        )
        survivor = ChatThread.objects.create(
            tenant=self.tenant,
            user=self.user,
            title="Survivor",
        )
        survivor_message = self._message(
            survivor,
            client_msg_id="recap-survivor",
            user_text="surviving history",
            reply_text="surviving reply",
        )
        last_reply = survivor_message.replied_at
        self.tenant.last_wake_at = last_reply + timedelta(seconds=1)
        self.tenant.save(update_fields=["last_wake_at"])
        recap_before = build_thread_recap_block(self.tenant, str(survivor.id))
        self.assertIn("surviving history", recap_before)

        deleted = self.client.delete(f"/api/v1/chat/threads/{self.other.id}/")

        self.assertEqual(deleted.status_code, 204, deleted.content)
        self.assertFalse(ChatThread.objects.filter(pk=self.other.pk).exists())
        self.assertFalse(AppChatMessage.objects.filter(pk=deleted_message.pk).exists())
        self.assertTrue(ChatThread.objects.filter(pk=self.main.pk).exists())
        self.assertTrue(ChatThread.objects.filter(pk=survivor.pk).exists())
        self.assertTrue(AppChatMessage.objects.filter(pk=survivor_message.pk).exists())
        self.assertEqual(
            build_thread_recap_block(self.tenant, str(survivor.id)),
            recap_before,
        )

    def test_set_main_is_idempotent_and_admin_endpoints_are_tenant_scoped(self):
        idempotent = self._set_main(self.main)
        self.assertEqual(idempotent.status_code, 200, idempotent.content)
        self.assertEqual(idempotent.data["id"], str(self.main.id))
        self.assertTrue(idempotent.data["is_main"])
        self.assertEqual(ChatThread.objects.filter(tenant=self.tenant, is_main=True).count(), 1)

        foreign_user = _make_user()
        foreign_tenant = _make_tenant(foreign_user)
        foreign_thread = ChatThread.objects.create(
            tenant=foreign_tenant,
            user=foreign_user,
            title="Foreign",
            is_main=True,
        )
        unknown = uuid.uuid4()
        for thread_id in (foreign_thread.id, unknown):
            deleted = self.client.delete(f"/api/v1/chat/threads/{thread_id}/")
            swapped = self.client.post(
                f"/api/v1/chat/threads/{thread_id}/set-main/",
                {},
                format="json",
            )
            self.assertEqual(deleted.status_code, 404, deleted.content)
            self.assertEqual(deleted.data, {"error": "thread_not_found"})
            self.assertEqual(swapped.status_code, 404, swapped.content)
            self.assertEqual(swapped.data, {"error": "thread_not_found"})
        self.assertTrue(ChatThread.objects.filter(pk=foreign_thread.pk).exists())

    def test_fresh_home_create_then_set_main_composition_is_working(self):
        created = self.client.post(
            "/api/v1/chat/threads/",
            {"title": "Fresh Home"},
            format="json",
        )
        self.assertEqual(created.status_code, 201, created.content)
        fresh = ChatThread.objects.get(pk=created.data["id"])

        swapped = self._set_main(fresh)
        self.assertEqual(swapped.status_code, 200, swapped.content)

        def persist_without_enqueue(**kwargs):
            return (
                AppChatMessage.objects.create(
                    tenant=kwargs["tenant"],
                    user=kwargs["user"],
                    thread=kwargs["thread"],
                    client_msg_id=kwargs["client_msg_id"],
                    user_text=kwargs["text"],
                ),
                True,
            )

        with patch(
            "apps.router.chat_views.enqueue_tenant_turn",
            side_effect=persist_without_enqueue,
        ):
            sent = self.client.post(
                "/api/v1/chat/messages/",
                {"text": "hello fresh home", "client_msg_id": "fresh-home-turn"},
                format="json",
            )

        self.assertEqual(sent.status_code, 201, sent.content)
        self.assertEqual(sent.data["thread_id"], str(fresh.id))
        self.assertEqual(
            AppChatMessage.objects.get(client_msg_id="fresh-home-turn").thread_id,
            fresh.id,
        )
        self.assertEqual(ChatThread.objects.filter(tenant=self.tenant, is_main=True).count(), 1)

    def test_admin_endpoints_require_authentication(self):
        anonymous = APIClient()
        deleted = anonymous.delete(f"/api/v1/chat/threads/{self.other.id}/")
        swapped = anonymous.post(
            f"/api/v1/chat/threads/{self.other.id}/set-main/",
            {},
            format="json",
        )
        self.assertIn(deleted.status_code, (401, 403))
        self.assertIn(swapped.status_code, (401, 403))
