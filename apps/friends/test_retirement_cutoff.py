"""Regression tests for serializing friend retirement with direct sends."""

from __future__ import annotations

from unittest import mock, skipUnless

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.db import OperationalError, connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.test import APIClient

from apps.tenants.models import Tenant, User

from . import services
from .models import FriendMessage, Friendship, FriendThread, FriendThreadMembership


def _tenant(username: str) -> Tenant:
    user = User.objects.create_user(username=username, password="pass", display_name=username.title())
    return Tenant.objects.create(user=user, status=Tenant.Status.ACTIVE, friends_enabled=True)


def _edge(a: Tenant, b: Tenant) -> Friendship:
    return Friendship.objects.create(requester=a, addressee=b, status=Friendship.Status.ACCEPTED)


def _client(user: User) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=user)
    return client


class RetirementCutoffResponseTest(TestCase):
    def setUp(self):
        self.a = _tenant("retirement_a")
        self.b = _tenant("retirement_b")
        self.edge = _edge(self.a, self.b)
        self.thread = services.open_thread(self.a, self.edge.id)

    def _send(self, sender: Tenant, client_msg_id: str, text: str):
        with mock.patch("apps.friends.services._notify_friend_message"):
            return services.send_friend_message(sender, sender.user, self.thread.id, client_msg_id, text)

    def _accept_next_incarnation_without_messages(self) -> dict:
        services.unfriend_with_retirement(self.a, self.edge.id)
        target_profile = services.ensure_neighbor_profile(self.b, self.b.user)
        with mock.patch("apps.friends.services._notify_wave_received"):
            pending, created = services.send_wave(self.a, self.a.user, target_profile.handle, "reconnect")
        self.assertFalse(created)
        self.assertEqual(pending.status, Friendship.Status.PENDING)
        response = _client(self.b.user).post(f"/api/v1/friends/waves/{self.edge.id}/accept/")
        self.assertEqual(response.status_code, 200)
        return response.json()

    def _assert_additive_shape(self, payload: dict, expected_status: str):
        legacy = {
            "friendship_id": str(self.edge.id),
            "status": expected_status,
        }
        self.assertEqual({key: payload[key] for key in legacy}, legacy)
        self.assertEqual(
            set(payload),
            {
                "friendship_id",
                "status",
                "thread_id",
                "retirement_cutoff_seq",
                "acceptance_incarnation",
            },
        )
        self.edge.refresh_from_db()
        self.assertEqual(payload["acceptance_incarnation"], self.edge.acceptance_incarnation)

    def test_block_response_carries_raw_thread_scoped_max_seq(self):
        first, _created = self._send(self.a, "block-first", "visible")
        last, _created = self._send(self.b, "block-last", "later soft-deleted")
        FriendMessage.objects.filter(seq=last.seq).update(deleted_at=timezone.now())

        # FriendMessage.seq is global. A later message elsewhere must not inflate
        # this friendship's thread-scoped retirement cutoff.
        other_a = _tenant("retirement_other_a")
        other_b = _tenant("retirement_other_b")
        other_edge = _edge(other_a, other_b)
        other_thread = services.open_thread(other_a, other_edge.id)
        with mock.patch("apps.friends.services._notify_friend_message"):
            unrelated, _created = services.send_friend_message(
                other_a,
                other_a.user,
                other_thread.id,
                "unrelated",
                "different thread",
            )

        response = _client(self.a.user).post(f"/api/v1/friends/waves/{self.edge.id}/block/")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self._assert_additive_shape(payload, Friendship.Status.BLOCKED)
        self.assertEqual(payload["thread_id"], str(self.thread.id))
        self.assertEqual(payload["retirement_cutoff_seq"], last.seq)
        self.assertLessEqual(first.seq, payload["retirement_cutoff_seq"])
        self.assertGreater(unrelated.seq, payload["retirement_cutoff_seq"])

    def test_unfriend_response_carries_max_seq(self):
        message, _created = self._send(self.a, "unfriend-before", "before retirement")

        response = _client(self.b.user).delete(f"/api/v1/friends/{self.edge.id}/")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self._assert_additive_shape(payload, Friendship.Status.REVOKED)
        self.assertEqual(payload["thread_id"], str(self.thread.id))
        self.assertEqual(payload["retirement_cutoff_seq"], message.seq)
        self.assertLessEqual(message.seq, payload["retirement_cutoff_seq"])

    def test_no_thread_returns_null_id_and_zero_cutoff(self):
        block_a = _tenant("retirement_no_thread_block_a")
        block_b = _tenant("retirement_no_thread_block_b")
        block_edge = _edge(block_a, block_b)
        block_response = _client(block_a.user).post(f"/api/v1/friends/waves/{block_edge.id}/block/")

        unfriend_a = _tenant("retirement_no_thread_unfriend_a")
        unfriend_b = _tenant("retirement_no_thread_unfriend_b")
        unfriend_edge = _edge(unfriend_a, unfriend_b)
        unfriend_response = _client(unfriend_a.user).delete(f"/api/v1/friends/{unfriend_edge.id}/")

        for response in (block_response, unfriend_response):
            self.assertEqual(response.status_code, 200)
            self.assertIsNone(response.json()["thread_id"])
            self.assertEqual(response.json()["retirement_cutoff_seq"], 0)
            self.assertEqual(response.json()["acceptance_incarnation"], 0)

    def test_accept_persists_pre_accept_boundary_and_threads_keep_exposing_it(self):
        old_message, _created = self._send(self.a, "acceptance-old", "previous incarnation")
        services.unfriend_with_retirement(self.a, self.edge.id)
        target_profile = services.ensure_neighbor_profile(self.b, self.b.user)
        with mock.patch("apps.friends.services._notify_wave_received"):
            pending, created = services.send_wave(
                self.a,
                self.a.user,
                target_profile.handle,
                "reconnect",
            )
        self.assertFalse(created)
        self.assertEqual(pending.status, Friendship.Status.PENDING)

        response = _client(self.b.user).post(f"/api/v1/friends/waves/{self.edge.id}/accept/")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(
            {key: payload[key] for key in ("friendship_id", "status")},
            {"friendship_id": str(self.edge.id), "status": Friendship.Status.ACCEPTED},
        )
        self.assertEqual(payload["acceptance_cutoff_seq"], old_message.seq)
        self.assertEqual(payload["acceptance_incarnation"], 1)
        self.edge.refresh_from_db()
        self.assertEqual(self.edge.acceptance_cutoff_seq, old_message.seq)
        self.assertEqual(self.edge.acceptance_incarnation, 1)

        new_message, _created = self._send(self.a, "acceptance-new", "current incarnation")
        self.assertGreater(new_message.seq, self.edge.acceptance_cutoff_seq)

        client = _client(self.b.user)
        opened = client.post("/api/v1/friends/threads/", {"friendship_id": str(self.edge.id)}, format="json")
        listed = client.get("/api/v1/friends/threads/")

        self.assertEqual(opened.status_code, 200)
        self.assertEqual(opened.json()["acceptance_cutoff_seq"], old_message.seq)
        self.assertEqual(opened.json()["acceptance_incarnation"], 1)
        self.assertEqual(listed.status_code, 200)
        listed_thread = next(item for item in listed.json() if item["thread_id"] == str(self.thread.id))
        self.assertEqual(listed_thread["acceptance_cutoff_seq"], old_message.seq)
        self.assertEqual(listed_thread["acceptance_incarnation"], 1)

    def test_mutual_wave_accept_persists_and_returns_pre_accept_boundary(self):
        old_message, _created = self._send(self.a, "mutual-old", "previous incarnation")
        services.unfriend_with_retirement(self.a, self.edge.id)
        requester_profile = services.ensure_neighbor_profile(self.a, self.a.user)
        addressee_profile = services.ensure_neighbor_profile(self.b, self.b.user)
        with mock.patch("apps.friends.services._notify_wave_received"):
            services.send_wave(self.a, self.a.user, addressee_profile.handle, "reconnect")

        response = _client(self.b.user).post(
            "/api/v1/friends/waves/",
            {"handle": requester_profile.handle},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], Friendship.Status.ACCEPTED)
        self.assertEqual(response.json()["acceptance_cutoff_seq"], old_message.seq)
        self.assertEqual(response.json()["acceptance_incarnation"], 1)
        self.edge.refresh_from_db()
        self.assertEqual(self.edge.acceptance_cutoff_seq, old_message.seq)
        self.assertEqual(self.edge.acceptance_incarnation, 1)

    def test_invite_accept_persists_and_returns_pre_accept_boundary(self):
        old_message, _created = self._send(self.a, "invite-old", "previous incarnation")
        services.unfriend_with_retirement(self.a, self.edge.id)
        invite = services.create_invite(self.a)

        response = _client(self.b.user).post(f"/api/v1/friends/invites/{invite.token}/claim/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], Friendship.Status.ACCEPTED)
        self.assertEqual(response.json()["acceptance_cutoff_seq"], old_message.seq)
        self.assertEqual(response.json()["acceptance_incarnation"], 1)
        self.edge.refresh_from_db()
        self.assertEqual(self.edge.acceptance_cutoff_seq, old_message.seq)
        self.assertEqual(self.edge.acceptance_incarnation, 1)

    def test_fresh_invite_accept_starts_first_incarnation(self):
        inviter = _tenant("incarnation_inviter")
        claimer = _tenant("incarnation_claimer")
        invite = services.create_invite(inviter)

        response = _client(claimer.user).post(f"/api/v1/friends/invites/{invite.token}/claim/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["acceptance_cutoff_seq"], 0)
        self.assertEqual(response.json()["acceptance_incarnation"], 1)
        edge = Friendship.objects.get(invite=invite)
        self.assertEqual(edge.acceptance_cutoff_seq, 0)
        self.assertEqual(edge.acceptance_incarnation, 1)

        idempotent_invite = services.create_invite(inviter)
        repeated = _client(claimer.user).post(f"/api/v1/friends/invites/{idempotent_invite.token}/claim/")
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(repeated.json()["acceptance_incarnation"], 1)
        edge.refresh_from_db()
        self.assertEqual(edge.acceptance_incarnation, 1)

    def test_message_free_reaccept_has_equal_cutoff_and_higher_incarnation(self):
        seed, _created = self._send(self.a, "incarnation-seed", "before both incarnations")
        first = self._accept_next_incarnation_without_messages()
        second = self._accept_next_incarnation_without_messages()

        self.assertEqual(first["acceptance_cutoff_seq"], second["acceptance_cutoff_seq"])
        self.assertEqual(second["acceptance_cutoff_seq"], seed.seq)
        self.assertGreater(second["acceptance_incarnation"], first["acceptance_incarnation"])
        self.assertEqual(second["acceptance_incarnation"], first["acceptance_incarnation"] + 1)

        retirement = _client(self.a.user).delete(f"/api/v1/friends/{self.edge.id}/")
        self.assertEqual(retirement.status_code, 200)
        self.assertEqual(retirement.json()["acceptance_incarnation"], second["acceptance_incarnation"])


@skipUnless(connection.vendor == "postgresql", "PostgreSQL row locks are required")
@override_settings(NBHD_DISABLE_BACKGROUND_THREADS=True)
class RetirementLockContentionTest(TransactionTestCase):
    """Use two DB connections without worker threads to pin real row contention."""

    def setUp(self):
        self.user_md_patcher = mock.patch(
            "apps.orchestrator.workspace_envelope.push_user_md",
            return_value=True,
        )
        self.user_md_patcher.start()
        self.addCleanup(self.user_md_patcher.stop)
        self.a = _tenant("retirement_lock_a")
        self.b = _tenant("retirement_lock_b")
        self.edge = _edge(self.a, self.b)
        self.thread = services.open_thread(self.a, self.edge.id)

    def _send(self, client_msg_id: str, text: str = "serialized"):
        with mock.patch("apps.friends.services._notify_friend_message"):
            return services.send_friend_message(
                self.a,
                self.a.user,
                self.thread.id,
                client_msg_id,
                text,
            )

    def _assert_waits_on_edge_lock(self, operation, *, edge=None):
        """Require ``operation`` to contend on the friendship choke-point row."""
        edge = edge or self.edge
        locker = connection.copy()
        try:
            locker.connect()
            locker.set_autocommit(False)
            with locker.cursor() as cursor:
                cursor.execute(
                    "SELECT id FROM friendships WHERE id = %s FOR UPDATE",
                    [edge.id],
                )

            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout TO '250ms'")
            try:
                with self.assertRaises(OperationalError):
                    operation()
            finally:
                with connection.cursor() as cursor:
                    cursor.execute("RESET lock_timeout")
        finally:
            locker.rollback()
            locker.close()

    def _prepare_revoked_incarnation(self, client_msg_id: str):
        old_message, _created = self._send(client_msg_id, "previous incarnation")
        services.unfriend_with_retirement(self.a, self.edge.id)
        services.ensure_neighbor_profile(self.a, self.a.user)
        target_profile = services.ensure_neighbor_profile(self.b, self.b.user)
        self.edge.refresh_from_db()
        self.assertEqual(self.edge.status, Friendship.Status.REVOKED)
        return old_message, target_profile.handle, self.edge.acceptance_cutoff_seq

    def _rewave(self, target_handle: str):
        with mock.patch("apps.friends.services._notify_wave_received"):
            return services.send_wave(self.a, self.a.user, target_handle, "again")

    def _prepare_pending_incarnation(self, client_msg_id: str):
        old_message, target_handle, previous_boundary = self._prepare_revoked_incarnation(client_msg_id)
        pending, created = self._rewave(target_handle)
        self.assertFalse(created)
        self.assertEqual(pending.status, Friendship.Status.PENDING)
        return old_message, previous_boundary

    def test_direct_send_waits_on_friendship_lock_before_insert(self):
        self._assert_waits_on_edge_lock(lambda: self._send("pre-block-send", "must wait for the relationship lock"))

        self.assertFalse(FriendMessage.objects.filter(client_msg_id="pre-block-send").exists())

        _blocked_edge, retirement = services.block_friendship(self.b, self.edge.id)
        with self.assertRaises(PermissionDenied):
            self._send("pre-block-send", "cannot resume after block")

        self.edge.refresh_from_db()
        self.assertEqual(self.edge.status, Friendship.Status.BLOCKED)
        self.assertFalse(FriendMessage.objects.filter(client_msg_id="pre-block-send").exists())
        self.assertFalse(
            FriendMessage.objects.filter(seq__gt=retirement["retirement_cutoff_seq"])
            .filter(client_msg_id="pre-block-send")
            .exists()
        )

    def test_block_waits_on_send_lock_then_cutoff_includes_send_winner(self):
        self._assert_waits_on_edge_lock(lambda: services.block_friendship(self.b, self.edge.id))
        self.edge.refresh_from_db()
        self.assertEqual(self.edge.status, Friendship.Status.ACCEPTED)

        message, created = self._send("send-wins", "commits before block")
        self.assertTrue(created)
        _blocked_edge, retirement = services.block_friendship(self.b, self.edge.id)

        self.assertLessEqual(message.seq, retirement["retirement_cutoff_seq"])
        self.assertFalse(
            FriendMessage.objects.filter(
                client_msg_id="send-wins",
                seq__gt=retirement["retirement_cutoff_seq"],
            ).exists()
        )

    def test_explicit_accept_waits_on_block_lock_and_cannot_overwrite_block(self):
        old_message, previous_boundary = self._prepare_pending_incarnation("accept-block-old")

        self._assert_waits_on_edge_lock(
            lambda: services.respond_to_wave(self.b, self.edge.id, "accept"),
        )
        _blocked_edge, retirement = services.block_friendship(self.a, self.edge.id)
        with self.assertRaises(ValidationError):
            services.respond_to_wave(self.b, self.edge.id, "accept")

        self.edge.refresh_from_db()
        self.assertEqual(self.edge.status, Friendship.Status.BLOCKED)
        self.assertEqual(self.edge.blocked_by_id, self.a.id)
        self.assertEqual(self.edge.acceptance_cutoff_seq, previous_boundary)
        self.assertLessEqual(old_message.seq, retirement["retirement_cutoff_seq"])

    def test_block_waits_on_explicit_accept_and_boundary_is_pre_accept_max(self):
        old_message, _previous_boundary = self._prepare_pending_incarnation("accept-wins-old")

        self._assert_waits_on_edge_lock(lambda: services.block_friendship(self.a, self.edge.id))
        accepted = services.respond_to_wave(self.b, self.edge.id, "accept")
        self.assertEqual(accepted.status, Friendship.Status.ACCEPTED)
        self.assertEqual(accepted.acceptance_cutoff_seq, old_message.seq)

        post_accept, created = self._send("accept-wins-new", "after persisted boundary")
        self.assertTrue(created)
        self.assertGreater(post_accept.seq, accepted.acceptance_cutoff_seq)
        _blocked_edge, retirement = services.block_friendship(self.a, self.edge.id)

        self.edge.refresh_from_db()
        self.assertEqual(self.edge.status, Friendship.Status.BLOCKED)
        self.assertEqual(self.edge.acceptance_cutoff_seq, old_message.seq)
        self.assertLessEqual(post_accept.seq, retirement["retirement_cutoff_seq"])

    def test_rewave_waits_on_block_lock_and_cannot_clear_block(self):
        old_message, target_handle, previous_boundary = self._prepare_revoked_incarnation("rewave-block-old")

        self._assert_waits_on_edge_lock(lambda: self._rewave(target_handle))
        _blocked_edge, retirement = services.block_friendship(self.b, self.edge.id)
        with self.assertRaises(NotFound):
            self._rewave(target_handle)

        self.edge.refresh_from_db()
        self.assertEqual(self.edge.status, Friendship.Status.BLOCKED)
        self.assertEqual(self.edge.blocked_by_id, self.b.id)
        self.assertEqual(self.edge.acceptance_cutoff_seq, previous_boundary)
        self.assertLessEqual(old_message.seq, retirement["retirement_cutoff_seq"])

    def test_block_waits_on_rewave_then_blocks_pending_winner(self):
        _old_message, target_handle, previous_boundary = self._prepare_revoked_incarnation("rewave-wins-old")

        self._assert_waits_on_edge_lock(lambda: services.block_friendship(self.b, self.edge.id))
        pending, created = self._rewave(target_handle)
        self.assertFalse(created)
        self.assertEqual(pending.status, Friendship.Status.PENDING)
        self.assertEqual(pending.acceptance_cutoff_seq, previous_boundary)

        services.block_friendship(self.b, self.edge.id)
        self.edge.refresh_from_db()
        self.assertEqual(self.edge.status, Friendship.Status.BLOCKED)
        self.assertEqual(self.edge.blocked_by_id, self.b.id)
        self.assertEqual(self.edge.acceptance_cutoff_seq, previous_boundary)

    def test_thread_open_waits_on_block_lock_and_creates_no_memberships(self):
        opener = _tenant("thread_open_block_opener")
        neighbor = _tenant("thread_open_block_neighbor")
        edge = _edge(opener, neighbor)

        self._assert_waits_on_edge_lock(lambda: services.open_thread(opener, edge.id), edge=edge)
        self.assertFalse(FriendThread.objects.filter(friendship=edge).exists())

        _blocked_edge, retirement = services.block_friendship(neighbor, edge.id)
        with self.assertRaises(DjangoPermissionDenied):
            services.open_thread(opener, edge.id)

        self.assertFalse(FriendThread.objects.filter(friendship=edge).exists())
        self.assertFalse(FriendThreadMembership.objects.filter(thread__friendship=edge).exists())
        self.assertIsNone(retirement["thread_id"])
        self.assertEqual(retirement["retirement_cutoff_seq"], 0)

    def test_block_waits_on_thread_open_then_keeps_created_memberships(self):
        opener = _tenant("thread_open_wins_opener")
        neighbor = _tenant("thread_open_wins_neighbor")
        edge = _edge(opener, neighbor)

        self._assert_waits_on_edge_lock(lambda: services.block_friendship(neighbor, edge.id), edge=edge)
        thread = services.open_thread(opener, edge.id)
        self.assertEqual(FriendThreadMembership.objects.filter(thread=thread).count(), 2)

        blocked, retirement = services.block_friendship(neighbor, edge.id)

        self.assertEqual(blocked.status, Friendship.Status.BLOCKED)
        self.assertEqual(retirement["thread_id"], str(thread.id))
        self.assertEqual(retirement["retirement_cutoff_seq"], 0)
        self.assertEqual(FriendThreadMembership.objects.filter(thread=thread).count(), 2)


@skipUnless(connection.vendor == "postgresql", "PostgreSQL FOR UPDATE SQL is required")
@override_settings(NBHD_DISABLE_BACKGROUND_THREADS=True)
class RetirementLockOrderTest(TransactionTestCase):
    def setUp(self):
        self.user_md_patcher = mock.patch(
            "apps.orchestrator.workspace_envelope.push_user_md",
            return_value=True,
        )
        self.user_md_patcher.start()
        self.addCleanup(self.user_md_patcher.stop)
        self.a = _tenant("retirement_order_a")
        self.b = _tenant("retirement_order_b")
        self.edge = _edge(self.a, self.b)
        self.thread = services.open_thread(self.a, self.edge.id)

    def _send(self, client_msg_id: str):
        with mock.patch("apps.friends.services._notify_friend_message"):
            return services.send_friend_message(
                self.a,
                self.a.user,
                self.thread.id,
                client_msg_id,
                "ordered",
            )

    def _prepare_revoked_incarnation(self, client_msg_id: str):
        message, _created = self._send(client_msg_id)
        services.unfriend_with_retirement(self.a, self.edge.id)
        services.ensure_neighbor_profile(self.a, self.a.user)
        target_profile = services.ensure_neighbor_profile(self.b, self.b.user)
        return message, target_profile.handle

    def _rewave(self, target_handle: str):
        with mock.patch("apps.friends.services._notify_wave_received"):
            return services.send_wave(self.a, self.a.user, target_handle, "ordered")

    def _capture(self, operation):
        records: list[tuple[str, tuple]] = []

        def trace(execute, sql, params, many, context):
            records.append((" ".join(str(sql).split()).upper(), tuple(connection.atomic_blocks)))
            return execute(sql, params, many, context)

        with connection.execute_wrapper(trace):
            result = operation()
        return result, records

    def _index(self, records, *needles):
        for index, (sql, _atomic_blocks) in enumerate(records):
            if all(needle in sql for needle in needles):
                return index
        self.fail(f"SQL containing {needles!r} was not executed: {[sql for sql, _stack in records]}")

    def _assert_same_outer_atomic(self, records, *indexes):
        stacks = [records[index][1] for index in indexes]
        self.assertTrue(all(stacks))
        outer = stacks[0][0]
        for stack in stacks[1:]:
            self.assertIs(stack[0], outer)

    def test_send_locks_friendship_before_message_insert_in_same_atomic(self):
        with mock.patch("apps.friends.services._notify_friend_message"):
            _result, records = self._capture(
                lambda: services.send_friend_message(
                    self.a,
                    self.a.user,
                    self.thread.id,
                    "ordered-send",
                    "serialized",
                )
            )

        lock_index = self._index(records, 'FROM "FRIENDSHIPS"', "FOR UPDATE")
        insert_index = self._index(records, 'INSERT INTO "FRIEND_MESSAGES"')
        self.assertLess(lock_index, insert_index)
        self._assert_same_outer_atomic(records, lock_index, insert_index)

    def test_block_locks_then_updates_then_reads_cutoff_in_same_atomic(self):
        with mock.patch("apps.friends.services._notify_friend_message"):
            services.send_friend_message(
                self.a,
                self.a.user,
                self.thread.id,
                "before-block-order",
                "before",
            )

        (_blocked_edge, retirement), records = self._capture(lambda: services.block_friendship(self.a, self.edge.id))

        lock_index = self._index(records, 'FROM "FRIENDSHIPS"', "FOR UPDATE")
        update_index = self._index(records, 'UPDATE "FRIENDSHIPS"')
        cutoff_index = self._index(records, "MAX(", 'FROM "FRIEND_MESSAGES"')
        self.assertLess(lock_index, update_index)
        self.assertLess(update_index, cutoff_index)
        self._assert_same_outer_atomic(records, lock_index, update_index, cutoff_index)
        self.assertGreater(retirement["retirement_cutoff_seq"], 0)

    def test_unfriend_locks_then_updates_then_reads_cutoff_in_same_atomic(self):
        with mock.patch("apps.friends.services._notify_friend_message"):
            services.send_friend_message(
                self.a,
                self.a.user,
                self.thread.id,
                "before-unfriend-order",
                "before",
            )

        (_retired_edge, retirement), records = self._capture(
            lambda: services.unfriend_with_retirement(self.a, self.edge.id)
        )

        lock_index = self._index(records, 'FROM "FRIENDSHIPS"', "FOR UPDATE")
        update_index = self._index(records, 'UPDATE "FRIENDSHIPS"')
        cutoff_index = self._index(records, "MAX(", 'FROM "FRIEND_MESSAGES"')
        self.assertLess(lock_index, update_index)
        self.assertLess(update_index, cutoff_index)
        self._assert_same_outer_atomic(records, lock_index, update_index, cutoff_index)
        self.assertGreater(retirement["retirement_cutoff_seq"], 0)

    def test_accept_locks_then_reads_boundary_then_updates_in_same_atomic(self):
        old_message, target_handle = self._prepare_revoked_incarnation("before-accept-order")
        pending, _created = self._rewave(target_handle)
        self.assertEqual(pending.status, Friendship.Status.PENDING)

        accepted, records = self._capture(
            lambda: services.respond_to_wave(self.b, self.edge.id, "accept"),
        )

        lock_index = self._index(records, 'FROM "FRIENDSHIPS"', "FOR UPDATE")
        cutoff_index = self._index(records, "MAX(", 'FROM "FRIEND_MESSAGES"')
        update_index = self._index(records, 'UPDATE "FRIENDSHIPS"')
        self.assertLess(lock_index, cutoff_index)
        self.assertLess(cutoff_index, update_index)
        self._assert_same_outer_atomic(records, lock_index, cutoff_index, update_index)
        self.assertEqual(accepted.acceptance_cutoff_seq, old_message.seq)
        self.assertEqual(accepted.acceptance_incarnation, 1)

    def test_invite_claim_locks_then_reads_boundary_then_updates_in_same_atomic(self):
        old_message, _target_handle = self._prepare_revoked_incarnation("before-invite-claim-order")
        invite = services.create_invite(self.a)

        claimed, records = self._capture(
            lambda: services.claim_invite(self.b, self.b.user, invite.token),
        )

        lock_index = self._index(records, 'FROM "FRIENDSHIPS"', "FOR UPDATE")
        cutoff_index = self._index(records, "MAX(", 'FROM "FRIEND_MESSAGES"')
        update_index = self._index(
            records,
            'UPDATE "FRIENDSHIPS"',
            '"ACCEPTANCE_CUTOFF_SEQ"',
            '"ACCEPTANCE_INCARNATION"',
        )
        self.assertLess(lock_index, cutoff_index)
        self.assertLess(cutoff_index, update_index)
        self._assert_same_outer_atomic(records, lock_index, cutoff_index, update_index)
        self.assertEqual(claimed.acceptance_cutoff_seq, old_message.seq)
        self.assertEqual(claimed.acceptance_incarnation, 1)

    def test_rewave_locks_before_pending_update_in_same_atomic(self):
        _old_message, target_handle = self._prepare_revoked_incarnation("before-rewave-order")

        (pending, _created), records = self._capture(lambda: self._rewave(target_handle))

        lock_index = self._index(records, 'FROM "FRIENDSHIPS"', "FOR UPDATE")
        update_index = self._index(records, 'UPDATE "FRIENDSHIPS"')
        self.assertLess(lock_index, update_index)
        self._assert_same_outer_atomic(records, lock_index, update_index)
        self.assertEqual(pending.status, Friendship.Status.PENDING)

    def test_decline_locks_before_status_update_in_same_atomic(self):
        requester = _tenant("decline_order_requester")
        addressee = _tenant("decline_order_addressee")
        edge = Friendship.objects.create(
            requester=requester,
            addressee=addressee,
            status=Friendship.Status.PENDING,
        )

        declined, records = self._capture(
            lambda: services.respond_to_wave(addressee, edge.id, "decline"),
        )

        lock_index = self._index(records, 'FROM "FRIENDSHIPS"', "FOR UPDATE")
        update_index = self._index(records, 'UPDATE "FRIENDSHIPS"')
        self.assertLess(lock_index, update_index)
        self._assert_same_outer_atomic(records, lock_index, update_index)
        self.assertEqual(declined.status, Friendship.Status.DECLINED)

    def test_unblock_locks_before_status_update_in_same_atomic(self):
        services.block_friendship(self.a, self.edge.id)

        unblocked, records = self._capture(lambda: services.unblock(self.a, self.edge.id))

        lock_index = self._index(records, 'FROM "FRIENDSHIPS"', "FOR UPDATE")
        update_index = self._index(records, 'UPDATE "FRIENDSHIPS"')
        self.assertLess(lock_index, update_index)
        self._assert_same_outer_atomic(records, lock_index, update_index)
        self.assertEqual(unblocked.status, Friendship.Status.REVOKED)

    def test_thread_open_locks_before_thread_and_membership_inserts(self):
        opener = _tenant("thread_order_opener")
        neighbor = _tenant("thread_order_neighbor")
        edge = _edge(opener, neighbor)

        thread, records = self._capture(lambda: services.open_thread(opener, edge.id))

        lock_index = self._index(records, 'FROM "FRIENDSHIPS"', "FOR UPDATE")
        thread_insert_index = self._index(records, 'INSERT INTO "FRIEND_THREADS"')
        membership_insert_index = self._index(records, 'INSERT INTO "FRIEND_THREAD_MEMBERSHIPS"')
        self.assertLess(lock_index, thread_insert_index)
        self.assertLess(thread_insert_index, membership_insert_index)
        self._assert_same_outer_atomic(records, lock_index, thread_insert_index, membership_insert_index)
        self.assertEqual(FriendThreadMembership.objects.filter(thread=thread).count(), 2)
