"""PR5 behavioral tests — friend chat 1:1 (poll-is-truth + backstage absorb).

Chat stores RAW human text (consent by typing, design §4.6); the sensitive
boundary is agent absorption, which redacts fresh in the recipient's session and
keeps raw friend text OUT of USER.md (envelope shows a neutral pointer only).
"""

from __future__ import annotations

from unittest import mock

from django.db import IntegrityError, ProgrammingError
from django.test import TestCase
from django.utils import timezone
from psycopg.errors import InsufficientPrivilege
from rest_framework.test import APIClient

from apps.router.models import DeviceToken
from apps.tenants.models import Tenant, User

from . import access, envelope, feed, services
from .models import AbsorbedItem, FriendMessage, Friendship, FriendThreadMembership, NeighborProfile


def _tenant(username, *, friends_enabled=True, status="active"):
    user = User.objects.create_user(username=username, password="pass", display_name=username.title())
    return Tenant.objects.create(user=user, status=status, friends_enabled=friends_enabled)


def _profile(tenant, handle):
    return NeighborProfile.objects.create(tenant=tenant, handle=handle, display_name=handle.title())


def _edge(a, b, status=Friendship.Status.ACCEPTED):
    return Friendship.objects.create(requester=a, addressee=b, status=status)


def _client(user):
    c = APIClient()
    c.force_authenticate(user=user)
    return c


class ThreadOpenTest(TestCase):
    def setUp(self):
        self.a = _tenant("th_a")
        self.b = _tenant("th_b")
        self.edge = _edge(self.a, self.b)

    def test_open_creates_thread_and_both_memberships(self):
        thread = services.open_thread(self.a, str(self.edge.id))
        self.assertEqual(FriendThreadMembership.objects.filter(thread=thread, left_at__isnull=True).count(), 2)
        # Idempotent (uq_direct_thread).
        thread2 = services.open_thread(self.b, str(self.edge.id))
        self.assertEqual(thread.id, thread2.id)

    def test_non_neighbor_cannot_open(self):
        stranger = _tenant("th_stranger")
        edge = _edge(self.a, stranger, status=Friendship.Status.PENDING)
        resp = _client(self.a.user).post("/api/v1/friends/threads/", {"friendship_id": str(edge.id)}, format="json")
        self.assertEqual(resp.status_code, 403)


class SendAndFeedTest(TestCase):
    def setUp(self):
        self.a = _tenant("snd_a")
        self.b = _tenant("snd_b")
        _profile(self.a, "aaa")
        _profile(self.b, "bbb")
        self.edge = _edge(self.a, self.b)
        self.thread = services.open_thread(self.a, str(self.edge.id))

    def _send(self, sender, text, cid):
        with mock.patch("apps.friends.services._notify_friend_message"):
            return services.send_friend_message(sender, sender.user, str(self.thread.id), cid, text)

    def test_keyset_ordering_and_cursor_roundtrip(self):
        self._send(self.a, "one", "c1")
        self._send(self.b, "two", "c2")
        self._send(self.a, "three", "c3")
        page1 = services.get_thread_messages(self.a, str(self.thread.id), None, 2)
        self.assertEqual([m["text"] for m in page1["messages"]], ["one", "two"])
        page2 = services.get_thread_messages(self.a, str(self.thread.id), page1["next_cursor"], 2)
        self.assertEqual([m["text"] for m in page2["messages"]], ["three"])
        # Empty tail echoes the cursor (no advance).
        page3 = services.get_thread_messages(self.a, str(self.thread.id), page2["next_cursor"], 2)
        self.assertEqual(page3["messages"], [])
        self.assertEqual(page3["next_cursor"], page2["next_cursor"])

    def test_mine_flag(self):
        self._send(self.a, "hi", "c1")
        page = services.get_thread_messages(self.b, str(self.thread.id), None, 10)
        self.assertFalse(page["messages"][0]["mine"])  # a's message, viewed by b

    def test_client_msg_id_idempotent(self):
        m1, c1 = self._send(self.a, "hi", "dup")
        m2, c2 = self._send(self.a, "hi again", "dup")
        self.assertTrue(c1)
        self.assertFalse(c2)
        self.assertEqual(m1.seq, m2.seq)
        self.assertEqual(FriendMessage.objects.filter(thread=self.thread).count(), 1)

    def test_create_message_retries_rls_privilege_failure_once(self):
        real_create = FriendMessage.objects.create
        rls_error = InsufficientPrivilege('new row violates row-level security policy for table "friend_messages"')
        wrapped_error = ProgrammingError(*rls_error.args)
        wrapped_error.__cause__ = rls_error
        create_calls = 0

        def fail_once(**kwargs):
            nonlocal create_calls
            create_calls += 1
            if create_calls == 1:
                raise wrapped_error
            return real_create(**kwargs)

        with (
            mock.patch.object(FriendMessage.objects, "create", side_effect=fail_once),
            mock.patch("apps.tenants.middleware.set_rls_context") as set_context,
        ):
            message, created = access.create_friend_message(
                self.thread,
                self.a,
                self.a.user,
                "rls-retry",
                "survives a pooled connection",
            )

        self.assertTrue(created)
        self.assertEqual(message.text, "survives a pooled connection")
        self.assertEqual(create_calls, 2)
        self.assertEqual(set_context.call_count, 2)
        set_context.assert_has_calls(
            [
                mock.call(tenant_id=self.a.id, user_id=self.a.user.id),
                mock.call(tenant_id=self.a.id, user_id=self.a.user.id),
            ]
        )

    def test_create_message_second_rls_failure_raises_without_looping(self):
        rls_error = InsufficientPrivilege('new row violates row-level security policy for table "friend_messages"')
        wrapped_error = ProgrammingError(*rls_error.args)
        wrapped_error.__cause__ = rls_error
        with (
            mock.patch.object(FriendMessage.objects, "create", side_effect=wrapped_error) as create,
            mock.patch("apps.tenants.middleware.set_rls_context") as set_context,
            self.assertRaises(ProgrammingError),
        ):
            access.create_friend_message(
                self.thread,
                self.a,
                self.a.user,
                "rls-double-failure",
                "must raise after one retry",
            )

        self.assertEqual(create.call_count, 2)
        self.assertEqual(set_context.call_count, 2)

    def test_create_message_false_miss_refetches_unique_winner(self):
        winner = FriendMessage.objects.create(
            thread=self.thread,
            sender_tenant=self.a,
            sender_user=self.a.user,
            client_msg_id="false-miss",
            text="already persisted",
        )
        with (
            mock.patch.object(
                FriendMessage.objects,
                "get",
                side_effect=[FriendMessage.DoesNotExist, winner],
            ),
            mock.patch.object(
                FriendMessage.objects,
                "create",
                side_effect=IntegrityError("duplicate uq_friend_msg_idem"),
            ),
            mock.patch("apps.tenants.middleware.set_rls_context") as set_context,
        ):
            message, created = access.create_friend_message(
                self.thread,
                self.a,
                self.a.user,
                "false-miss",
                "outbox replay",
            )

        self.assertFalse(created)
        self.assertEqual(message.seq, winner.seq)
        self.assertEqual(set_context.call_count, 2)

    def test_create_message_plain_idempotent_retry_returns_existing(self):
        first, first_created = access.create_friend_message(
            self.thread,
            self.a,
            self.a.user,
            "plain-retry",
            "original",
        )
        second, second_created = access.create_friend_message(
            self.thread,
            self.a,
            self.a.user,
            "plain-retry",
            "ignored replay",
        )

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(second.seq, first.seq)
        self.assertEqual(second.text, "original")

    def test_malformed_cursor_restarts(self):
        self._send(self.a, "one", "c1")
        page = services.get_thread_messages(self.a, str(self.thread.id), "not-a-cursor", 10)
        self.assertEqual(len(page["messages"]), 1)  # lenient restart, not a 4xx

    def test_cursor_encode_decode(self):
        self.assertEqual(feed.decode_cursor(feed.encode_cursor(42)), 42)
        self.assertEqual(feed.decode_cursor(None), 0)
        self.assertEqual(feed.decode_cursor("garbage"), 0)


class ParticipantGateTest(TestCase):
    def setUp(self):
        self.a = _tenant("pg_a")
        self.b = _tenant("pg_b")
        self.c = _tenant("pg_c")  # stranger
        self.edge = _edge(self.a, self.b)
        self.thread = services.open_thread(self.a, str(self.edge.id))

    def test_non_member_read_404(self):
        # §4.5 IDOR: swap in someone else's thread_id → no-reveal 404.
        resp = _client(self.c.user).get(f"/api/v1/friends/threads/{self.thread.id}/messages/")
        self.assertEqual(resp.status_code, 404)

    def test_non_member_send_404(self):
        resp = _client(self.c.user).post(
            f"/api/v1/friends/threads/{self.thread.id}/messages/", {"text": "x", "client_msg_id": "c1"}, format="json"
        )
        self.assertEqual(resp.status_code, 404)

    def test_flag_off_403(self):
        off = _tenant("pg_off", friends_enabled=False)
        resp = _client(off.user).get("/api/v1/friends/threads/")
        self.assertEqual(resp.status_code, 403)


class LifecycleGateTest(TestCase):
    """Blocked/revoked freezes SENDS; history stays readable. Suspended target →
    store-only (control-plane) send OK."""

    def setUp(self):
        self.a = _tenant("lc_a")
        self.b = _tenant("lc_b")
        self.edge = _edge(self.a, self.b)
        self.thread = services.open_thread(self.a, str(self.edge.id))
        with mock.patch("apps.friends.services._notify_friend_message"):
            services.send_friend_message(self.a, self.a.user, str(self.thread.id), "c1", "before")

    def test_blocked_freezes_send_but_history_readable(self):
        Friendship.objects.filter(id=self.edge.id).update(status=Friendship.Status.BLOCKED, blocked_by=self.b)
        # Send frozen.
        resp = _client(self.a.user).post(
            f"/api/v1/friends/threads/{self.thread.id}/messages/",
            {"text": "after", "client_msg_id": "c2"},
            format="json",
        )
        self.assertEqual(resp.status_code, 403)
        # History still readable.
        page = services.get_thread_messages(self.a, str(self.thread.id), None, 10)
        self.assertEqual([m["text"] for m in page["messages"]], ["before"])

    def test_suspended_target_store_only_send_ok(self):
        Tenant.objects.filter(id=self.b.id).update(status=Tenant.Status.SUSPENDED)
        with mock.patch("apps.friends.services._notify_friend_message"):
            message, created = services.send_friend_message(
                self.a, self.a.user, str(self.thread.id), "c3", "still sends"
            )
        self.assertTrue(created)  # stored in the control plane; no container touch to skip


class PushTest(TestCase):
    def setUp(self):
        self.a = _tenant("push_a")
        self.b = _tenant("push_b")
        _profile(self.a, "pusha")
        self.edge = _edge(self.a, self.b)
        self.thread = services.open_thread(self.a, str(self.edge.id))

    def test_one_push_claim(self):
        with mock.patch("apps.friends.services._notify_friend_message"):
            message, _ = services.send_friend_message(self.a, self.a.user, str(self.thread.id), "c1", "hi")
        self.assertTrue(access.claim_message_notified(message))
        self.assertFalse(access.claim_message_notified(message))  # second delivery is a no-op

    def test_muted_stops_push(self):
        from apps.friends.notifications import _deliver_friend_push

        with mock.patch("apps.friends.services._notify_friend_message"):
            message, _ = services.send_friend_message(self.a, self.a.user, str(self.thread.id), "c1", "hi")
        # Mute b's membership.
        FriendThreadMembership.objects.filter(thread=self.thread, tenant=self.b).update(muted=True)
        with (
            mock.patch("apps.common.apns.apns_configured", return_value=True),
            mock.patch("apps.router.push_views._push_to_user_devices") as push,
        ):
            _deliver_friend_push(message)
        push.assert_not_called()

    def test_push_to_unmuted(self):
        from apps.friends.notifications import _deliver_friend_push

        with mock.patch("apps.friends.services._notify_friend_message"):
            message, _ = services.send_friend_message(self.a, self.a.user, str(self.thread.id), "c1", "hi")
        with (
            mock.patch("apps.common.apns.apns_configured", return_value=True),
            mock.patch("apps.router.push_views._push_to_user_devices") as push,
        ):
            _deliver_friend_push(message)
        push.assert_called_once()

    def test_revoked_token_gets_zero_friend_sends(self):
        from apps.friends.notifications import _deliver_friend_push

        with mock.patch("apps.friends.services._notify_friend_message"):
            message, _ = services.send_friend_message(self.a, self.a.user, str(self.thread.id), "c1", "hi")
        DeviceToken.objects.create(
            user=self.b.user,
            tenant=self.b,
            token="a" * 64,
            revoked_at=timezone.now(),
        )

        with (
            mock.patch("apps.common.apns.apns_configured", return_value=True),
            mock.patch("apps.common.apns.send_push") as send_push,
        ):
            _deliver_friend_push(message)

        send_push.assert_not_called()

    def test_inactive_user_gets_zero_friend_sends(self):
        from apps.friends.notifications import _deliver_friend_push

        with mock.patch("apps.friends.services._notify_friend_message"):
            message, _ = services.send_friend_message(self.a, self.a.user, str(self.thread.id), "c1", "hi")
        DeviceToken.objects.create(user=self.b.user, tenant=self.b, token="a" * 64)
        User.objects.filter(pk=self.b.user_id).update(is_active=False)

        with (
            mock.patch("apps.common.apns.apns_configured", return_value=True),
            mock.patch("apps.common.apns.send_push") as send_push,
        ):
            _deliver_friend_push(message)

        send_push.assert_not_called()


class AbsorbTest(TestCase):
    def setUp(self):
        self.a = _tenant("ab_a")  # sender
        self.b = _tenant("ab_b")  # absorber
        _profile(self.a, "sender")
        _profile(self.b, "absorber")
        self.edge = _edge(self.a, self.b)
        self.thread = services.open_thread(self.a, str(self.edge.id))
        with mock.patch("apps.friends.services._notify_friend_message"):
            services.send_friend_message(self.a, self.a.user, str(self.thread.id), "c1", "RAW my name is Kenji")

    def _context(self, tenant):
        # redact_user_message would load the 554MB model — patch it (identity).
        with mock.patch("apps.pii.redactor.redact_user_message", side_effect=lambda text, tenant: f"[red]{text}"):
            return services.neighborhood_context(tenant)

    def test_absorb_returns_redacted_chat_and_advances_cursor(self):
        ctx = self._context(self.b)
        self.assertEqual(len(ctx["chat"]), 1)
        self.assertEqual(ctx["chat"][0]["from_handle"], "sender")
        self.assertTrue(ctx["chat"][0]["messages"][0].startswith("[red]"))  # redacted fresh
        # Cursor advanced → a repeat call re-absorbs nothing.
        ctx2 = self._context(self.b)
        self.assertEqual(ctx2["chat"], [])

    def test_absorb_off_membership_skips(self):
        FriendThreadMembership.objects.filter(thread=self.thread, tenant=self.b).update(agent_absorb_enabled=False)
        ctx = self._context(self.b)
        self.assertEqual(ctx["chat"], [])

    def test_absorb_excludes_own_messages(self):
        # The sender absorbing their OWN thread sees no chat (only other party's).
        ctx = self._context(self.a)
        self.assertEqual(ctx["chat"], [])

    def test_ledger_label_is_neutral_no_message_text(self):
        self._context(self.b)
        item = AbsorbedItem.objects.get(tenant=self.b, source_kind=AbsorbedItem.SourceKind.FRIEND_MESSAGE)
        self.assertEqual(item.label, "Chat with @sender")
        self.assertNotIn("Kenji", item.label)  # pointer, never a message-text copy
        self.assertNotIn("RAW", item.label)

    def test_purge_chat_item_removed_from_ledger(self):
        self._context(self.b)
        item = AbsorbedItem.objects.get(tenant=self.b, source_kind=AbsorbedItem.SourceKind.FRIEND_MESSAGE)
        services.purge_absorbed(self.b, item.id)
        listed = services.list_absorbed(self.b)
        self.assertEqual(listed, [])

    def test_envelope_chat_pointer_has_no_message_text(self):
        out = envelope.render_neighborhood(self.b)
        self.assertIn("new from @sender", out)
        self.assertNotIn("Kenji", out)  # no message text on the share file
        self.assertNotIn("RAW", out)
        # After absorb (cursor advanced), the pointer clears.
        self._context(self.b)
        out2 = envelope.render_neighborhood(self.b)
        self.assertNotIn("new from @sender", out2)
