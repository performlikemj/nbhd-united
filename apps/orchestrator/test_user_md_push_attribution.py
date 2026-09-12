"""Offline attribution and canary-gated USER.md skip-unchanged contracts."""

import ast
import io
import threading
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch
from uuid import UUID

from django.test import SimpleTestCase, override_settings

from apps.orchestrator import workspace_envelope as envelope
from apps.orchestrator.azure_client import sanitize_share_text
from apps.tenants.models import Tenant

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
TENANT_ID = "148ccf1c-ef13-47f8-ada1-a98fa90e14a0"


def snapshot(*, stamp=None, clock="Friday, September 11, 2026 at 12:00", content="## Tasks\n- Walk"):
    stamp = (NOW - timedelta(seconds=30)).isoformat() if stamp is None else stamp
    return "\n".join(
        [
            envelope.BEGIN_MARKER,
            "",
            "# Pre-loaded user state",
            "",
            f"_Current local time: {clock} (UTC)_",
            f"_Last refreshed: {stamp}_",
            "",
            envelope._SYNTHESIS_HINT,
            "",
            content,
            "",
            envelope.END_MARKER,
            "",
        ]
    )


class ComparatorTests(SimpleTestCase):
    def test_truth_table(self):
        old = snapshot()
        fresh = snapshot(stamp=NOW.isoformat(), clock="Friday, September 11, 2026 at 12:01")
        cases = [
            ("identical", old, old, "equal"),
            ("timestamps only", old, fresh, "equal"),
            ("same size meaning", old, fresh.replace("Walk", "Rest"), "different"),
            ("unmanaged null repair", old + "note\x00\n", fresh + "note\n", "different"),
            ("replacement char", old + "\ufffd", fresh, "unknown"),
            ("replacement proposed", old, fresh + "\ufffd", "unknown"),
            ("duplicate begin", old + envelope.BEGIN_MARKER, fresh, "unknown"),
            ("duplicate end", old + envelope.END_MARKER, fresh, "unknown"),
            ("duplicate block", old + old, fresh, "unknown"),
            ("missing begin", old.replace(envelope.BEGIN_MARKER, ""), fresh, "unknown"),
            ("missing end", old.replace(envelope.END_MARKER, ""), fresh, "unknown"),
            ("missing proposed markers", old, "plain", "unknown"),
            ("missing file", "", fresh, "unknown"),
            (
                "section timestamp",
                snapshot(content="_Last refreshed: yesterday_"),
                snapshot(content="_Last refreshed: today_"),
                "different",
            ),
            (
                "section clock",
                snapshot(content="_Current local time: noon_"),
                snapshot(content="_Current local time: midnight_"),
                "different",
            ),
            ("crlf", old.replace("\n", "\r\n"), fresh, "different"),
            ("crlf timestamps only", old.replace("\n", "\r\n"), fresh.replace("\n", "\r\n"), "equal"),
            ("timestamp newline preserved", old.replace("(UTC)_\n", "(UTC)_\r\n"), fresh, "different"),
            ("missing final newline", old[:-1], fresh, "different"),
            ("extra newline", old + "\n", fresh, "different"),
            ("section whitespace", old.replace("- Walk", "- Walk "), fresh, "different"),
            ("sentinel indent", " " + old, fresh, "unknown"),
            ("sentinel trailing space", old.replace(envelope.END_MARKER, envelope.END_MARKER + " "), fresh, "unknown"),
            ("sentinel internal space", old.replace("BEGIN: NBHD", "BEGIN:  NBHD"), fresh, "unknown"),
            ("header indent", old.replace("# Pre-loaded", " # Pre-loaded"), fresh, "unknown"),
            ("header blank whitespace", old.replace("\n\n#", "\n \n#"), fresh, "unknown"),
            ("record indent", old.replace("_Last refreshed", " _Last refreshed"), fresh, "unknown"),
            ("record trailing whitespace", old.replace("(UTC)_", "(UTC)_ "), fresh, "unknown"),
            ("missing clock", old.replace("_Current local time:", "_Time:"), fresh, "unknown"),
            ("malformed clock", old.replace("Friday, September", "bad"), fresh, "unknown"),
            ("malformed refresh", snapshot(stamp="invalid"), fresh, "unknown"),
            ("midnight rollover", snapshot(content="Today: Friday"), snapshot(content="Today: Saturday"), "different"),
            ("lookback expiry", snapshot(content="Recent: walk"), snapshot(content="Recent: none"), "different"),
            ("situation decay", snapshot(content="Away: Kyoto"), snapshot(content="At home"), "different"),
            ("commitment eligibility", snapshot(content="Commitment: active"), snapshot(content=""), "different"),
            ("due window", snapshot(content="Due: later"), snapshot(content="Due: now"), "different"),
            ("preserved prefix", "notes\n" + old, "notes\n" + fresh, "equal"),
            ("prefix changed", "notes\n" + old, "other\n" + fresh, "different"),
            ("suffix changed", old + "notes", fresh + "other", "different"),
        ]
        for name, existing, proposed, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(envelope._compare_user_md(existing.encode(), proposed.encode()), expected)

    def test_real_renderer_header_is_recognized(self):
        tenant = SimpleNamespace(id=TENANT_ID, user=SimpleNamespace(timezone="UTC"))
        with (
            patch("apps.pii.redactor.RedactionSession") as session,
            patch.object(envelope, "all_sections", return_value=[]),
        ):
            session.return_value.entity_map = {}
            rendered = envelope.render_managed_region(tenant).encode()
        self.assertEqual(envelope._compare_user_md(rendered, rendered), "equal")
        self.assertGreaterEqual(envelope._user_md_age_s(rendered, datetime.now(UTC)), 0)

    def test_compare_errors_are_unknown(self):
        self.assertEqual(envelope._compare_user_md(None, b""), "unknown")
        with patch.object(envelope, "_user_md_header", side_effect=ValueError("private content")):
            self.assertEqual(envelope._compare_user_md(b"", b""), "unknown")

    def test_age_truth_table(self):
        for stamp, expected in [
            ("2026-09-11T11:59:30+00:00", 30),
            ("2026-09-11T20:59:30+09:00", 30),
            ("2026-09-11T11:59:30Z", 30),
            (NOW.isoformat(), 0),
            ("2026-09-11T11:00:00+00:00", 3600),
            ("2026-09-11T12:00:00.000001+00:00", -1),
            ("2026-09-12T12:00:00+00:00", -1),
            ("2026-09-11T11:59:30", -1),
            ("nonsense", -1),
            ("2026-99-11T11:59:30+00:00", -1),
        ]:
            with self.subTest(stamp=stamp):
                self.assertEqual(envelope._user_md_age_s(snapshot(stamp=stamp).encode(), NOW), expected)
        self.assertEqual(envelope._user_md_age_s(b"", NOW), -1)
        self.assertEqual(envelope._user_md_age_s(snapshot().encode(), NOW.replace(tzinfo=None)), -1)

    def test_shadow_uses_sanitized_proposal_and_actual_read(self):
        existing = snapshot() + "new unmanaged note\x00\n"
        merged = envelope.merge_into_user_md(existing, snapshot(stamp=NOW.isoformat()))
        with patch.object(envelope, "_compare_user_md", wraps=envelope._compare_user_md) as compare:
            comparison, _ = envelope._user_md_shadow(existing, merged)
        compare.assert_called_once_with(existing.encode(), sanitize_share_text(merged).encode())
        self.assertEqual(comparison, "different")
        clean = existing.replace("\x00", "")
        self.assertEqual(envelope._user_md_shadow(clean, envelope.merge_into_user_md(clean, snapshot()))[0], "equal")


class SkipUnchangedGateTests(SimpleTestCase):
    def test_gate_truth_table(self):
        other = "00000000-0000-0000-0000-000000000001"
        for raw, tenant, expected in [
            ("", TENANT_ID, False),
            (None, TENANT_ID, False),
            (" , \t, ", TENANT_ID, False),
            (TENANT_ID, TENANT_ID, True),
            (TENANT_ID, UUID(TENANT_ID), True),
            (other, TENANT_ID, False),
            ("*", TENANT_ID, True),
            (" invalid, * , ", other, True),
            (f" {other}, \t{TENANT_ID.upper()} , ", TENANT_ID, True),
            (TENANT_ID, TENANT_ID.upper(), True),
            (f"invalid,{TENANT_ID},148ccf1c", TENANT_ID, True),
            ("invalid,148ccf1c", TENANT_ID, False),
            ("invalid", "invalid", False),
            ("148ccf1c", "148ccf1c", False),
            (TENANT_ID.replace("-", ""), TENANT_ID.replace("-", ""), False),
            ("{" + TENANT_ID + "}", "{" + TENANT_ID + "}", False),
            (TENANT_ID[:-1] + "g", TENANT_ID[:-1] + "g", False),
        ]:
            with self.subTest(raw=raw, tenant=tenant), override_settings(USER_MD_SKIP_UNCHANGED_TENANT_IDS=raw):
                self.assertIs(envelope.user_md_skip_unchanged_enabled(tenant), expected)


@override_settings(USER_MD_SKIP_UNCHANGED_TENANT_IDS="")
class PushAttributionTests(SimpleTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        # Isolate every piece of process-local coordination and telemetry state.
        for name, value in {
            "_PUSHES_IN_FLIGHT": set(),
            "_PUSHES_DIRTY": set(),
            "_PUSH_PENDING": {},
            "_PUSH_UNREPORTED": {"debounced": 0, "coalesced": 0},
            "_PUSH_SUMMARY_AT": 1000,
        }.items():
            self.stack.enter_context(patch.object(envelope, name, value))
        self.stack.enter_context(patch.object(envelope.time, "monotonic", return_value=1000))
        self.tenant = Tenant(id=TENANT_ID)
        query = self.stack.enter_context(patch.object(Tenant.objects, "select_related"))
        query.return_value.get.return_value = self.tenant
        self.cache = self.stack.enter_context(patch.object(envelope, "cache"))
        self.cache.get.return_value = False
        self.render = self.stack.enter_context(patch.object(envelope, "render_managed_region", return_value=snapshot()))
        self.download = self.stack.enter_context(
            patch.object(envelope, "download_workspace_file", return_value=snapshot())
        )
        self.upload = self.stack.enter_context(patch.object(envelope, "upload_workspace_file"))
        clock = self.stack.enter_context(patch.object(envelope, "datetime", wraps=datetime))
        clock.now.return_value = NOW
        self.messages = []
        self.logger = self.stack.enter_context(patch.object(envelope, "logger"))
        for level in ("info", "warning", "debug", "exception"):
            getattr(self.logger, level).side_effect = self._log_unlocked

    def _log_unlocked(self, message, *args, **kwargs):
        # Acquiring (rather than inspecting locked()) also catches ownership
        # regressions if a future refactor replaces the underlying lock.
        self.assertTrue(envelope._PUSH_STATE_LOCK.acquire(blocking=False))
        envelope._PUSH_STATE_LOCK.release()
        self.assertNotIn("extra", kwargs)
        self.messages.append(message % args)

    def writes(self):
        return [line for line in self.messages if line.startswith("Pushed USER.md")]

    def assert_released(self):
        self.assertEqual(envelope._PUSHES_IN_FLIGHT, set())
        self.assertEqual(envelope._PUSHES_DIRTY, set())
        self.assertEqual(envelope._PUSH_PENDING, {})

    def follower(self, trigger=envelope.TRIGGER_REGISTRY_SIGNAL, **kwargs):
        info_count = self.logger.info.call_count
        self.assertFalse(envelope.push_user_md(TENANT_ID, debounce_seconds=0, trigger=trigger, **kwargs))
        self.assertEqual(self.logger.info.call_count, info_count)

    @override_settings(USER_MD_SKIP_UNCHANGED_TENANT_IDS=TENANT_ID)
    def test_gated_equal_fresh_snapshot_skips(self):
        for age in (0, 30, 3599):
            with self.subTest(age=age):
                self.messages.clear()
                self.download.return_value = snapshot(stamp=(NOW - timedelta(seconds=age)).isoformat())
                self.render.return_value = snapshot(stamp=NOW.isoformat(), clock="Friday, September 11, 2026 at 12:01")
                self.assertFalse(envelope.push_user_md(self.tenant, trigger=envelope.TRIGGER_FRIENDS))
                self.assertEqual(
                    self.messages,
                    [
                        f"USER.md push not written tenant={TENANT_ID} trigger=friends "
                        f"outcome=unchanged age_s={age} debounced=0 coalesced=0"
                    ],
                )
                self.assertEqual(self.writes(), [])
                self.assert_released()
        self.upload.assert_not_called()

    @override_settings(USER_MD_SKIP_UNCHANGED_TENANT_IDS=TENANT_ID)
    def test_gated_false_comparison_or_age_writes(self):
        for existing, comparison, age in [
            (snapshot(content="## Tasks\n- Rest"), "different", 30),
            (snapshot() + "\ufffd", "unknown", -1),
            (None, "unknown", -1),
            (snapshot(stamp=NOW.replace(tzinfo=None).isoformat()), "equal", -1),
            (snapshot(stamp=(NOW + timedelta(seconds=1)).isoformat()), "equal", -1),
            (snapshot(stamp=(NOW - timedelta(seconds=3600)).isoformat()), "equal", 3600),
            (snapshot(stamp=(NOW - timedelta(seconds=7200)).isoformat()), "equal", 7200),
        ]:
            with self.subTest(comparison=comparison, age=age):
                self.download.return_value = existing
                self.assertTrue(envelope.push_user_md(self.tenant, trigger=envelope.TRIGGER_FRIENDS))
                self.upload.assert_called_with(
                    TENANT_ID, "workspace/USER.md", envelope.merge_into_user_md(existing, snapshot())
                )
                self.assertIn(f"cmp={comparison} age_s={age} eligible=false", self.writes()[-1])
        self.download.return_value = snapshot()
        with patch.object(envelope, "_compare_user_md", return_value="unknown"):
            self.assertTrue(envelope.push_user_md(self.tenant, trigger=envelope.TRIGGER_FRIENDS))
        self.assertIn("cmp=unknown age_s=30 eligible=false", self.writes()[-1])
        self.assertFalse(any("outcome=unchanged" in line for line in self.messages))

    @override_settings(USER_MD_SKIP_UNCHANGED_TENANT_IDS=TENANT_ID)
    def test_gated_forced_freshness_and_unclassified_write(self):
        for trigger in sorted(envelope._FORCED_FRESHNESS_TRIGGERS | {envelope.TRIGGER_UNCLASSIFIED}):
            with self.subTest(trigger=trigger):
                self.assertTrue(envelope.push_user_md(self.tenant, trigger=trigger))
                self.upload.assert_called_with(TENANT_ID, "workspace/USER.md", snapshot())
                self.assertIn("cmp=equal age_s=30 eligible=false", self.writes()[-1])
        self.assertFalse(any("outcome=unchanged" in line for line in self.messages))

    def test_ungated_eligible_snapshot_preserves_payload_and_log(self):
        for allowlist in ("", "00000000-0000-0000-0000-000000000001"):
            with self.subTest(allowlist=allowlist), override_settings(USER_MD_SKIP_UNCHANGED_TENANT_IDS=allowlist):
                self.messages.clear()
                self.upload.reset_mock()
                existing = snapshot() + "unmanaged note\n"
                managed = snapshot(stamp=NOW.isoformat())
                self.download.return_value = existing
                self.render.return_value = managed
                merged = envelope.merge_into_user_md(existing, managed)
                self.assertTrue(envelope.push_user_md(self.tenant, trigger=envelope.TRIGGER_FRIENDS))
                self.upload.assert_called_once_with(TENANT_ID, "workspace/USER.md", merged)
                self.assertEqual(
                    self.messages,
                    [
                        f"Pushed USER.md for tenant {TENANT_ID} ({len(merged)} chars) "
                        "trigger=friends sources=[friends:none] debounced=0 coalesced=0 "
                        "rerun=0 cmp=equal age_s=30 eligible=true"
                    ],
                )

    @override_settings(USER_MD_SKIP_UNCHANGED_TENANT_IDS=TENANT_ID)
    def test_forced_or_unclassified_request_coalesced_into_signal_pass_writes(self):
        for trigger in sorted(envelope._FORCED_FRESHNESS_TRIGGERS | {envelope.TRIGGER_UNCLASSIFIED}):
            with self.subTest(trigger=trigger):
                self.messages.clear()
                self.upload.reset_mock()
                self.render.reset_mock()

                def render(_tenant, trigger=trigger):
                    if self.render.call_count == 1:
                        self.follower(envelope.TRIGGER_REGISTRY_SIGNAL, sender_model="Document")
                        self.follower(trigger)
                    return snapshot()

                self.render.side_effect = render
                self.assertTrue(envelope.push_user_md(self.tenant, trigger=envelope.TRIGGER_FRIENDS))
                self.assertIn("outcome=unchanged", self.messages[0])
                self.assertEqual(self.render.call_count, 2)
                self.upload.assert_called_once_with(TENANT_ID, "workspace/USER.md", snapshot())
                self.assertEqual(len(self.writes()), 1)
                self.assertIn("trigger=registry_signal", self.writes()[0])
                self.assertIn(f"{trigger}:none", self.writes()[0])
                self.assertIn("debounced=0 coalesced=2 rerun=1 cmp=equal age_s=30 eligible=false", self.writes()[0])
                self.assert_released()

    @override_settings(USER_MD_SKIP_UNCHANGED_TENANT_IDS=TENANT_ID)
    def test_gated_read_failure_preserves_warning_and_retry_marker(self):
        self.download.side_effect = TimeoutError("private body")
        self.assertFalse(envelope.push_user_md(self.tenant, trigger=envelope.TRIGGER_FRIENDS))
        self.upload.assert_not_called()
        self.assertEqual(
            self.messages,
            [
                f"Aborted USER.md push for tenant {TENANT_ID} after existing-file read failed with TimeoutError",
                f"USER.md push not written tenant={TENANT_ID} trigger=friends outcome=read_failed",
            ],
        )
        self.cache.delete.assert_called_once_with(f"{envelope._DEBOUNCE_CACHE_PREFIX}{TENANT_ID}")
        self.assert_released()

    @override_settings(USER_MD_SKIP_UNCHANGED_TENANT_IDS=TENANT_ID)
    def test_skip_preserves_debounce_marker(self):
        self.cache.get.side_effect = [False, True]
        self.assertFalse(envelope.push_user_md(self.tenant, trigger=envelope.TRIGGER_FRIENDS))
        self.assertFalse(envelope.push_user_md(self.tenant, trigger=envelope.TRIGGER_FRIENDS))
        self.cache.set.assert_called_once_with(f"{envelope._DEBOUNCE_CACHE_PREFIX}{TENANT_ID}", "1", timeout=60)
        self.cache.delete.assert_not_called()
        self.render.assert_called_once()
        self.upload.assert_not_called()
        self.assertEqual(envelope._PUSH_UNREPORTED["debounced"], 1)

    @override_settings(USER_MD_SKIP_UNCHANGED_TENANT_IDS=TENANT_ID)
    def test_dirty_changed_followup_after_skip_writes(self):
        changed = snapshot(content="## Tasks\n- Rest")

        def render(_tenant):
            if self.render.call_count == 1:
                self.follower(envelope.TRIGGER_REGISTRY_SIGNAL)
                return snapshot()
            return changed

        self.render.side_effect = render
        self.assertTrue(envelope.push_user_md(self.tenant, trigger=envelope.TRIGGER_FRIENDS))
        self.assertIn("outcome=unchanged", self.messages[0])
        self.assertEqual(self.render.call_count, 2)
        self.upload.assert_called_once_with(TENANT_ID, "workspace/USER.md", changed)
        self.assertIn("coalesced=1 rerun=1 cmp=different", self.writes()[0])
        self.cache.delete.assert_not_called()
        self.assert_released()

    def test_skip_and_write_drain_counters_identically(self):
        for allowlist in ("", TENANT_ID):
            with self.subTest(allowlist=allowlist), override_settings(USER_MD_SKIP_UNCHANGED_TENANT_IDS=allowlist):
                self.render.reset_mock()

                def failed_render(_tenant):
                    if self.render.call_count == 1:
                        self.follower(envelope.TRIGGER_REGISTRY_SIGNAL)
                    return snapshot()

                self.render.side_effect = failed_render
                self.download.side_effect = TimeoutError("private body")
                self.assertFalse(envelope.push_user_md(self.tenant, trigger=envelope.TRIGGER_FRIENDS))
                self.assertEqual(envelope._PUSH_UNREPORTED, {"debounced": 0, "coalesced": 1})
                self.download.side_effect = None
                self.messages.clear()
                self.render.reset_mock()
                self.cache.get.return_value = True
                self.assertFalse(envelope.push_user_md(self.tenant, trigger=envelope.TRIGGER_FRIENDS))
                self.assertFalse(envelope.push_user_md(self.tenant, trigger=envelope.TRIGGER_FRIENDS))
                self.assertEqual(envelope._PUSH_UNREPORTED, {"debounced": 2, "coalesced": 1})
                self.cache.get.return_value = False
                consumed = []

                def render(_tenant, consumed=consumed):
                    if self.render.call_count == 1:
                        self.follower(envelope.TRIGGER_REGISTRY_SIGNAL)
                        consumed.append(envelope._PUSH_PENDING[TENANT_ID])
                    return snapshot()

                self.render.side_effect = render
                self.assertIs(
                    envelope.push_user_md(self.tenant, trigger=envelope.TRIGGER_FRIENDS),
                    not bool(allowlist),
                )
                self.assertEqual(self.render.call_count, 2)
                self.assertEqual(consumed[0].coalesced, 0)
                self.assertEqual(envelope._PUSH_UNREPORTED, {"debounced": 0, "coalesced": 0})
                if allowlist:
                    self.assertEqual(
                        [line for line in self.messages if "outcome=unchanged" in line],
                        [
                            f"USER.md push not written tenant={TENANT_ID} trigger=friends "
                            "outcome=unchanged age_s=30 debounced=2 coalesced=1",
                            f"USER.md push not written tenant={TENANT_ID} trigger=registry_signal "
                            "outcome=unchanged age_s=30 debounced=0 coalesced=1",
                        ],
                    )
                    self.assertEqual(self.writes(), [])
                else:
                    self.assertIn("debounced=2 coalesced=1 rerun=0", self.writes()[0])
                    self.assertIn("debounced=0 coalesced=1 rerun=1", self.writes()[1])
                with patch.object(envelope.time, "monotonic", return_value=1600):
                    envelope._push_counter_summary()
                self.assertFalse(any(line.startswith("USER.md push summary") for line in self.messages))
                self.render.side_effect = None
                self.assertTrue(envelope.push_user_md(self.tenant, trigger=envelope.TRIGGER_MANUAL))
                self.assertIn("debounced=0 coalesced=0", self.writes()[-1])
                self.assert_released()

    @override_settings(USER_MD_SKIP_UNCHANGED_TENANT_IDS=TENANT_ID)
    def test_gated_clock_driven_content_changes_write(self):
        for old, new in [
            ("Today: Friday", "Today: Saturday"),
            ("Recent: walk", "Recent: none"),
        ]:
            with self.subTest(old=old):
                self.download.return_value = snapshot(content=old)
                self.render.return_value = snapshot(content=new)
                self.assertTrue(envelope.push_user_md(self.tenant, trigger=envelope.TRIGGER_FRIENDS))
                self.upload.assert_called_with(TENANT_ID, "workspace/USER.md", snapshot(content=new))
                self.assertIn("cmp=different age_s=30 eligible=false", self.writes()[-1])

    def test_debounced_leader_services_zero_debounce_follower(self):
        def marker(_key):
            self.follower(sender_model="Document")
            return True

        self.cache.get.side_effect = marker
        self.assertTrue(envelope.push_user_md(self.tenant, trigger=envelope.TRIGGER_CONVERSATION))
        self.upload.assert_called_once_with(TENANT_ID, "workspace/USER.md", snapshot())
        self.assertIn(
            "trigger=registry_signal sources=[registry_signal:Document] debounced=1 coalesced=1 rerun=1",
            self.writes()[0],
        )
        self.assertIn("eligible=true", self.writes()[0])  # Internal force is not freshness.
        self.assert_released()

    def test_failed_leader_services_each_forced_freshness_follower(self):
        for trigger in sorted(envelope._FORCED_FRESHNESS_TRIGGERS):
            with self.subTest(trigger=trigger):
                self.messages.clear()
                calls = 0

                def render(_tenant, trigger=trigger):
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        self.follower(trigger)
                        raise RuntimeError("private content must not appear")
                    return snapshot()

                self.render.side_effect = render
                self.assertTrue(envelope.push_user_md(self.tenant, trigger=envelope.TRIGGER_REGISTRY_SIGNAL))
                self.assertEqual(calls, 2)
                self.assertIn(f"sources=[{trigger}:none]", self.writes()[0])
                self.assertIn("coalesced=1 rerun=1 cmp=equal age_s=30 eligible=false", self.writes()[0])
                self.assertNotIn("private content", " ".join(self.messages))
                self.assert_released()

    def test_arrivals_stay_pending_until_their_followup(self):
        calls = 0

        def render(_tenant):
            nonlocal calls
            calls += 1
            if calls == 1:
                self.follower(envelope.TRIGGER_MANUAL)
                self.follower(envelope.TRIGGER_FRIENDS)
            elif calls == 2:
                self.follower(envelope.TRIGGER_HEALTHKIT)
            return snapshot()

        self.render.side_effect = render
        self.assertTrue(envelope.push_user_md(self.tenant, trigger=envelope.TRIGGER_CONVERSATION))
        lines = self.writes()
        self.assertEqual(len(lines), 3)
        self.assertIn("sources=[conversation:none] debounced=0 coalesced=0 rerun=0", lines[0])
        self.assertIn("sources=[friends:none,manual:none] debounced=0 coalesced=2 rerun=1", lines[1])
        self.assertIn("eligible=false", lines[1])
        self.assertIn("sources=[healthkit:none] debounced=0 coalesced=1 rerun=2", lines[2])
        self.assertIn("eligible=true", lines[2])
        self.assert_released()

    def test_pending_bound_counts_overflow_and_retains_freshness_flags(self):
        model_names = sorted({m.__name__ for s in envelope.all_sections() for m in s.refresh_on})
        self.assertGreaterEqual(len(model_names), 17)
        for overflow_trigger in (envelope.TRIGGER_MANUAL, envelope.TRIGGER_UNCLASSIFIED):
            with self.subTest(trigger=overflow_trigger):
                self.messages.clear()
                calls = 0

                def render(_tenant, overflow_trigger=overflow_trigger):
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        for name in model_names:
                            self.follower(sender_model=name)
                        self.follower(overflow_trigger)
                        with envelope._PUSH_STATE_LOCK:
                            pending = envelope._PUSH_PENDING[TENANT_ID]
                            self.assertEqual(len(pending.sources), 16)
                            self.assertEqual(pending.coalesced, len(model_names) + 1)
                    return snapshot()

                self.render.side_effect = render
                envelope.push_user_md(self.tenant, trigger=envelope.TRIGGER_CONVERSATION)
                self.assertIn(f"coalesced={len(model_names) + 1}", self.writes()[1])
                self.assertIn("eligible=false", self.writes()[1])
                self.assert_released()

    def test_read_failure_retains_warning_and_services_pending_request(self):
        reads = 0

        def download(*_args):
            nonlocal reads
            reads += 1
            if reads == 1:
                self.follower(envelope.TRIGGER_FLEET_SWEEP)
                raise TimeoutError("private body")
            return snapshot()

        self.download.side_effect = download
        self.assertTrue(envelope.push_user_md(self.tenant, trigger=envelope.TRIGGER_CONVERSATION))
        self.assertIn(
            f"USER.md push not written tenant={TENANT_ID} trigger=conversation outcome=read_failed", self.messages
        )
        self.assertTrue(any(line.startswith("Aborted USER.md push") for line in self.messages))
        self.assertNotIn("private body", " ".join(self.messages))
        self.assertIn("sources=[fleet_sweep:none]", self.writes()[0])
        self.cache.delete.assert_called_once()
        self.upload.assert_called_once()
        self.assert_released()

    def test_last_pass_boolean_is_not_a_physical_write_count(self):
        def first_render(_tenant):
            if self.render.call_count == 1:
                self.follower(envelope.TRIGGER_MANUAL)
            return snapshot()

        self.render.side_effect = first_render
        self.download.side_effect = [snapshot(), TimeoutError("private")]
        self.assertFalse(envelope.push_user_md(self.tenant, debounce_seconds=0))
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(envelope._PUSH_UNREPORTED["coalesced"], 1)
        self.assert_released()

    def test_failure_and_terminal_cleanup_release_pending_state(self):
        for error in (RuntimeError("private"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):

                def render(_tenant, error=error):
                    if isinstance(error, KeyboardInterrupt):
                        self.follower(envelope.TRIGGER_MANUAL)
                    raise error

                self.render.side_effect = render
                with self.assertRaises(type(error)):
                    envelope.push_user_md(self.tenant, debounce_seconds=0)
                self.assert_released()
        self.render.side_effect = None
        self.assertTrue(envelope.push_user_md(self.tenant, debounce_seconds=0))
        self.assertIn("sources=[unclassified:none]", self.writes()[0])

    def test_equal_different_unknown_all_upload_identical_merge_bytes(self):
        for existing, expected in [
            (snapshot(), "equal"),
            (snapshot(content="## Tasks\n- Rest"), "different"),
            (snapshot() + "notes\x00\n", "different"),
            (snapshot() + "\ufffd", "unknown"),
            (None, "unknown"),
            ("unmanaged", "unknown"),
        ]:
            with self.subTest(comparison=expected, existing=existing is None):
                self.download.return_value = existing
                self.assertTrue(
                    envelope.push_user_md(self.tenant, debounce_seconds=0, trigger=envelope.TRIGGER_FRIENDS)
                )
                self.upload.assert_called_with(
                    TENANT_ID, "workspace/USER.md", envelope.merge_into_user_md(existing, snapshot())
                )
                self.assertIn(f"cmp={expected}", self.writes()[-1])

    def test_upload_failure_preserves_retry_and_pending_metadata(self):
        def upload(*_args):
            if self.upload.call_count == 1:
                self.follower(envelope.TRIGGER_MANUAL)
                raise TimeoutError("private upload response")

        self.upload.side_effect = upload
        self.assertTrue(envelope.push_user_md(self.tenant, trigger=envelope.TRIGGER_CONVERSATION))
        self.assertEqual(self.upload.call_count, 2)
        self.cache.set.assert_called_once_with(
            f"{envelope._DEBOUNCE_CACHE_PREFIX}{TENANT_ID}",
            "1",
            timeout=60,
        )
        self.cache.delete.assert_called_once()
        self.assertEqual(len(self.writes()), 1)
        self.assertIn("sources=[manual:none] debounced=0 coalesced=1 rerun=1", self.writes()[0])
        self.assertNotIn("private upload response", " ".join(self.messages))
        self.assert_released()

    def test_unwritten_coalesced_counts_reach_summary_once(self):
        def render(_tenant):
            if self.render.call_count == 1:
                self.follower(envelope.TRIGGER_MANUAL)
            return snapshot()

        self.render.side_effect = render
        self.download.side_effect = TimeoutError("private")
        self.assertFalse(envelope.push_user_md(self.tenant, debounce_seconds=0))
        self.upload.assert_not_called()
        with patch.object(envelope.time, "monotonic", return_value=1600):
            envelope._push_counter_summary()
            envelope._push_counter_summary()
        summaries = [line for line in self.messages if line.startswith("USER.md push summary")]
        self.assertEqual(summaries, ["USER.md push summary debounced=0 coalesced=1"])
        self.download.side_effect = None
        self.assertTrue(envelope.push_user_md(self.tenant, debounce_seconds=0))
        self.assertIn("debounced=0 coalesced=0", self.writes()[0])
        self.assert_released()

    def test_shadow_failure_is_fail_open_and_local_sanitizer_is_patchable(self):
        with patch("apps.orchestrator.azure_client.sanitize_share_text", side_effect=ValueError("private content")):
            self.assertTrue(envelope.push_user_md(self.tenant, debounce_seconds=0))
        self.upload.assert_called_once_with(TENANT_ID, "workspace/USER.md", snapshot())
        self.assertIn("cmp=unknown age_s=-1 eligible=false", self.writes()[0])
        self.assertNotIn("private content", " ".join(self.messages))

    def test_full_eligibility_predicate_and_force_semantics(self):
        for trigger, force, age, expected in [
            (envelope.TRIGGER_FRIENDS, False, 0, "true"),
            (envelope.TRIGGER_FRIENDS, True, 3599, "true"),
            (envelope.TRIGGER_FRIENDS, False, 3600, "false"),
            (envelope.TRIGGER_FRIENDS, False, -1, "false"),
            (envelope.TRIGGER_UNCLASSIFIED, False, 30, "false"),
            (envelope.TRIGGER_MANUAL, False, 30, "false"),
        ]:
            with self.subTest(trigger=trigger, force=force, age=age):
                with patch.object(envelope, "_user_md_age_s", return_value=age):
                    self.assertTrue(envelope.push_user_md(self.tenant, force=force, trigger=trigger))
                self.assertIn(f"eligible={expected}", self.writes()[-1])
        self.cache.get.return_value = True
        self.assertFalse(envelope.push_user_md(self.tenant))
        self.assertTrue(envelope.push_user_md(self.tenant, force=True))

    def test_log_token_allowlist(self):
        for trigger, sender in [("private text\nmanual", "PrivateUserName"), ([], object()), ("manual", "Document")]:
            envelope.push_user_md(self.tenant, debounce_seconds=0, trigger=trigger, sender_model=sender)
        allowed_models = {m.__name__ for s in envelope.all_sections() for m in s.refresh_on} | {"none"}
        for line in self.writes():
            fields = dict(token.split("=", 1) for token in line.split(" chars) ", 1)[1].split())
            self.assertEqual(
                set(fields), {"trigger", "sources", "debounced", "coalesced", "rerun", "cmp", "age_s", "eligible"}
            )
            self.assertIn(fields["trigger"], envelope._TRIGGER_VOCABULARY)
            self.assertIn(fields["cmp"], {"equal", "different", "unknown"})
            self.assertIn(fields["eligible"], {"true", "false"})
            for pair in fields["sources"][1:-1].split(","):
                trigger, sender = pair.split(":")
                self.assertIn(trigger, envelope._TRIGGER_VOCABULARY)
                self.assertIn(sender, allowed_models)
        self.assertNotIn("Private", " ".join(self.messages))
        self.assertIn("trigger=unclassified sources=[unclassified:none]", self.writes()[0])

    def test_debounce_counters_drain_once_and_summary_is_rate_limited(self):
        self.cache.get.return_value = True
        envelope.push_user_md(self.tenant)
        envelope.push_user_md(self.tenant)
        self.logger.info.assert_not_called()
        self.assertTrue(envelope.push_user_md(self.tenant, force=True))
        self.assertIn("debounced=2 coalesced=0", self.writes()[0])
        self.assertTrue(envelope.push_user_md(self.tenant, force=True))
        self.assertIn("debounced=0 coalesced=0", self.writes()[1])
        for now in (1599, 1600, 1601, 2199, 2200):
            with patch.object(envelope.time, "monotonic", return_value=now):
                envelope.push_user_md(self.tenant)
        summaries = [line for line in self.messages if line.startswith("USER.md push summary")]
        self.assertEqual(
            summaries,
            [
                "USER.md push summary debounced=2 coalesced=0",
                "USER.md push summary debounced=3 coalesced=0",
            ],
        )


@override_settings(NBHD_DISABLE_BACKGROUND_THREADS=True)
class PropagationTests(SimpleTestCase):
    def test_background_wrapper_propagates_both_values_in_both_modes(self):
        for disabled in (True, False):
            with (
                self.subTest(disabled=disabled),
                override_settings(NBHD_DISABLE_BACKGROUND_THREADS=disabled),
                patch.object(envelope, "push_user_md") as push,
                patch.object(threading, "Thread") as thread,
            ):
                envelope.push_user_md_in_background(
                    TENANT_ID, trigger=envelope.TRIGGER_REGISTRY_SIGNAL, sender_model="Document"
                )
                if not disabled:
                    thread.call_args.kwargs["target"]()
                    self.assertTrue(thread.call_args.kwargs["daemon"])
                    thread.return_value.start.assert_called_once()
                push.assert_called_once_with(
                    TENANT_ID, trigger=envelope.TRIGGER_REGISTRY_SIGNAL, sender_model="Document"
                )

    def test_registry_schedules_sender_name_after_commit(self):
        from apps.journal.models import Document
        from apps.orchestrator.envelope_registry import _universal_refresh_receiver

        callbacks = []
        with (
            patch("django.db.transaction.on_commit", side_effect=callbacks.append),
            patch.object(envelope, "push_user_md") as push,
        ):
            _universal_refresh_receiver(Document, SimpleNamespace(tenant_id=TENANT_ID))
            push.assert_not_called()
            self.assertEqual(len(callbacks), 1)
            callbacks[0]()
            push.assert_called_once_with(
                TENANT_ID, debounce_seconds=0, trigger=envelope.TRIGGER_REGISTRY_SIGNAL, sender_model="Document"
            )

    def test_post_batch_and_conversation_callers(self):
        from apps.datebook.services import push_visibility_refresh as datebook_refresh
        from apps.friends.envelope import _schedule_recipient_push
        from apps.fuel.healthkit import push_visibility_refresh as healthkit_refresh
        from apps.router.conversation_capture import _REFRESH_DEBOUNCE_SECONDS, schedule_user_md_refresh

        with (
            patch("django.db.transaction.on_commit", side_effect=lambda callback: callback()),
            patch.object(envelope, "push_user_md") as push,
        ):
            _schedule_recipient_push(TENANT_ID)
            healthkit_refresh(TENANT_ID)
            datebook_refresh(TENANT_ID)
            schedule_user_md_refresh(SimpleNamespace(id=TENANT_ID))
        self.assertEqual(
            push.call_args_list,
            [
                call(TENANT_ID, debounce_seconds=0, trigger=envelope.TRIGGER_FRIENDS),
                call(TENANT_ID, debounce_seconds=0, trigger=envelope.TRIGGER_HEALTHKIT),
                call(TENANT_ID, debounce_seconds=0, trigger=envelope.TRIGGER_DATEBOOK),
                call(TENANT_ID, debounce_seconds=_REFRESH_DEBOUNCE_SECONDS, trigger=envelope.TRIGGER_CONVERSATION),
            ],
        )

    def test_all_listed_call_sites_pass_imported_constants(self):
        # Audit complex service/view paths without running provisioning or HTTP
        # side effects. Runtime tests above cover the scheduling seams; existing
        # DB suites exercise the service/view branches themselves.
        expected = {
            "apps/orchestrator/envelope_registry.py": ["REGISTRY_SIGNAL"],
            "apps/friends/envelope.py": ["FRIENDS"],
            "apps/fuel/healthkit.py": ["HEALTHKIT"],
            "apps/datebook/services.py": ["DATEBOOK"],
            "apps/router/conversation_capture.py": ["CONVERSATION"],
            "apps/router/chat_views.py": ["PLACE_OBSERVATION"],
            "apps/integrations/runtime_views.py": ["PLACE_OBSERVATION"],
            "apps/orchestrator/services.py": ["PROVISION", "CONFIG_UPDATE", "CRON_PROMPTS"],
            "apps/orchestrator/tasks.py": ["FLEET_SWEEP"],
            "apps/orchestrator/management/commands/refresh_user_md.py": ["MANUAL"],
        }
        root = Path(__file__).resolve().parents[2]
        for filename, triggers in expected.items():
            with self.subTest(filename=filename):
                tree = ast.parse((root / filename).read_text())
                calls = sorted(
                    (
                        node
                        for node in ast.walk(tree)
                        if isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id in {"push_user_md", "push_user_md_in_background"}
                    ),
                    key=lambda n: n.lineno,
                )
                self.assertEqual(len(calls), len(triggers))
                for node, trigger in zip(calls, triggers):
                    value = next(kw.value for kw in node.keywords if kw.arg == "trigger")
                    self.assertIsInstance(value, ast.Name)
                    self.assertEqual(value.id, f"TRIGGER_{trigger}")
                    self.assertTrue(
                        any(
                            isinstance(imp, ast.ImportFrom)
                            and (imp.module or "").endswith("workspace_envelope")
                            and any(alias.name == value.id for alias in imp.names)
                            for imp in ast.walk(tree)
                        )
                    )

    def test_manual_preserves_output_and_force(self):
        from apps.orchestrator.management.commands.refresh_user_md import Command

        tenant = Tenant(id=TENANT_ID)
        command = Command(stdout=io.StringIO())
        with (
            patch.object(Tenant.objects, "select_related") as query,
            patch("apps.orchestrator.management.commands.refresh_user_md.push_user_md", return_value=False) as push,
        ):
            query.return_value.filter.return_value.exclude.return_value = [tenant]
            command.handle(tenant="")
        push.assert_called_once_with(tenant, force=True, trigger=envelope.TRIGGER_MANUAL)
        self.assertIn("Done: 1 pushed, 0 errors", command.stdout.getvalue())

    def test_sweep_accounts_for_boolean_returns_and_raises(self):
        from apps.orchestrator.tasks import refresh_user_md_fleet_task

        tenants = [Tenant(), Tenant(), Tenant()]
        with (
            patch.object(Tenant.objects, "filter") as query,
            patch.object(envelope, "push_user_md", side_effect=[True, False, RuntimeError("private")]) as push,
        ):
            query.return_value.exclude.return_value.select_related.return_value = tenants
            with self.assertLogs("apps.orchestrator.tasks", level="INFO") as logs:
                result = refresh_user_md_fleet_task()
        self.assertEqual(
            result, {"attempted": 3, "returned_true": 1, "returned_false": 1, "raised": 1, "pushed": 1, "failed": 1}
        )
        self.assertIn("refresh_user_md_fleet: attempted=3 returned_true=1 returned_false=1 raised=1", logs.output[-1])
        self.assertEqual(
            push.call_args_list,
            [call(tenant, force=True, debounce_seconds=0, trigger=envelope.TRIGGER_FLEET_SWEEP) for tenant in tenants],
        )
