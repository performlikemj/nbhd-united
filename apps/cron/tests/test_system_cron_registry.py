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
        names = {name for name, _cron, _path in reg_cmd.SYSTEM_CRONS}
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

        for name, cron_expr, path in reg_cmd.SYSTEM_CRONS:
            m = self.TRIGGER_RE.match(path)
            if not m:
                continue  # dedicated-view endpoint, not a trigger task
            self.assertIn(
                m.group("name"),
                TASK_MAP,
                msg=f"SYSTEM_CRONS entry {name!r} → {path!r} has no TASK_MAP task",
            )

    def test_names_and_paths_are_unique(self):
        names = [name for name, _e, _p in reg_cmd.SYSTEM_CRONS]
        paths = [path for _n, _e, path in reg_cmd.SYSTEM_CRONS]
        self.assertEqual(len(names), len(set(names)), "duplicate SYSTEM_CRONS name")
        self.assertEqual(len(paths), len(set(paths)), "duplicate SYSTEM_CRONS path")

    def test_cron_exprs_have_five_fields(self):
        for name, cron_expr, _path in reg_cmd.SYSTEM_CRONS:
            self.assertEqual(
                len(cron_expr.split()),
                5,
                msg=f"SYSTEM_CRONS entry {name!r} has a malformed cron expr {cron_expr!r}",
            )

    def test_wave_b_eval_probes_are_scheduled(self):
        """The five Wave B eval probes (PR-B6) are wired with the planned exprs."""
        by_name = {name: (cron_expr, path) for name, cron_expr, path in reg_cmd.SYSTEM_CRONS}
        expected = {
            "eval-journey-chat": ("*/30 * * * *", "/api/cron/trigger/eval_journey_chat/"),
            "eval-journey-journal": ("5 5 * * *", "/api/cron/trigger/eval_journey_journal/"),
            "eval-journey-wake": ("12 5 * * *", "/api/cron/trigger/eval_journey_wake/"),
            "eval-journey-cron": ("20 5 * * *", "/api/cron/trigger/eval_journey_cron/"),
            "reap-stuck-eval-runs": ("35 5 * * *", "/api/cron/trigger/reap_stuck_eval_runs/"),
        }
        for name, (cron_expr, path) in expected.items():
            self.assertIn(name, by_name, msg=f"{name} not scheduled in SYSTEM_CRONS")
            self.assertEqual(by_name[name], (cron_expr, path))

    def test_daily_eval_probes_avoid_the_chat_probe_fire_boundary(self):
        """The chat probe fires at :00 and :30 every hour. No daily eval probe
        may share those minutes, else it races the chat probe (the wake probe
        force-hibernates the tenant — a concurrent chat fire would wake it
        mid-test). Probe-1↔Probe-4 race, plan §Green-theater."""
        chat_minutes = {0, 30}
        for name, cron_expr, _path in reg_cmd.SYSTEM_CRONS:
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
