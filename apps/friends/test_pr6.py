"""PR6 behavioral tests — Missions (shared goals + crew projection + weekly huddle).

The "PM" is a control-plane status projection + a QStash digest cron, not a human
and not an agent-participant. Each agent nudges its OWN human (PendingGoalAction);
approvals mint the member's own local Task + emit the single SharedGoalUpdate
stream that feeds projection + digest + envelope.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from unittest import mock
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.exceptions import NotFound
from rest_framework.test import APIClient

from apps.journal.models import Task
from apps.tenants.models import Tenant, User

from . import digest, envelope, projection, services
from .models import (
    Friendship,
    NeighborProfile,
    SharedGoal,
    SharedGoalMembership,
    SharedGoalUpdate,
)


def _tenant(username, *, friends_enabled=True):
    user = User.objects.create_user(username=username, password="pass", display_name=username.title())
    return Tenant.objects.create(user=user, status="active", friends_enabled=friends_enabled)


def _profile(tenant, handle):
    return NeighborProfile.objects.create(tenant=tenant, handle=handle, display_name=handle.title())


def _edge(a, b):
    return Friendship.objects.create(requester=a, addressee=b, status=Friendship.Status.ACCEPTED)


def _client(user):
    c = APIClient()
    c.force_authenticate(user=user)
    return c


def _completed(mission, tenant, when):
    update = SharedGoalUpdate.objects.create(
        shared_goal=mission,
        tenant=tenant,
        kind=SharedGoalUpdate.Kind.TASK_COMPLETED,
        payload={"task_id": str(uuid.uuid4())},
    )
    SharedGoalUpdate.objects.filter(id=update.id).update(created_at=when)
    return update


class MissionCreateJoinTest(TestCase):
    def setUp(self):
        self.a = _tenant("m_a")
        self.b = _tenant("m_b")
        _profile(self.a, "alfa")
        _profile(self.b, "bravo")
        self.edge = _edge(self.a, self.b)

    def test_create_owner_active_other_invited(self):
        mission = services.create_mission(self.a, self.a.user, str(self.edge.id), title="July Steps")
        owner = SharedGoalMembership.objects.get(shared_goal=mission, tenant=self.a)
        other = SharedGoalMembership.objects.get(shared_goal=mission, tenant=self.b)
        self.assertEqual((owner.role, owner.status), ("owner", "active"))
        self.assertEqual((other.role, other.status), ("member", "invited"))

    def test_join_and_leave_lifecycle(self):
        mission = services.create_mission(self.a, self.a.user, str(self.edge.id), title="July Steps")
        services.join_mission(self.b, self.b.user, str(mission.id), commitment="walk daily")
        self.assertEqual(SharedGoalMembership.objects.get(shared_goal=mission, tenant=self.b).status, "active")
        services.leave_mission(self.b, str(mission.id))
        self.assertEqual(SharedGoalMembership.objects.get(shared_goal=mission, tenant=self.b).status, "left")

    def test_non_member_detail_404_no_reveal(self):
        mission = services.create_mission(self.a, self.a.user, str(self.edge.id), title="July Steps")
        stranger = _tenant("m_stranger")
        resp = _client(stranger.user).get(f"/api/v1/friends/missions/{mission.id}/")
        self.assertEqual(resp.status_code, 404)

    def test_create_requires_accepted_friendship(self):
        stranger = _tenant("m_stranger2")
        pending = Friendship.objects.create(requester=self.a, addressee=stranger, status=Friendship.Status.PENDING)
        resp = _client(self.a.user).post(
            "/api/v1/friends/missions/", {"friendship_id": str(pending.id), "title": "x"}, format="json"
        )
        self.assertEqual(resp.status_code, 403)


class ProjectionTest(TestCase):
    def setUp(self):
        self.a = _tenant("p_a")
        self.b = _tenant("p_b")
        _profile(self.a, "aya")
        _profile(self.b, "kiho")
        self.edge = _edge(self.a, self.b)
        self.mission = services.create_mission(
            self.a, self.a.user, str(self.edge.id), title="July Steps", target={"cadence": "daily", "value": 10000}
        )
        services.join_mission(self.b, self.b.user, str(self.mission.id))

    def test_folds_showed_up_streak_and_overall(self):
        now = timezone.now()
        # a: 3 consecutive days up to today → streak 3, showed_up 3.
        for d in range(3):
            _completed(self.mission, self.a, now - timedelta(days=d))
        # b: one day 5 days ago → showed_up 1, streak 0.
        _completed(self.mission, self.b, now - timedelta(days=5))

        status = projection.build_mission_status(self.mission, now=now)
        by_handle = {m["handle"]: m for m in status["members"]}
        self.assertEqual(by_handle["aya"]["showed_up"], 3)
        self.assertEqual(by_handle["aya"]["streak"], 3)
        self.assertEqual(by_handle["kiho"]["showed_up"], 1)
        self.assertEqual(by_handle["kiho"]["streak"], 0)
        # overall = (3+1) / (2 members * 7 days) = 4/14 ≈ 29%.
        self.assertEqual(status["overall_pct"], round(100 * 4 / 14))

    def test_outside_window_not_counted(self):
        now = timezone.now()
        _completed(self.mission, self.a, now - timedelta(days=20))  # beyond the 7-day daily window
        status = projection.build_mission_status(self.mission, now=now)
        self.assertEqual({m["handle"]: m["showed_up"] for m in status["members"]}["aya"], 0)

    def test_next_step_is_open_task_added(self):
        SharedGoalUpdate.objects.create(
            shared_goal=self.mission,
            tenant=self.a,
            kind=SharedGoalUpdate.Kind.TASK_ADDED,
            payload={"title": "Buy shoes"},
        )
        status = projection.build_mission_status(self.mission)
        self.assertEqual({m["handle"]: m["next_step"] for m in status["members"]}["aya"], "Buy shoes")


class TaskLinkageTest(TestCase):
    def setUp(self):
        self.a = _tenant("tl_a")
        self.b = _tenant("tl_b")
        self.edge = _edge(self.a, self.b)
        self.mission = services.create_mission(self.a, self.a.user, str(self.edge.id), title="July Steps")

    def test_completing_linked_task_appends_update_once(self):
        task = Task.objects.create(
            tenant=self.a,
            title="Walk 10k",
            related_ref={"pillar": "friends", "object_type": "shared_goal", "object_id": str(self.mission.id)},
        )
        # Not complete yet → no update.
        self.assertEqual(SharedGoalUpdate.objects.filter(shared_goal=self.mission, kind="task_completed").count(), 0)
        task.complete()
        self.assertEqual(SharedGoalUpdate.objects.filter(shared_goal=self.mission, kind="task_completed").count(), 1)
        # Re-save the done task → still one (idempotent).
        task.save()
        self.assertEqual(SharedGoalUpdate.objects.filter(shared_goal=self.mission, kind="task_completed").count(), 1)

    def test_unlinked_task_no_update(self):
        task = Task.objects.create(tenant=self.a, title="unrelated")
        task.complete()
        self.assertEqual(SharedGoalUpdate.objects.filter(kind="task_completed").count(), 0)

    def test_add_mission_task_mints_own_task_and_added_update(self):
        result = services.add_mission_task(self.a, self.a.user, str(self.mission.id), title="Prep gym bag")
        task = Task.objects.get(id=result["task_id"])
        self.assertEqual(task.tenant_id, self.a.id)  # the caller's OWN task
        self.assertEqual(task.related_ref["object_id"], str(self.mission.id))
        self.assertEqual(task.pii_receipts["title"], {"state": "bypass"})
        self.assertTrue(SharedGoalUpdate.objects.filter(shared_goal=self.mission, kind="task_added").exists())

    def test_flag_on_mission_task_stores_placeholder_and_receipt(self):
        self.a.layer1_placeholder_writes = True
        self.a.pii_entity_map = {"[PERSON_1]": {"name": "Alice"}}
        self.a.save(update_fields=["layer1_placeholder_writes", "pii_entity_map"])
        with (
            patch("apps.pii.redactor._detect_pii", return_value=[]),
            patch("apps.pii.authoring._detect_pii", return_value=[]),
        ):
            result = services.add_mission_task(
                self.a,
                self.a.user,
                str(self.mission.id),
                title="Walk with Alice",
            )

        task = Task.objects.get(id=result["task_id"])
        self.assertEqual(task.title, "Walk with [PERSON_1]")
        self.assertEqual(task.pii_receipts["title"]["state"], "placeholder")

    def test_near_limit_mission_task_truncates_after_authoring_without_partial_token(self):
        self.a.layer1_placeholder_writes = True
        self.a.pii_entity_map = {"[PERSON_1]": {"name": "Amy"}}
        self.a.save(update_fields=["layer1_placeholder_writes", "pii_entity_map"])
        with (
            patch("apps.pii.redactor._detect_pii", return_value=[]),
            patch("apps.pii.authoring._detect_pii", return_value=[]),
        ):
            result = services.add_mission_task(
                self.a,
                self.a.user,
                str(self.mission.id),
                title="x" * 250 + " Amy!",
            )

        task = Task.objects.get(id=result["task_id"])
        self.assertEqual(task.title, "x" * 250 + " ")
        self.assertLessEqual(len(task.title), Task._meta.get_field("title").max_length)
        self.assertNotIn("[PERSON", task.title)


class ProposeApproveTest(TestCase):
    def setUp(self):
        self.a = _tenant("pa_a")
        self.b = _tenant("pa_b")
        self.edge = _edge(self.a, self.b)
        self.mission = services.create_mission(self.a, self.a.user, str(self.edge.id), title="July Steps")

    def test_propose_creates_pending_no_task(self):
        action, created = services.propose_mission_task(self.a, str(self.mission.id), title="Walk after dinner")
        self.assertTrue(created)
        self.assertEqual(action.status, "pending")
        self.assertEqual(action.tenant_id, self.a.id)
        self.assertEqual(Task.objects.filter(tenant=self.a).count(), 0)  # no task until human approves

    def test_propose_idempotent(self):
        a1, c1 = services.propose_mission_task(self.a, str(self.mission.id), title="Walk")
        a2, c2 = services.propose_mission_task(self.a, str(self.mission.id), title="Walk")
        self.assertTrue(c1)
        self.assertFalse(c2)
        self.assertEqual(a1.id, a2.id)

    def test_propose_by_non_member_denied(self):
        stranger = _tenant("pa_stranger")
        with self.assertRaises(NotFound):
            services.propose_mission_task(stranger, str(self.mission.id), title="x")

    def test_approve_mints_own_task_and_added_update(self):
        action, _ = services.propose_mission_task(self.a, str(self.mission.id), title="Walk after dinner")
        result = services.approve_goal_action(self.a, str(action.id))
        task = Task.objects.get(id=result["task_id"])
        self.assertEqual(task.tenant_id, self.a.id)
        action.refresh_from_db()
        self.assertEqual(action.status, "approved")
        self.assertTrue(SharedGoalUpdate.objects.filter(shared_goal=self.mission, kind="task_added").exists())

    def test_reject(self):
        action, _ = services.propose_mission_task(self.a, str(self.mission.id), title="x")
        services.reject_goal_action(self.a, str(action.id))
        action.refresh_from_db()
        self.assertEqual(action.status, "rejected")

    def test_approve_foreign_action_404(self):
        action, _ = services.propose_mission_task(self.a, str(self.mission.id), title="x")
        with self.assertRaises(NotFound):
            services.approve_goal_action(self.b, str(action.id))  # not the proposer's tenant


class OptimisticLockTest(TestCase):
    def setUp(self):
        self.a = _tenant("ol_a")
        self.b = _tenant("ol_b")
        self.edge = _edge(self.a, self.b)
        self.mission = services.create_mission(self.a, self.a.user, str(self.edge.id), title="July Steps")

    def test_version_conflict_409(self):
        payload, code = services.update_mission(
            self.a, str(self.mission.id), expected_version=99, fields={"title": "new"}
        )
        self.assertEqual(code, 409)

    def test_matching_version_ok_and_bumps(self):
        payload, code = services.update_mission(
            self.a, str(self.mission.id), expected_version=0, fields={"title": "Renamed"}
        )
        self.assertEqual(code, 200)
        self.assertEqual(payload["version"], 1)
        self.mission.refresh_from_db()
        self.assertEqual(self.mission.title, "Renamed")

    def test_foreign_lock_409(self):
        SharedGoal.objects.filter(id=self.mission.id).update(
            edit_lock_until=timezone.now() + timedelta(minutes=5), edit_lock_owner="user:someone-else"
        )
        payload, code = services.update_mission(self.a, str(self.mission.id), expected_version=0, fields={"title": "x"})
        self.assertEqual(code, 409)


class DigestTest(TestCase):
    def setUp(self):
        self.a = _tenant("d_a")
        self.b = _tenant("d_b")
        _profile(self.a, "dalfa")
        _profile(self.b, "dbravo")
        self.edge = _edge(self.a, self.b)
        self.mission = services.create_mission(self.a, self.a.user, str(self.edge.id), title="July Steps")
        services.join_mission(self.b, self.b.user, str(self.mission.id))

    def test_idempotent_per_member_window(self):
        with mock.patch("apps.friends.digest._deliver_digest") as deliver:
            first = digest.run_weekly_mission_digest()
            second = digest.run_weekly_mission_digest()
        self.assertEqual(first["sent"], 2)  # both active members
        self.assertEqual(second["sent"], 0)  # CAS prevents a double-nudge
        self.assertEqual(deliver.call_count, 2)

    def test_left_member_gets_no_digest(self):
        services.leave_mission(self.b, str(self.mission.id))
        with mock.patch("apps.friends.digest._deliver_digest") as deliver:
            result = digest.run_weekly_mission_digest()
        self.assertEqual(result["sent"], 1)  # only the remaining active member
        self.assertEqual(deliver.call_count, 1)

    def test_dedup_id_has_no_colon_or_whitespace(self):
        dedup = digest.digest_dedup_id(self.mission.id, self.a.id, digest.iso_week(timezone.now()))
        self.assertNotIn(":", dedup)
        self.assertFalse(any(c.isspace() for c in dedup))

    def test_render_is_warm_non_shaming(self):
        text = digest._render_digest(projection.build_mission_status(self.mission))
        self.assertIn("July Steps", text)
        self.assertIn("crew", text.lower())

    def test_app_channel_member_digest_writes_proactive_outbound(self):
        """A token-holding member (iOS device, no Telegram/LINE) is delivered via
        a ProactiveOutbound row — the APNs push + ?since= feed IS the delivery.
        Regression: the app branch previously ``return True``'d without writing
        anything, so once outbound routing became app-first the digest silently
        vanished for token-holders."""
        from apps.router.models import DeviceToken, ProactiveOutbound

        DeviceToken.objects.create(tenant=self.a, user=self.a.user, token="f" * 64)
        with mock.patch("apps.router.proactive_context._dispatch_ios_push"):
            delivered = digest._deliver_text(self.a, "\U0001f331 crew digest")

        self.assertTrue(delivered)
        row = ProactiveOutbound.objects.get(tenant=self.a)
        self.assertEqual(row.channel, "app")
        self.assertEqual(row.message_text, "\U0001f331 crew digest")
        self.assertEqual(row.channel_user_id, str(self.a.user_id))

    def test_eval_sink_does_not_fall_through_to_telegram(self):
        """An eval-sink member's digest never touches a real transport — it is
        recorded as an internal ``eval`` evidence row (no APNs, no Telegram),
        even with a stale Telegram id still linked."""
        from apps.router.models import ProactiveOutbound

        self.a.is_synthetic = True
        self.a.is_eval_sink = True
        self.a.save(update_fields=["is_synthetic", "is_eval_sink"])
        self.a.user.telegram_chat_id = 987654
        self.a.user.save(update_fields=["telegram_chat_id"])
        with (
            mock.patch("apps.router.services.send_telegram_message") as send,
            mock.patch("apps.router.proactive_context._dispatch_ios_push") as push,
        ):
            delivered = digest._deliver_text(self.a, "weekly update")
        self.assertTrue(delivered)
        send.assert_not_called()
        push.assert_not_called()
        row = ProactiveOutbound.objects.get(tenant=self.a)
        self.assertEqual(row.channel, "eval")


class EnvelopeMissionsTest(TestCase):
    def test_renders_active_missions_and_hides_after_leave(self):
        a = _tenant("em_a")
        b = _tenant("em_b")
        _profile(a, "ema")
        edge = _edge(a, b)
        mission = services.create_mission(a, a.user, str(edge.id), title="July Steps")
        SharedGoalMembership.objects.filter(shared_goal=mission, tenant=a).update(commitment="10k steps")

        out = envelope.render_missions(a)
        self.assertIn("July Steps", out)
        self.assertIn("10k steps", out)

        services.leave_mission(a, str(mission.id))
        self.assertEqual(envelope.render_missions(a), "")

    def test_never_raises(self):
        self.assertEqual(envelope.render_missions(object()), "")
