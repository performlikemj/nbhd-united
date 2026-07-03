"""Tests for the server-authoritative unread badge primitives:

* ``POST /api/v1/chat/read/`` stamps the per-user read cursor and returns 0.
* ``_compute_unread_count`` — READY replies after the cursor + proactive/cron
  pushes after it, with the exactly-at-timestamp boundary (strictly greater),
  the empty-reply / non-terminal exclusions, and the never-read recent-window
  cap.
"""

from __future__ import annotations

import secrets
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.router.models import AppChatMessage, ChatThread, ProactiveOutbound
from apps.router.push_views import _UNREAD_WINDOW, _compute_unread_count
from apps.tenants.models import Tenant, User


def _make_user() -> User:
    return User.objects.create_user(
        username=f"read_{secrets.token_hex(4)}",
        email=f"{secrets.token_hex(4)}@example.com",
    )


def _make_tenant(user: User) -> Tenant:
    return Tenant.objects.create(user=user, status=Tenant.Status.ACTIVE, container_fqdn="oc-read.example.com")


class ChatReadEndpointTest(TestCase):
    def setUp(self):
        self.user = _make_user()
        self.tenant = _make_tenant(self.user)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_requires_auth(self):
        resp = APIClient().post("/api/v1/chat/read/", {}, format="json")
        self.assertIn(resp.status_code, (401, 403))

    def test_stamps_cursor_and_returns_zero(self):
        self.assertIsNone(self.user.chat_last_read_at)
        before = timezone.now()
        resp = self.client.post("/api/v1/chat/read/", {}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json(), {"unread": 0})
        self.user.refresh_from_db()
        self.assertIsNotNone(self.user.chat_last_read_at)
        self.assertGreaterEqual(self.user.chat_last_read_at, before)

    def test_stamp_advances_on_repeat(self):
        self.client.post("/api/v1/chat/read/", {}, format="json")
        self.user.refresh_from_db()
        first = self.user.chat_last_read_at
        self.client.post("/api/v1/chat/read/", {}, format="json")
        self.user.refresh_from_db()
        self.assertGreaterEqual(self.user.chat_last_read_at, first)


class UnreadCountTest(TestCase):
    def setUp(self):
        self.user = _make_user()
        self.tenant = _make_tenant(self.user)
        self.thread = ChatThread.objects.create(tenant=self.tenant, user=self.user, is_main=True, title="Main")

    def _reply(self, *, replied_at, reply_text="an answer", status=AppChatMessage.Status.READY):
        return AppChatMessage.objects.create(
            tenant=self.tenant,
            user=self.user,
            thread=self.thread,
            client_msg_id=secrets.token_hex(6),
            user_text="q",
            reply_text=reply_text,
            status=status,
            replied_at=replied_at,
        )

    def _set_cursor(self, when):
        User.objects.filter(pk=self.user.pk).update(chat_last_read_at=when)
        self.user.refresh_from_db()

    def test_counts_ready_replies_after_cursor_only(self):
        now = timezone.now()
        self._set_cursor(now - timedelta(hours=1))
        self._reply(replied_at=now - timedelta(minutes=30))  # after cursor → counted
        self._reply(replied_at=now - timedelta(hours=2))  # before cursor → not
        self.assertEqual(_compute_unread_count(self.user), 1)

    def test_boundary_exactly_at_cursor_is_not_counted(self):
        now = timezone.now()
        cursor = now - timedelta(minutes=10)
        self._set_cursor(cursor)
        self._reply(replied_at=cursor)  # exactly-at → strictly-greater excludes it
        self.assertEqual(_compute_unread_count(self.user), 0)
        self._reply(replied_at=cursor + timedelta(seconds=1))  # just after → counted
        self.assertEqual(_compute_unread_count(self.user), 1)

    def test_excludes_empty_reply_and_non_terminal(self):
        now = timezone.now()
        self._set_cursor(now - timedelta(hours=1))
        self._reply(replied_at=now, reply_text="")  # empty reply → not a readable unread
        self._reply(replied_at=now, status=AppChatMessage.Status.PENDING)  # not ready
        self._reply(replied_at=now, status=AppChatMessage.Status.ERROR, reply_text="")  # errored
        self.assertEqual(_compute_unread_count(self.user), 0)

    def test_counts_proactive_pushed_after_cursor(self):
        now = timezone.now()
        self._set_cursor(now - timedelta(hours=1))
        # A proactive row that fired an APNs push after the cursor → unread.
        ProactiveOutbound.objects.create(
            tenant=self.tenant,
            channel=ProactiveOutbound.Channel.APP,
            channel_user_id="",
            message_text="check-in",
            notified_at=now - timedelta(minutes=5),
        )
        # A proactive row that never pushed (notified_at NULL) is NOT user-visible.
        ProactiveOutbound.objects.create(
            tenant=self.tenant,
            channel=ProactiveOutbound.Channel.APP,
            channel_user_id="",
            message_text="silent",
            notified_at=None,
        )
        # Pushed, but before the cursor → already seen.
        ProactiveOutbound.objects.create(
            tenant=self.tenant,
            channel=ProactiveOutbound.Channel.APP,
            channel_user_id="",
            message_text="old",
            notified_at=now - timedelta(hours=2),
        )
        self.assertEqual(_compute_unread_count(self.user), 1)

    def test_replies_and_proactive_sum(self):
        now = timezone.now()
        self._set_cursor(now - timedelta(hours=1))
        self._reply(replied_at=now - timedelta(minutes=20))
        self._reply(replied_at=now - timedelta(minutes=10))
        ProactiveOutbound.objects.create(
            tenant=self.tenant,
            channel=ProactiveOutbound.Channel.APP,
            channel_user_id="",
            message_text="check-in",
            notified_at=now - timedelta(minutes=5),
        )
        self.assertEqual(_compute_unread_count(self.user), 3)

    def test_never_read_counts_only_recent_window(self):
        # No read cursor at all → count from the window floor, not from epoch, so
        # ancient history can't balloon the badge.
        self.assertIsNone(self.user.chat_last_read_at)
        now = timezone.now()
        self._reply(replied_at=now - _UNREAD_WINDOW - timedelta(days=1))  # outside window
        self._reply(replied_at=now - timedelta(days=1))  # inside window → counted
        self.assertEqual(_compute_unread_count(self.user), 1)
