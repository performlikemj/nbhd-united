"""Consent and attribution contracts for the native mutual-aid experience."""

from unittest.mock import patch

from django.test import TestCase
from rest_framework.exceptions import NotFound

from . import access, circles, feed, services
from .models import FriendThread, SharedGoalMembership
from .test_pr6 import _client, _edge, _profile, _tenant


class MutualAidContractTests(TestCase):
    def setUp(self):
        self.a, self.b, self.c = [_tenant("aid_" + name) for name in ("a", "b", "c")]
        for tenant, handle in [(self.a, "aya"), (self.b, "ben"), (self.c, "cleo")]:
            _profile(tenant, handle)
        self.edge = _edge(self.a, self.b)
        self.mission = services.create_mission(
            self.a, self.a.user, self.edge.id, title="Help each other get started", description="One small step"
        )

    def test_invitation_discoverable_without_joining_or_revealing_activity(self):
        services.add_mission_update(self.a, self.a.user, self.mission.id, "note", "Member conversation")
        rows = services.list_missions(self.b, include_invited=True)
        self.assertEqual(rows[0]["my_status"], "invited")
        self.assertEqual(services.list_missions(self.b), [])
        self.assertEqual(list(access.missions_for(self.b)), [])
        preview = services.get_mission_detail(self.b, self.mission.id)
        self.assertEqual(preview["description"], "One small step")
        self.assertEqual(preview["members"], [])
        self.assertEqual(preview["updates"], [])
        with self.assertRaises(NotFound):
            services.add_mission_update(self.b, self.b.user, self.mission.id, "note", "Not yet")

    def test_accept_reveals_attributed_history_and_voluntary_commitment(self):
        services.add_mission_update(self.a, self.a.user, self.mission.id, "note", "Bring what you can")
        services.join_mission(self.b, self.b.user, self.mission.id, "Listen for ten minutes")
        detail = services.get_mission_detail(self.b, self.mission.id)
        self.assertEqual(detail["my_status"], "active")
        self.assertEqual(detail["my_commitment"], "Listen for ten minutes")
        note = next(u for u in detail["updates"] if u["text"] == "Bring what you can")
        self.assertEqual(note["author_name"], "Aya")

    def test_decline_is_idempotent_and_removes_invitation(self):
        client = _client(self.b.user)
        path = f"/api/v1/friends/missions/{self.mission.id}/decline/"
        self.assertEqual(client.post(path).status_code, 200)
        self.assertEqual(client.post(path).status_code, 200)
        self.assertEqual(services.list_missions(self.b, include_invited=True), [])
        with self.assertRaises(NotFound):
            services.join_mission(self.b, self.b.user, self.mission.id)
        with self.assertRaises(NotFound):
            services.get_mission_detail(self.b, self.mission.id)

    def test_stranger_cannot_read_or_decline_and_active_member_cannot_decline(self):
        for tenant in [self.c, self.a]:
            response = _client(tenant.user).post(f"/api/v1/friends/missions/{self.mission.id}/decline/")
            self.assertEqual(response.status_code, 404)
        self.assertEqual(services.list_missions(self.c), [])
        self.assertEqual(_client(self.c.user).get(f"/api/v1/friends/missions/{self.mission.id}/").status_code, 404)
        self.assertEqual(SharedGoalMembership.objects.get(shared_goal=self.mission, tenant=self.a).status, "active")

    def test_history_is_bounded_and_newest_first(self):
        for index in range(55):
            services.add_mission_update(self.a, self.a.user, self.mission.id, "note", str(index))
        history = services.get_mission_detail(self.a, self.mission.id)["updates"]
        self.assertEqual(len(history), 50)
        self.assertEqual(history[0]["text"], "54")

    @patch("apps.friends.services._notify_friend_message")
    def test_circle_feed_has_human_attribution_and_revokes_departed_reader(self, _notify):
        circle = circles.create_circle(self.a, self.a.user, name="Small steps")
        circles.join_circle(self.b, self.b.user, circle.invite_code)
        thread = FriendThread.objects.get(circle=circle)
        services.send_friend_message(self.b, self.b.user, thread.id, "aid-message-1", "I can listen")
        messages, _ = feed.build_thread_page(self.a, thread, cursor=None, limit=50)
        self.assertEqual(messages[0]["author"]["handle"], "ben")
        self.assertEqual(messages[0]["author"]["display_name"], "Ben")
        self.assertFalse(messages[0]["mine"])
        circles.leave_circle(self.b, circle.id)
        self.assertEqual(_client(self.b.user).get(f"/api/v1/friends/threads/{thread.id}/messages/").status_code, 404)

    def test_circle_assistant_choice_is_explicit_and_does_not_change_others(self):
        from .models import FriendThreadMembership

        response = _client(self.a.user).post(
            "/api/v1/friends/circles/", {"name": "Care", "agent_absorb_enabled": False}, format="json"
        )
        self.assertEqual(response.status_code, 201)
        circle = circles.Circle.objects.get(id=response.data["circle_id"])
        thread = FriendThread.objects.get(circle=circle)
        self.assertFalse(FriendThreadMembership.objects.get(thread=thread, tenant=self.a).agent_absorb_enabled)
        response = _client(self.b.user).post(
            "/api/v1/friends/circles/join/",
            {"invite_code": circle.invite_code, "agent_absorb_enabled": True},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(FriendThreadMembership.objects.get(thread=thread, tenant=self.b).agent_absorb_enabled)
        self.assertFalse(FriendThreadMembership.objects.get(thread=thread, tenant=self.a).agent_absorb_enabled)

    def test_network_contract_requires_enabled_authenticated_account(self):
        self.assertEqual(_client(self.a.user).get("/api/v1/friends/network/").data["version"], 1)
        self.c.friends_enabled = False
        self.c.save(update_fields=["friends_enabled"])
        self.assertEqual(_client(self.c.user).get("/api/v1/friends/network/").status_code, 403)

    def test_revoked_friendship_hides_and_prevents_accepting_invitation(self):
        self.edge.status = "blocked"
        self.edge.save(update_fields=["status"])
        self.assertEqual(services.list_missions(self.b, include_invited=True), [])
        response = _client(self.b.user).post(f"/api/v1/friends/missions/{self.mission.id}/join/")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(SharedGoalMembership.objects.get(shared_goal=self.mission, tenant=self.b).status, "invited")
