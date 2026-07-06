"""PR9 behavioral tests — first-class Lesson.pillar, neighbor/circle caps, and the
friends_agent_propose_enabled split (absorb-only vs propose-enabled agent)."""

from __future__ import annotations

from unittest import mock

from django.apps import apps as global_apps
from django.test import TestCase
from django.test.utils import override_settings
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.lessons.models import Lesson
from apps.lessons.pillars import infer_pillar_from_tags
from apps.orchestrator.personas import render_workspace_files
from apps.tenants.models import Tenant, User
from apps.tenants.test_utils import seed_internal_key

from . import circles, services
from .models import Friendship, NeighborProfile

_RUNTIME = "/api/v1/integrations/runtime"


def _tenant(username, *, friends_enabled=True, propose_enabled=False) -> Tenant:
    user = User.objects.create_user(username=username, password="pass", display_name=username.title())
    return Tenant.objects.create(
        user=user,
        status="active",
        friends_enabled=friends_enabled,
        friends_agent_propose_enabled=propose_enabled,
    )


def _profile(tenant, handle):
    return NeighborProfile.objects.create(tenant=tenant, handle=handle, display_name=handle.title())


def _accepted(a, b):
    return Friendship.objects.create(requester=a, addressee=b, status=Friendship.Status.ACCEPTED)


def _lesson(tenant, *, tags=None, pillar=None, text="A thing I learned.") -> Lesson:
    lesson = Lesson.objects.create(
        tenant=tenant, text=text, source_type="experience", status="approved", tags=tags or []
    )
    if pillar is not None:
        Lesson.objects.filter(id=lesson.id).update(pillar=pillar)  # bypass save() auto-fill
        lesson.refresh_from_db()
    return lesson


# ── Lesson.pillar field ───────────────────────────────────────────────────────


class PillarFieldTest(TestCase):
    def setUp(self):
        self.t = _tenant("pf")

    def test_helper_classifies_tags(self):
        self.assertEqual(infer_pillar_from_tags(["finance", "x"]), "gravity")
        self.assertEqual(infer_pillar_from_tags(["meditation"]), "core")
        self.assertEqual(infer_pillar_from_tags(["cooking"]), "lessons")
        self.assertEqual(infer_pillar_from_tags([]), "lessons")

    def test_save_autofills_from_tags(self):
        self.assertEqual(_lesson(self.t, tags=["debt"]).pillar, "gravity")
        self.assertEqual(_lesson(self.t, tags=["mindfulness"]).pillar, "core")
        self.assertEqual(_lesson(self.t, tags=["running"]).pillar, "lessons")

    def test_save_leaves_tagless_blank(self):
        self.assertEqual(_lesson(self.t, tags=[]).pillar, "")

    def test_explicit_pillar_not_overwritten(self):
        lesson = Lesson(
            tenant=self.t, text="x", source_type="experience", status="approved", tags=["finance"], pillar="fuel"
        )
        lesson.save()
        self.assertEqual(lesson.pillar, "fuel")  # save() only fills a BLANK pillar

    def test_lesson_pillar_prefers_field(self):
        lesson = _lesson(self.t, tags=["running"], pillar="core")
        self.assertEqual(services.lesson_pillar(lesson), "core")


# ── Share-block: field-first, heuristic fallback, never weaker ────────────────


class ShareBlockTest(TestCase):
    def setUp(self):
        self.t = _tenant("sb")

    def test_field_based_block(self):
        lesson = _lesson(self.t, tags=["running"], pillar="gravity")  # neutral tags, blocked field
        with self.assertRaises(PermissionDenied):
            services.assert_shareable_pillar(lesson)

    def test_heuristic_fallback_block_when_field_blank(self):
        lesson = _lesson(self.t, tags=["finance"], pillar="")  # blank field, finance tags
        with self.assertRaises(PermissionDenied):
            services.assert_shareable_pillar(lesson)

    def test_never_weaker_neutral_field_but_blocked_tags(self):
        # Field says the neutral "lessons" pillar, but a finance tag was added
        # after — the OR check still blocks (never weaker than pre-field).
        lesson = _lesson(self.t, tags=["debt"], pillar="lessons")
        with self.assertRaises(PermissionDenied):
            services.assert_shareable_pillar(lesson)

    def test_neutral_lesson_is_shareable(self):
        lesson = _lesson(self.t, tags=["cooking"], pillar="lessons")
        services.assert_shareable_pillar(lesson)  # no raise


class BackfillMigrationTest(TestCase):
    def test_backfill_sets_pillar_from_tags_leaves_tagless_blank(self):
        from importlib import import_module

        # Module name starts with a digit → import via string path.
        backfill = import_module("apps.lessons.migrations.0006_backfill_lesson_pillar").backfill_pillar
        t = _tenant("bf")
        finance = _lesson(t, tags=["finance"])
        neutral = _lesson(t, tags=["cooking"])
        tagless = _lesson(t, tags=[])
        # Clear the auto-filled pillars to simulate pre-PR9 rows.
        Lesson.objects.update(pillar="")
        backfill(global_apps, None)
        self.assertEqual(Lesson.objects.get(id=finance.id).pillar, "gravity")
        self.assertEqual(Lesson.objects.get(id=neutral.id).pillar, "lessons")
        self.assertEqual(Lesson.objects.get(id=tagless.id).pillar, "")  # tagless stays blank


# ── Caps ──────────────────────────────────────────────────────────────────────


class NeighborCapTest(TestCase):
    @mock.patch("apps.friends.services.MAX_NEIGHBORS", 2)
    def test_wave_send_blocked_at_cap(self):
        me = _tenant("nc_me")
        _profile(me, "ncme")
        for i in range(2):
            _accepted(me, _tenant(f"nc_f{i}"))
        target = _tenant("nc_target")
        _profile(target, "nctarget")
        with self.assertRaises(ValidationError):
            services.send_wave(me, me.user, "nctarget")

    @mock.patch("apps.friends.services.MAX_NEIGHBORS", 2)
    def test_wave_send_allowed_under_cap(self):
        me = _tenant("nc_me2")
        _profile(me, "ncme2")
        _accepted(me, _tenant("nc_one"))
        target = _tenant("nc_t2")
        _profile(target, "nct2")
        friendship, _ = services.send_wave(me, me.user, "nct2")
        self.assertIsNotNone(friendship)

    @mock.patch("apps.friends.services.MAX_NEIGHBORS", 2)
    def test_invite_claim_blocked_at_cap(self):
        inviter = _tenant("nc_inviter")
        claimer = _tenant("nc_claimer")
        for i in range(2):
            _accepted(claimer, _tenant(f"nc_c{i}"))
        invite = services.create_invite(inviter, max_uses=5)
        with self.assertRaises(ValidationError):
            services.claim_invite(claimer, claimer.user, invite.token)


class CircleMemberCapTest(TestCase):
    @mock.patch("apps.friends.circles.MAX_CIRCLE_MEMBERS", 2)
    def test_join_blocked_when_full(self):
        creator = _tenant("cm_creator")
        _profile(creator, "cmcreator")
        joiner1 = _tenant("cm_j1")
        _profile(joiner1, "cmj1")
        joiner2 = _tenant("cm_j2")
        _profile(joiner2, "cmj2")
        for j in (joiner1, joiner2):
            _accepted(creator, j)
        circle = circles.create_circle(creator, creator.user, name="Full")  # creator = member #1
        circles.join_circle(joiner1, joiner1.user, circle.invite_code)  # member #2 → full
        with self.assertRaises(ValidationError):
            circles.join_circle(joiner2, joiner2.user, circle.invite_code)

    @mock.patch("apps.friends.circles.MAX_CIRCLE_MEMBERS", 1)
    def test_add_blocked_when_full(self):
        creator = _tenant("cm_creator2")
        _profile(creator, "cmcreator2")
        neighbor = _tenant("cm_n")
        _profile(neighbor, "cmn")
        _accepted(creator, neighbor)
        circle = circles.create_circle(creator, creator.user, name="Solo")  # already at cap of 1
        with self.assertRaises(ValidationError):
            circles.add_circle_member(creator, creator.user, circle.id, "cmn")


# ── friends_agent_propose_enabled split ───────────────────────────────────────


@override_settings(NBHD_INTERNAL_API_KEY="shared-key")
class ProposeFlagRuntimeTest(TestCase):
    def _headers(self, tenant):
        return {"HTTP_X_NBHD_INTERNAL_KEY": "shared-key", "HTTP_X_NBHD_TENANT_ID": str(tenant.id)}

    def test_propose_share_403_when_flag_off(self):
        a = seed_internal_key(_tenant("pf_off", propose_enabled=False))
        lesson = _lesson(a)
        resp = self.client.post(
            f"{_RUNTIME}/{a.id}/lessons/{lesson.id}/propose-share/",
            {"target_handle": "whoever"},
            content_type="application/json",
            **self._headers(a),
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"], "propose_disabled")

    def test_propose_share_allowed_when_flag_on(self):
        a = seed_internal_key(_tenant("pf_on", propose_enabled=True))
        b = _tenant("pf_on_b")
        _profile(b, "pfonb")
        _accepted(a, b)
        lesson = _lesson(a)
        with mock.patch("apps.friends.services._enqueue_scrub"):
            resp = self.client.post(
                f"{_RUNTIME}/{a.id}/lessons/{lesson.id}/propose-share/",
                {"target_handle": "pfonb"},
                content_type="application/json",
                **self._headers(a),
            )
        self.assertEqual(resp.status_code, 201)

    def test_propose_task_403_when_flag_off(self):
        import uuid

        a = seed_internal_key(_tenant("pf_off_m", propose_enabled=False))
        resp = self.client.post(
            f"{_RUNTIME}/{a.id}/missions/{uuid.uuid4()}/propose-task/",
            {"title": "x"},
            content_type="application/json",
            **self._headers(a),
        )
        self.assertEqual(resp.status_code, 403)


class ProposeFlagAgentsMdTest(TestCase):
    def test_absorb_only_variant_omits_propose_tools(self):
        tenant = _tenant("md_off", propose_enabled=False)
        md = render_workspace_files("neighbor", tenant=tenant)["NBHD_AGENTS_MD"]
        self.assertIn("ABSORB", md)
        self.assertNotIn("nbhd_propose_lesson_share", md)
        self.assertNotIn("nbhd_propose_mission_task", md)
        self.assertIn("do NOT propose", md)

    def test_propose_variant_includes_propose_tools(self):
        tenant = _tenant("md_on", propose_enabled=True)
        md = render_workspace_files("neighbor", tenant=tenant)["NBHD_AGENTS_MD"]
        self.assertIn("nbhd_propose_lesson_share", md)
        self.assertIn("nbhd_propose_mission_task", md)

    def test_no_gate_when_friends_disabled(self):
        tenant = _tenant("md_none", friends_enabled=False)
        md = render_workspace_files("neighbor", tenant=tenant)["NBHD_AGENTS_MD"]
        self.assertNotIn("Neighborhood — you are BACKSTAGE", md)


class ProposeFlagPluginConfigTest(TestCase):
    def _friends_entry_config(self, tenant):
        from apps.orchestrator.config_generator import generate_openclaw_config

        config = generate_openclaw_config(tenant)
        entries = config.get("plugins", {}).get("entries", {})
        return entries.get("nbhd-friends-tools", {}).get("config", {})

    def test_propose_enabled_flag_flows_to_plugin_config(self):
        off = _tenant("pc_off", propose_enabled=False)
        on = _tenant("pc_on", propose_enabled=True)
        self.assertFalse(self._friends_entry_config(off).get("proposeEnabled"))
        self.assertTrue(self._friends_entry_config(on).get("proposeEnabled"))
