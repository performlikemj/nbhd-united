"""PR1 behavioral tests — Neighbors console + wave/accept + invites + callbacks.

Mirrors the PR0 rigor: HTTP-level flows via DRF APIClient (wave / accept /
decline / block / unfriend / profile / invites) plus service-level edge cases
(re-wave reuse, concurrent-race idempotency, notification hook) and the
Telegram/LINE callback handlers.
"""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.test import APIClient

from apps.tenants.models import Tenant, User

from . import services
from .models import FriendInvite, Friendship, NeighborProfile


def _make_tenant(username: str, *, friends_enabled: bool = True, status: str = "active") -> Tenant:
    user = User.objects.create_user(username=username, password="pass", display_name=username.title())
    return Tenant.objects.create(user=user, status=status, friends_enabled=friends_enabled)


def _client(user) -> APIClient:
    c = APIClient()
    c.force_authenticate(user=user)
    return c


def _profile(tenant, handle) -> NeighborProfile:
    return NeighborProfile.objects.create(tenant=tenant, handle=handle, display_name=handle.title())


class FlagGateTest(TestCase):
    def test_disabled_tenant_gets_403(self):
        t = _make_tenant("gate_off", friends_enabled=False)
        resp = _client(t.user).get("/api/v1/friends/")
        self.assertEqual(resp.status_code, 403)

    def test_enabled_tenant_gets_200(self):
        t = _make_tenant("gate_on")
        resp = _client(t.user).get("/api/v1/friends/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("neighbors", resp.json())


class WaveFlowTest(TestCase):
    def setUp(self):
        cache.clear()  # WaveSendDayThrottle is cache-backed; isolate between tests
        self.a = _make_tenant("wave_a")
        self.b = _make_tenant("wave_b")
        _profile(self.b, "bee")

    def test_wave_creates_pending_edge(self):
        with mock.patch("apps.friends.services._notify_wave_received") as notify:
            resp = _client(self.a.user).post("/api/v1/friends/waves/", {"handle": "bee", "note": "hi"}, format="json")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["status"], "pending")
        edge = Friendship.objects.get(requester=self.a, addressee=self.b)
        self.assertEqual(edge.status, "pending")
        self.assertEqual(edge.invite_note, "hi")
        notify.assert_called_once()

    def test_unknown_handle_404(self):
        resp = _client(self.a.user).post("/api/v1/friends/waves/", {"handle": "nobody"}, format="json")
        self.assertEqual(resp.status_code, 404)

    def test_self_wave_400(self):
        _profile(self.a, "aay")
        resp = _client(self.a.user).post("/api/v1/friends/waves/", {"handle": "aay"}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_blocked_edge_is_no_reveal_404(self):
        Friendship.objects.create(
            requester=self.b, addressee=self.a, status=Friendship.Status.BLOCKED, blocked_by=self.b
        )
        resp = _client(self.a.user).post("/api/v1/friends/waves/", {"handle": "bee"}, format="json")
        # Behaves exactly like an unknown handle — never reveals the block.
        self.assertEqual(resp.status_code, 404)

    def test_duplicate_wave_is_idempotent(self):
        with mock.patch("apps.friends.services._notify_wave_received") as notify:
            r1 = _client(self.a.user).post("/api/v1/friends/waves/", {"handle": "bee"}, format="json")
            r2 = _client(self.a.user).post("/api/v1/friends/waves/", {"handle": "bee"}, format="json")
        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r2.status_code, 200)  # existing edge, not re-created
        self.assertEqual(r1.json()["friendship_id"], r2.json()["friendship_id"])
        self.assertEqual(Friendship.objects.filter(requester=self.a, addressee=self.b).count(), 1)
        self.assertEqual(notify.call_count, 1)  # only the first send notifies

    def test_wave_back_on_pending_accepts(self):
        # b waved a; a waves back → accepted (mutual consent).
        _profile(self.a, "aay")
        Friendship.objects.create(requester=self.b, addressee=self.a, status=Friendship.Status.PENDING)
        with mock.patch("apps.friends.services._notify_wave_received"):
            resp = _client(self.a.user).post("/api/v1/friends/waves/", {"handle": "bee"}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "accepted")

    def test_wave_throttled_after_daily_cap(self):
        # Send up to the 10/day cap (idempotent re-sends still count as requests).
        client = _client(self.a.user)
        with mock.patch("apps.friends.services._notify_wave_received"):
            statuses = [
                client.post("/api/v1/friends/waves/", {"handle": "bee"}, format="json").status_code for _ in range(11)
            ]
        self.assertEqual(statuses[-1], 429)


class WaveRespondTest(TestCase):
    def setUp(self):
        self.a = _make_tenant("resp_a")  # requester (waver)
        self.b = _make_tenant("resp_b")  # addressee
        self.c = _make_tenant("resp_c")  # stranger
        self.edge = Friendship.objects.create(requester=self.a, addressee=self.b, status=Friendship.Status.PENDING)

    def test_addressee_accepts(self):
        resp = _client(self.b.user).post(f"/api/v1/friends/waves/{self.edge.id}/accept/")
        self.assertEqual(resp.status_code, 200)
        self.edge.refresh_from_db()
        self.assertEqual(self.edge.status, "accepted")
        self.assertIsNotNone(self.edge.responded_at)

    def test_requester_cannot_accept_own_wave(self):
        resp = _client(self.a.user).post(f"/api/v1/friends/waves/{self.edge.id}/accept/")
        self.assertEqual(resp.status_code, 403)
        self.edge.refresh_from_db()
        self.assertEqual(self.edge.status, "pending")

    def test_non_party_accept_denied_idor(self):
        # §4.5 IDOR: a stranger swaps in someone else's friendship_id.
        resp = _client(self.c.user).post(f"/api/v1/friends/waves/{self.edge.id}/accept/")
        self.assertEqual(resp.status_code, 404)  # no-reveal

    def test_addressee_declines(self):
        resp = _client(self.b.user).post(f"/api/v1/friends/waves/{self.edge.id}/decline/")
        self.assertEqual(resp.status_code, 200)
        self.edge.refresh_from_db()
        self.assertEqual(self.edge.status, "declined")

    def test_either_party_blocks(self):
        resp = _client(self.a.user).post(f"/api/v1/friends/waves/{self.edge.id}/block/")
        self.assertEqual(resp.status_code, 200)
        self.edge.refresh_from_db()
        self.assertEqual(self.edge.status, "blocked")
        self.assertEqual(self.edge.blocked_by_id, self.a.id)

    def test_unfriend_revokes(self):
        Friendship.objects.filter(id=self.edge.id).update(status=Friendship.Status.ACCEPTED)
        resp = _client(self.b.user).delete(f"/api/v1/friends/{self.edge.id}/")
        self.assertEqual(resp.status_code, 200)
        self.edge.refresh_from_db()
        self.assertEqual(self.edge.status, "revoked")
        self.assertIsNotNone(self.edge.revoked_at)

    def test_unfriend_leaves_block_intact(self):
        Friendship.objects.filter(id=self.edge.id).update(status=Friendship.Status.BLOCKED, blocked_by=self.a)
        _client(self.b.user).delete(f"/api/v1/friends/{self.edge.id}/")
        self.edge.refresh_from_db()
        self.assertEqual(self.edge.status, "blocked")  # not resurrected/revoked


class ReWaveAndRaceServiceTest(TestCase):
    """Service-level edge cases that are awkward over HTTP."""

    def setUp(self):
        self.a = _make_tenant("rw_a")
        self.b = _make_tenant("rw_b")
        _profile(self.a, "alfa")
        _profile(self.b, "bravo")

    def test_rewave_after_decline_reuses_row_flips_pending(self):
        edge = Friendship.objects.create(requester=self.a, addressee=self.b, status=Friendship.Status.DECLINED)
        original_id = edge.id
        with mock.patch("apps.friends.services._notify_wave_received"):
            new_edge, created = services.send_wave(self.b, self.b.user, "alfa", "second try")
        self.assertFalse(created)  # reused, not a new row
        self.assertEqual(new_edge.id, original_id)
        self.assertEqual(new_edge.status, "pending")
        self.assertEqual(new_edge.requester_id, self.b.id)  # direction swapped to new initiator
        self.assertEqual(new_edge.addressee_id, self.a.id)
        self.assertEqual(Friendship.objects.count(), 1)

    def test_concurrent_race_returns_existing_edge(self):
        # Simulate the pair_key unique-constraint losing race: first wave lands,
        # then a "concurrent" create hits IntegrityError → we return the winner.
        Friendship.objects.create(requester=self.b, addressee=self.a, status=Friendship.Status.PENDING)
        with mock.patch("apps.friends.services._notify_wave_received"):
            # a waves b while b→a already pending; a is the addressee → accepts.
            edge, created = services.send_wave(self.a, self.a.user, "bravo")
        self.assertFalse(created)
        self.assertEqual(Friendship.objects.count(), 1)


class ProfileTest(TestCase):
    def setUp(self):
        self.t = _make_tenant("prof_a")

    def test_get_autocreates_profile(self):
        self.assertFalse(NeighborProfile.objects.filter(tenant=self.t).exists())
        resp = _client(self.t.user).get("/api/v1/friends/profile/")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(NeighborProfile.objects.filter(tenant=self.t).exists())
        self.assertTrue(resp.json()["handle"])

    def test_patch_valid_handle(self):
        resp = _client(self.t.user).patch(
            "/api/v1/friends/profile/", {"handle": "cool_neighbor", "bio": "hi", "avatar_hue": 42}, format="json"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["handle"], "cool_neighbor")
        self.assertEqual(resp.json()["avatar_hue"], 42)

    def test_patch_invalid_handle_400(self):
        resp = _client(self.t.user).patch("/api/v1/friends/profile/", {"handle": "ab"}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_patch_reserved_handle_400(self):
        for reserved in ("admin", "nbhd", "neighborhood", "support", "mj"):
            resp = _client(self.t.user).patch("/api/v1/friends/profile/", {"handle": reserved}, format="json")
            self.assertEqual(resp.status_code, 400, reserved)

    def test_patch_taken_handle_400(self):
        other = _make_tenant("prof_b")
        _profile(other, "taken")
        resp = _client(self.t.user).patch("/api/v1/friends/profile/", {"handle": "taken"}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_hue_out_of_range_400(self):
        resp = _client(self.t.user).patch("/api/v1/friends/profile/", {"avatar_hue": 900}, format="json")
        self.assertEqual(resp.status_code, 400)


class InviteTest(TestCase):
    def setUp(self):
        self.inviter = _make_tenant("inv_a")
        self.claimer = _make_tenant("inv_b")

    def test_create_invite(self):
        resp = _client(self.inviter.user).post("/api/v1/friends/invites/", {"max_uses": 3}, format="json")
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertTrue(body["token"])
        self.assertIn(body["token"], body["url"])
        self.assertEqual(body["max_uses"], 3)

    def test_public_metadata(self):
        invite = services.create_invite(self.inviter)
        resp = APIClient().get(f"/api/v1/friends/invites/{invite.token}/")  # AllowAny, no auth
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["valid"])
        self.assertIn("inviter_display_name", resp.json())

    def test_claim_creates_accepted_edge(self):
        invite = services.create_invite(self.inviter)
        resp = _client(self.claimer.user).post(f"/api/v1/friends/invites/{invite.token}/claim/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "accepted")
        edge = Friendship.objects.get(pair_key__isnull=False)
        self.assertEqual(edge.status, "accepted")
        invite.refresh_from_db()
        self.assertEqual(invite.uses, 1)

    def test_claim_own_invite_400(self):
        invite = services.create_invite(self.inviter)
        resp = _client(self.inviter.user).post(f"/api/v1/friends/invites/{invite.token}/claim/")
        self.assertEqual(resp.status_code, 400)

    def test_claim_expired_400(self):
        invite = services.create_invite(self.inviter)
        FriendInvite.objects.filter(id=invite.id).update(expires_at=timezone.now() - timedelta(days=1))
        resp = _client(self.claimer.user).post(f"/api/v1/friends/invites/{invite.token}/claim/")
        self.assertEqual(resp.status_code, 400)

    def test_claim_used_up_400(self):
        invite = services.create_invite(self.inviter, max_uses=1)
        FriendInvite.objects.filter(id=invite.id).update(uses=1)
        resp = _client(self.claimer.user).post(f"/api/v1/friends/invites/{invite.token}/claim/")
        self.assertEqual(resp.status_code, 400)


class NeighborhoodListingTest(TestCase):
    def test_buckets_neighbors_and_pending(self):
        me = _make_tenant("list_me")
        friend = _make_tenant("list_friend")
        waver = _make_tenant("list_waver")
        waved = _make_tenant("list_waved")
        _profile(me, "me_h")
        _profile(friend, "friend_h")
        _profile(waver, "waver_h")
        _profile(waved, "waved_h")
        Friendship.objects.create(requester=me, addressee=friend, status=Friendship.Status.ACCEPTED)
        Friendship.objects.create(requester=waver, addressee=me, status=Friendship.Status.PENDING)  # incoming
        Friendship.objects.create(requester=me, addressee=waved, status=Friendship.Status.PENDING)  # outgoing

        data = _client(me.user).get("/api/v1/friends/").json()
        self.assertEqual([n["handle"] for n in data["neighbors"]], ["friend_h"])
        self.assertEqual([p["handle"] for p in data["pending_incoming"]], ["waver_h"])
        self.assertEqual([p["handle"] for p in data["pending_outgoing"]], ["waved_h"])
        # never leaks a tenant_id
        self.assertNotIn("tenant_id", data["neighbors"][0])


class CallbackHandlerTest(TestCase):
    """The Telegram/LINE wave callback handlers are double-tap idempotent."""

    def setUp(self):
        self.a = _make_tenant("cb_a")
        self.b = _make_tenant("cb_b")
        _profile(self.a, "cbalfa")
        self.edge = Friendship.objects.create(requester=self.a, addressee=self.b, status=Friendship.Status.PENDING)

    def _update(self, action):
        return {
            "callback_query": {
                "id": "cbq-1",
                "data": f"friend:{action}:{self.edge.id}",
                "message": {"chat": {"id": 999}, "message_id": 111},
            }
        }

    def test_telegram_accept_then_idempotent(self):
        from apps.router.friends_callbacks import handle_friend_callback

        r1 = handle_friend_callback(self._update("accept"), self.b)
        self.edge.refresh_from_db()
        self.assertEqual(self.edge.status, "accepted")
        self.assertEqual(r1.status_code, 200)
        # second tap (decline after accept) is answered gracefully, not an error
        r2 = handle_friend_callback(self._update("decline"), self.b)
        self.edge.refresh_from_db()
        self.assertEqual(self.edge.status, "accepted")  # unchanged
        self.assertEqual(r2.status_code, 200)

    def test_telegram_non_addressee_rejected(self):
        from apps.router.friends_callbacks import handle_friend_callback

        # a (the requester) can't accept via the button either.
        handle_friend_callback(self._update("accept"), self.a)
        self.edge.refresh_from_db()
        self.assertEqual(self.edge.status, "pending")

    def test_line_postback_accept(self):
        from apps.router.friends_callbacks import handle_friend_line_postback

        reply = handle_friend_line_postback(self.b, f"friend:accept:{self.edge.id}")
        self.edge.refresh_from_db()
        self.assertEqual(self.edge.status, "accepted")
        self.assertIn("neighbors", reply.lower())


class HandleValidationServiceTest(TestCase):
    def setUp(self):
        self.t = _make_tenant("hv_a")

    def test_reserved_rejected(self):
        with self.assertRaises(ValidationError):
            services.validate_handle("admin", self.t)

    def test_format_rejected(self):
        # Note: uppercase is silently lowercased (friendly), so "UPPER" is valid.
        for bad in ("ab", "has space", "a" * 31, "bad-dash", "bad!char", "café"):
            with self.assertRaises(ValidationError):
                services.validate_handle(bad, self.t)

    def test_uppercase_is_normalized_not_rejected(self):
        self.assertEqual(services.validate_handle("CoolName", self.t), "coolname")

    def test_derive_unique_handle_avoids_collision(self):
        _profile(_make_tenant("hv_b"), "kenji")
        derived = services.derive_unique_handle("Kenji")
        self.assertNotEqual(derived, "kenji")
        self.assertTrue(derived.startswith("kenji"))


class RespondServiceGuardTest(TestCase):
    def test_requester_accept_raises_permission_denied(self):
        a = _make_tenant("rg_a")
        b = _make_tenant("rg_b")
        edge = Friendship.objects.create(requester=a, addressee=b, status=Friendship.Status.PENDING)
        with self.assertRaises(PermissionDenied):
            services.respond_to_wave(a, edge.id, "accept")
