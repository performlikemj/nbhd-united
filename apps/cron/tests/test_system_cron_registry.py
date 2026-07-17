"""Tests for the shared system-cron registration core.

Covers ``apps.cron.system_cron_registry.sync_system_crons`` (register / update /
skip / deregister / failed branches over a mocked QStash HTTP surface) and that
BOTH entry points delegate to it: the ``X-Deploy-Secret`` view and the
QStash-signed ``reconcile_system_crons`` task.

QStash is never touched — ``httpx.get/post/delete`` are mocked, and
``SYSTEM_CRONS`` / ``RETIRED_CRON_PATHS`` are patched to small deterministic
lists so each branch is exercised in isolation.
"""

from __future__ import annotations

import re
from unittest import mock

from django.test import TestCase, override_settings

from apps.cron import system_cron_registry as registry
from apps.cron.management.commands import register_system_crons as reg_cmd

BASE = "https://app.test"

# name, cron_expr, path — one per register-loop branch.
TEST_CRONS = [
    ("keep-me", "0 0 * * *", "/api/cron/trigger/keep_me/"),  # already at desired cron → skip
    ("change-me", "5 0 * * *", "/api/cron/trigger/change_me/"),  # cron differs → delete+recreate (update)
    ("add-me", "0 1 * * *", "/api/cron/trigger/add_me/"),  # absent → register
]
TEST_RETIRED = ["/api/cron/trigger/gone/"]


def _resp(status: int = 200, json_data=None):
    r = mock.Mock()
    r.status_code = status
    r.json.return_value = json_data if json_data is not None else []
    r.text = ""
    r.raise_for_status.return_value = None
    return r


def _existing_schedules():
    """QStash 'list schedules' payload: keep-me at its desired cron, change-me at
    a stale cron, add-me absent, plus a live retired schedule to deregister."""
    return [
        {"destination": f"{BASE}/api/cron/trigger/keep_me/", "cron": "0 0 * * *", "scheduleId": "s-keep"},
        {"destination": f"{BASE}/api/cron/trigger/change_me/", "cron": "9 9 * * *", "scheduleId": "s-change"},
        {"destination": f"{BASE}/api/cron/trigger/gone/", "cron": "0 3 * * *", "scheduleId": "s-gone"},
    ]


@override_settings(QSTASH_TOKEN="tok", DJANGO_BASE_URL=BASE)
class SyncSystemCronsTests(TestCase):
    def test_register_update_skip_deregister(self):
        with (
            mock.patch.object(reg_cmd, "SYSTEM_CRONS", TEST_CRONS),
            mock.patch.object(reg_cmd, "RETIRED_CRON_PATHS", TEST_RETIRED),
            mock.patch("httpx.get", return_value=_resp(json_data=_existing_schedules())) as m_get,
            mock.patch("httpx.post", return_value=_resp(status=201)) as m_post,
            mock.patch("httpx.delete", return_value=_resp(status=200)) as m_delete,
        ):
            result = registry.sync_system_crons(BASE)

        self.assertEqual(result["skipped"], ["keep-me"])
        self.assertEqual(result["updated"], ["change-me"])
        self.assertEqual(result["registered"], ["add-me"])
        self.assertEqual(result["deregistered"], ["/api/cron/trigger/gone/"])
        self.assertEqual(result["failed"], [])
        # Two deletes: change-me (stale) + gone (retired). Two posts: change-me
        # recreate + add-me register. One GET for the schedules list.
        self.assertEqual(m_delete.call_count, 2)
        self.assertEqual(m_post.call_count, 2)
        m_get.assert_called_once()

    def test_trailing_slash_in_base_url_is_stripped(self):
        with (
            mock.patch.object(reg_cmd, "SYSTEM_CRONS", []),
            mock.patch.object(reg_cmd, "RETIRED_CRON_PATHS", []),
            mock.patch("httpx.get", return_value=_resp(json_data=[])),
            mock.patch("httpx.post"),
            mock.patch("httpx.delete"),
        ):
            # Should not raise and should return empty lists regardless of the
            # trailing slash — the loop just has nothing to do.
            result = registry.sync_system_crons(BASE + "/")
        self.assertEqual(result["registered"], [])

    def test_create_failure_counts_as_failed(self):
        with (
            mock.patch.object(reg_cmd, "SYSTEM_CRONS", [("add-me", "0 1 * * *", "/api/cron/trigger/add_me/")]),
            mock.patch.object(reg_cmd, "RETIRED_CRON_PATHS", []),
            mock.patch("httpx.get", return_value=_resp(json_data=[])),
            mock.patch("httpx.post", return_value=_resp(status=500)),
            mock.patch("httpx.delete", return_value=_resp(status=200)),
        ):
            result = registry.sync_system_crons(BASE)
        self.assertEqual(result["failed"], ["add-me"])
        self.assertEqual(result["registered"], [])

    def test_missing_qstash_token_raises(self):
        with override_settings(QSTASH_TOKEN=""), self.assertRaises(registry.SystemCronConfigError):
            registry.sync_system_crons(BASE)

    def test_four_tuple_retries_sets_upstash_retries_header_on_register(self):
        # A 4-tuple entry (retries=0) with no live schedule → registered, and the
        # create POST carries Upstash-Retries: "0".
        crons = [("cap-me", "0 1 * * *", "/api/cron/trigger/cap_me/", 0)]
        with (
            mock.patch.object(reg_cmd, "SYSTEM_CRONS", crons),
            mock.patch.object(reg_cmd, "RETIRED_CRON_PATHS", []),
            mock.patch("httpx.get", return_value=_resp(json_data=[])),
            mock.patch("httpx.post", return_value=_resp(status=201)) as m_post,
            mock.patch("httpx.delete"),
        ):
            result = registry.sync_system_crons(BASE)
        self.assertEqual(result["registered"], ["cap-me"])
        _args, kwargs = m_post.call_args
        self.assertEqual(kwargs["headers"].get("Upstash-Retries"), "0")

    def test_three_tuple_entry_omits_retries_header(self):
        # A legacy 3-tuple entry sends NO Upstash-Retries header (QStash default 3).
        crons = [("plain", "0 1 * * *", "/api/cron/trigger/plain/")]
        with (
            mock.patch.object(reg_cmd, "SYSTEM_CRONS", crons),
            mock.patch.object(reg_cmd, "RETIRED_CRON_PATHS", []),
            mock.patch("httpx.get", return_value=_resp(json_data=[])),
            mock.patch("httpx.post", return_value=_resp(status=201)) as m_post,
            mock.patch("httpx.delete"),
        ):
            result = registry.sync_system_crons(BASE)
        self.assertEqual(result["registered"], ["plain"])
        _args, kwargs = m_post.call_args
        self.assertNotIn("Upstash-Retries", kwargs["headers"])

    def test_retries_drift_recreates_then_matching_retries_skips(self):
        # Same cron but the live schedule still carries QStash's default retries=3
        # while the entry pins 0 → counts as a change → delete+recreate with the cap.
        crons = [("cap-me", "0 0 * * *", "/api/cron/trigger/cap_me/", 0)]
        stale = [
            {
                "destination": f"{BASE}/api/cron/trigger/cap_me/",
                "cron": "0 0 * * *",
                "retries": 3,
                "scheduleId": "s-cap",
            }
        ]
        with (
            mock.patch.object(reg_cmd, "SYSTEM_CRONS", crons),
            mock.patch.object(reg_cmd, "RETIRED_CRON_PATHS", []),
            mock.patch("httpx.get", return_value=_resp(json_data=stale)),
            mock.patch("httpx.post", return_value=_resp(status=201)) as m_post,
            mock.patch("httpx.delete", return_value=_resp(status=200)) as m_delete,
        ):
            result = registry.sync_system_crons(BASE)
        self.assertEqual(result["updated"], ["cap-me"])
        m_delete.assert_called_once()
        _args, kwargs = m_post.call_args
        self.assertEqual(kwargs["headers"].get("Upstash-Retries"), "0")

        # Once the live schedule already carries retries=0, the same entry is a
        # no-op (idempotent after the first sync) — no delete, no recreate.
        settled = [
            {
                "destination": f"{BASE}/api/cron/trigger/cap_me/",
                "cron": "0 0 * * *",
                "retries": 0,
                "scheduleId": "s-cap",
            }
        ]
        with (
            mock.patch.object(reg_cmd, "SYSTEM_CRONS", crons),
            mock.patch.object(reg_cmd, "RETIRED_CRON_PATHS", []),
            mock.patch("httpx.get", return_value=_resp(json_data=settled)),
            mock.patch("httpx.post") as m_post2,
            mock.patch("httpx.delete") as m_delete2,
        ):
            result = registry.sync_system_crons(BASE)
        self.assertEqual(result["skipped"], ["cap-me"])
        m_post2.assert_not_called()
        m_delete2.assert_not_called()


@override_settings(QSTASH_TOKEN="tok", DJANGO_BASE_URL=BASE)
class ReconcileTaskTests(TestCase):
    def test_task_delegates_to_sync(self):
        sentinel = {"registered": ["x"], "updated": [], "skipped": [], "failed": [], "deregistered": []}
        with mock.patch.object(registry, "sync_system_crons", return_value=sentinel) as m_sync:
            out = registry.reconcile_system_crons_task()
        m_sync.assert_called_once_with(BASE)
        self.assertEqual(out, sentinel)

    def test_task_skips_gracefully_when_base_url_unset(self):
        with override_settings(DJANGO_BASE_URL=""), mock.patch.object(registry, "sync_system_crons") as m_sync:
            out = registry.reconcile_system_crons_task()
        m_sync.assert_not_called()
        self.assertEqual(out["status"], "skipped")

    def test_task_registered_in_task_map(self):
        from apps.cron.views import TASK_MAP

        self.assertEqual(
            TASK_MAP["reconcile_system_crons"],
            "apps.cron.system_cron_registry.reconcile_system_crons_task",
        )

    def test_reconcile_cron_registered_in_system_crons(self):
        names = {name for name, _c, _p, _r in reg_cmd.iter_system_crons()}
        self.assertIn("reconcile-system-crons", names)
        entry = next(e for e in reg_cmd.SYSTEM_CRONS if e[0] == "reconcile-system-crons")
        self.assertEqual(entry[2], "/api/cron/trigger/reconcile_system_crons/")


class SystemCronsWellFormednessTests(TestCase):
    """SYSTEM_CRONS structural invariants — no QStash involved.

    Guards the failure mode where a schedule points at a task name that does
    not exist: sync_system_crons would happily register a QStash schedule whose
    fires all 404 at ``trigger_task``. Every ``/api/cron/trigger/<name>/`` entry
    must resolve to a TASK_MAP-registered task. Dedicated-view paths
    (apply-pending-configs, expire-trials) are not trigger tasks and are skipped
    by the same regex the router uses.
    """

    TRIGGER_RE = re.compile(r"^/api/cron/trigger/(?P<name>[a-z0-9_]+)/$")

    def test_every_trigger_path_resolves_to_a_task_map_entry(self):
        from apps.cron.views import TASK_MAP

        for name, cron_expr, path, _retries in reg_cmd.iter_system_crons():
            m = self.TRIGGER_RE.match(path)
            if not m:
                continue  # dedicated-view endpoint, not a trigger task
            self.assertIn(
                m.group("name"),
                TASK_MAP,
                msg=f"SYSTEM_CRONS entry {name!r} → {path!r} has no TASK_MAP task",
            )

    def test_names_and_paths_are_unique(self):
        names = [name for name, _e, _p, _r in reg_cmd.iter_system_crons()]
        paths = [path for _n, _e, path, _r in reg_cmd.iter_system_crons()]
        self.assertEqual(len(names), len(set(names)), "duplicate SYSTEM_CRONS name")
        self.assertEqual(len(paths), len(set(paths)), "duplicate SYSTEM_CRONS path")

    def test_cron_exprs_have_five_fields(self):
        for name, cron_expr, _path, _retries in reg_cmd.iter_system_crons():
            self.assertEqual(
                len(cron_expr.split()),
                5,
                msg=f"SYSTEM_CRONS entry {name!r} has a malformed cron expr {cron_expr!r}",
            )

    def test_stale_app_chat_reaper_is_registered_every_five_minutes(self):
        from apps.cron.views import TASK_MAP

        entry = next(e for e in reg_cmd.SYSTEM_CRONS if e[0] == "reap-stale-app-chat-messages")
        self.assertEqual(entry[1], "*/5 * * * *")
        self.assertEqual(entry[2], "/api/cron/trigger/reap_stale_app_chat_messages/")
        self.assertEqual(
            TASK_MAP["reap_stale_app_chat_messages"],
            "apps.router.pending_queue.reap_stale_app_chat_messages_task",
        )

    def test_finished_at_cron_retirement_is_scheduled_hourly(self):
        from apps.cron.views import TASK_MAP

        by_name = {name: (cron_expr, path, retries) for name, cron_expr, path, retries in reg_cmd.iter_system_crons()}
        self.assertIn("expire-finished-at-crons", by_name)
        self.assertEqual(
            by_name["expire-finished-at-crons"],
            ("0 * * * *", "/api/cron/trigger/expire_finished_at_crons/", None),
        )
        self.assertEqual(
            TASK_MAP["expire_finished_at_crons"],
            "apps.cron.tasks.expire_finished_at_crons_task",
        )

    def test_wave_b_eval_probes_are_scheduled(self):
        """The five Wave B eval probes (PR-B6) are wired with the planned exprs.

        Each carries an explicit ``retries=0`` (the optional 4th tuple element):
        finalize_task_run alerts the owner + DLQs on the FIRST failing run, so
        QStash's default 3 retries would only re-run the whole probe. The reaper is
        HOURLY at :50 (not the original daily 05:35) so a SIGKILL-stranded run is
        surfaced within ~2h, not ~24h. See the PR-B6 review fixes.
        """
        by_name = {name: (cron_expr, path, retries) for name, cron_expr, path, retries in reg_cmd.iter_system_crons()}
        expected = {
            "eval-journey-chat": ("*/30 * * * *", "/api/cron/trigger/eval_journey_chat/", 0),
            "eval-journey-journal": ("5 5 * * *", "/api/cron/trigger/eval_journey_journal/", 0),
            "eval-journey-wake": ("12 5 * * *", "/api/cron/trigger/eval_journey_wake/", 0),
            "eval-journey-cron": ("20 5 * * *", "/api/cron/trigger/eval_journey_cron/", 0),
            "reap-stuck-eval-runs": ("50 * * * *", "/api/cron/trigger/reap_stuck_eval_runs/", 0),
        }
        for name, expected_tuple in expected.items():
            self.assertIn(name, by_name, msg=f"{name} not scheduled in SYSTEM_CRONS")
            self.assertEqual(by_name[name], expected_tuple)

    def test_wave_d_e_eval_schedules_are_scheduled(self):
        """The nightly behavior suite (Wave D) and the SLO snapshot + weekly digest
        (Wave E) are wired with the planned exprs — the exact-tuple lock that mirrors
        test_wave_b_eval_probes_are_scheduled and closes out the eval program.

        The behavior and snapshot suites retain ``retries=0`` because rerunning their
        full probes multiplies failure alerts. The digest uses ``retries=2`` because a
        failed mail delivery now raises and two bounded retries can heal a transient
        provider blip without falling back to QStash's larger default.
        """
        by_name = {name: (cron_expr, path, retries) for name, cron_expr, path, retries in reg_cmd.iter_system_crons()}
        expected = {
            "eval-behavior": ("40 5 * * *", "/api/cron/trigger/eval_behavior/", 0),
            "slo-snapshot": ("55 5 * * *", "/api/cron/trigger/slo_snapshot/", 0),
            "weekly-slo-digest": ("15 6 * * 1", "/api/cron/trigger/weekly_slo_digest/", 2),
        }
        for name, expected_tuple in expected.items():
            self.assertIn(name, by_name, msg=f"{name} not scheduled in SYSTEM_CRONS")
            self.assertEqual(by_name[name], expected_tuple)

    def test_nightly_eval_suites_stay_off_the_chat_and_journey_probe_fires(self):
        """Stagger discipline for the Wave D/E nightly fires: none may land on a
        :00/:30 chat-probe minute, and the two 05:xx suites must not collide with the
        existing 05:xx journey probes (05:05/05:12/05:20) or the hourly :50 reaper.

        These run against the BEHAVIOR tenant / read metadata (not the journey
        tenant), so they can't race the journey probes at the tenant level — this is
        shared-control-plane-worker hygiene, hence a minute-stagger check rather than
        the journey probes' wall-clock-window disjointness test above."""
        by_name = {name: cron_expr for name, cron_expr, _p, _r in reg_cmd.iter_system_crons()}
        # (1) No Wave D/E fire lands on a :00/:30 chat-probe minute.
        for name in ("eval-behavior", "slo-snapshot", "weekly-slo-digest"):
            self.assertIn(name, by_name, msg=f"{name} missing from SYSTEM_CRONS")
            minute_field = by_name[name].split()[0]
            self.assertTrue(minute_field.isdigit(), msg=f"{name} must fire at a fixed minute")
            self.assertNotIn(int(minute_field), {0, 30}, msg=f"{name} fires on a :00/:30 chat-probe minute")
        # (2) The two nightly-at-05:xx suites avoid the existing 05:xx eval fires.
        taken_at_05 = {0, 5, 12, 20, 50}  # cleanup-inbound-media + 3 journey probes + hourly reaper
        for name in ("eval-behavior", "slo-snapshot"):
            minute_field, hour_field = by_name[name].split()[0], by_name[name].split()[1]
            if hour_field == "5":
                self.assertNotIn(int(minute_field), taken_at_05, msg=f"{name} collides with an existing 05:xx fire")

    def test_daily_eval_probes_avoid_the_chat_probe_fire_boundary(self):
        """The chat probe fires at :00 and :30 every hour. No daily eval probe
        may share those minutes, else it races the chat probe (the wake probe
        force-hibernates the tenant — a concurrent chat fire would wake it
        mid-test). Probe-1↔Probe-4 race, plan §Green-theater."""
        chat_minutes = {0, 30}
        for name, cron_expr, _path, _retries in reg_cmd.iter_system_crons():
            if not name.startswith(("eval-journey-", "reap-stuck-eval")):
                continue
            minute_field = cron_expr.split()[0]
            if minute_field.startswith("*"):
                continue  # the chat probe itself (*/30)
            self.assertNotIn(
                int(minute_field),
                chat_minutes,
                msg=f"{name} fires on a chat-probe minute ({minute_field})",
            )

    def test_daily_eval_probe_windows_are_disjoint_from_each_other_and_the_chat_fires(self):
        """Stronger than the minute-set guard above: build each DAILY probe's real
        ``[fire, fire + worst_case_runtime + margin]`` wall-clock window from the
        SAME deadline constants the suites run on, then assert those windows are
        pairwise disjoint AND clear of every :00/:30 chat window (chat fire + its
        poll deadline). A minute-set check passes ``"29 5"`` even though its runtime
        crosses the 05:30 chat fire — this catches that, and it FAILS if someone
        edits a probe's deadline constant into a collision."""
        from apps.evals.journey.chat_drive import DEFAULT_DEADLINE_SECONDS
        from apps.evals.journey.wake_control import _WALL_CLOCK_BUDGET_SECONDS
        from apps.evals.suites.journey_cron import POLL_BUDGET_SECONDS
        from apps.evals.suites.journey_journal import _HTTP_TIMEOUT_S
        from apps.evals.suites.journey_wake import WAKE_DEADLINE_SECONDS

        margin_s = 60  # pad beyond each documented worst case

        # Worst-case wall-clock runtime per daily probe, each DERIVED from its own
        # suite constants (never a re-hardcoded literal, so a drift in the suite
        # drifts this test with it):
        #   journal: a sequential PUT + GET, each bounded by _HTTP_TIMEOUT_S.
        #   wake:    the hibernate-precondition budget + the wake poll deadline.
        #   cron:    the total arm+poll budget measured from suite start.
        worst_case = {
            "eval-journey-journal": 2 * _HTTP_TIMEOUT_S,
            "eval-journey-wake": _WALL_CLOCK_BUDGET_SECONDS + WAKE_DEADLINE_SECONDS,
            "eval-journey-cron": POLL_BUDGET_SECONDS,
        }

        by_name = {name: cron_expr for name, cron_expr, _p, _r in reg_cmd.iter_system_crons()}

        def fire_seconds(cron_expr):
            minute_f, hour_f = cron_expr.split()[0], cron_expr.split()[1]
            self.assertTrue(
                minute_f.isdigit() and hour_f.isdigit(),
                msg=f"daily probe expr {cron_expr!r} must fire at a fixed HH:MM",
            )
            return int(hour_f) * 3600 + int(minute_f) * 60

        # Each daily probe's [start, end) window in seconds-of-day.
        windows = {}
        for name, runtime in worst_case.items():
            self.assertIn(name, by_name, msg=f"{name} missing from SYSTEM_CRONS")
            start = fire_seconds(by_name[name])
            windows[name] = (start, start + runtime + margin_s)

        def overlaps(a, b):
            return a[0] < b[1] and b[0] < a[1]

        # (1) Pairwise disjoint among the daily probes themselves.
        names = list(windows)
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                a, b = windows[names[i]], windows[names[j]]
                self.assertFalse(overlaps(a, b), msg=f"{names[i]} window {a} overlaps {names[j]} window {b}")

        # (2) Disjoint from every :00/:30 chat window across the whole day.
        chat_end_offset = DEFAULT_DEADLINE_SECONDS + margin_s
        chat_windows = [(h * 3600 + m * 60, h * 3600 + m * 60 + chat_end_offset) for h in range(24) for m in (0, 30)]
        for name, w in windows.items():
            for cw in chat_windows:
                self.assertFalse(overlaps(w, cw), msg=f"{name} window {w} overlaps chat fire window {cw}")


class ViewDelegationTests(TestCase):
    @override_settings(QSTASH_TOKEN="tok", DEPLOY_SECRET="sekret", DJANGO_BASE_URL=BASE)
    def test_view_delegates_to_sync_and_returns_result(self):
        sentinel = {"registered": [], "updated": [], "skipped": ["keep-me"], "failed": [], "deregistered": []}
        with mock.patch("apps.cron.system_cron_registry.sync_system_crons", return_value=sentinel) as m_sync:
            resp = self.client.post(
                "/api/cron/register-system-crons/",
                data="{}",
                content_type="application/json",
                HTTP_X_DEPLOY_SECRET="sekret",
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), sentinel)
        # base_url falls back to settings.DJANGO_BASE_URL when the body omits it.
        m_sync.assert_called_once_with(BASE)

    @override_settings(QSTASH_TOKEN="", DEPLOY_SECRET="sekret", DJANGO_BASE_URL=BASE)
    def test_view_returns_503_when_qstash_token_unset(self):
        # Real sync raises SystemCronConfigError; the view maps it to 503.
        resp = self.client.post(
            "/api/cron/register-system-crons/",
            data="{}",
            content_type="application/json",
            HTTP_X_DEPLOY_SECRET="sekret",
        )
        self.assertEqual(resp.status_code, 503)

    @override_settings(DEPLOY_SECRET="sekret")
    def test_view_rejects_bad_deploy_secret(self):
        resp = self.client.post(
            "/api/cron/register-system-crons/",
            data="{}",
            content_type="application/json",
            HTTP_X_DEPLOY_SECRET="wrong",
        )
        self.assertEqual(resp.status_code, 401)
