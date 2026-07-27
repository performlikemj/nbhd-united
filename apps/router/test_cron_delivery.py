"""Tests for cron delivery endpoint."""

from datetime import datetime
from unittest.mock import MagicMock, PropertyMock, patch
from zoneinfo import ZoneInfo

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.cron.models import CronJob
from apps.journal.models import Document
from apps.router.cron_delivery import _rate_counts, _split_message
from apps.router.models import ProactiveOutbound
from apps.tenants.models import Tenant
from apps.tenants.test_utils import seed_internal_key


class SplitMessageTest(TestCase):
    def test_short_message(self):
        self.assertEqual(_split_message("hello"), ["hello"])

    def test_long_message_splits_on_paragraph(self):
        text = "A" * 4000 + "\n\n" + "B" * 100
        chunks = _split_message(text, max_len=4096)
        self.assertEqual(len(chunks), 2)

    def test_very_long_message(self):
        text = "A" * 10000
        chunks = _split_message(text, max_len=4096)
        self.assertTrue(all(len(c) <= 4096 for c in chunks))


@override_settings(
    TELEGRAM_BOT_TOKEN="test-token",
    NBHD_INTERNAL_API_KEY="test-key",
)
class CronDeliveryViewTest(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        User = get_user_model()
        self.user = User.objects.create_user(username="crontest", password="pass")
        self.user.telegram_chat_id = 12345
        self.user.save()
        self.tenant = Tenant.objects.create(
            user=self.user,
            status=Tenant.Status.ACTIVE,
        )
        seed_internal_key(self.tenant)
        self.client = APIClient()
        self.url = f"/api/v1/integrations/runtime/{self.tenant.id}/send-to-user/"
        _rate_counts.clear()

    def _headers(self):
        return {
            "HTTP_X_NBHD_INTERNAL_KEY": "test-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }

    def test_auth_required(self):
        resp = self.client.post(self.url, {"message": "hello"}, format="json")
        self.assertEqual(resp.status_code, 401)

    def test_missing_message(self):
        resp = self.client.post(self.url, {}, format="json", **self._headers())
        self.assertEqual(resp.status_code, 400)

    @patch("apps.router.cron_delivery.httpx.Client")
    def test_successful_send(self, mock_client_cls):
        mock_http = MagicMock()
        mock_resp = MagicMock()
        mock_resp.is_success = True
        mock_resp.status_code = 200
        mock_http.post.return_value = mock_resp
        mock_http.__enter__ = MagicMock(return_value=mock_http)
        mock_http.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_http

        resp = self.client.post(self.url, {"message": "Good morning!"}, format="json", **self._headers())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "sent")
        self.assertEqual(resp.json()["chunks"], 1)

    @patch("apps.router.cron_delivery.httpx.Client")
    def test_large_table_transport_stays_full_while_history_is_shortened(self, mock_client_cls):
        from apps.journal.models import Document
        from apps.router.models import ProactiveOutbound

        self.tenant.experimental_reply_artifacts_to_journal = True
        self.tenant.save(update_fields=["experimental_reply_artifacts_to_journal"])
        lines = ["| Name | Value |", "| --- | --- |"]
        lines.extend(f"| row {index} | value {index} |" for index in range(26))
        full_message = "\n".join(lines)

        mock_http = MagicMock()
        mock_resp = MagicMock()
        mock_resp.is_success = True
        mock_resp.status_code = 200
        mock_http.post.return_value = mock_resp
        mock_http.__enter__ = MagicMock(return_value=mock_http)
        mock_http.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_http

        response = self.client.post(
            self.url,
            {"message": full_message},
            format="json",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 200)
        sent_bodies = "\n".join(
            call.kwargs["json"]["text"]
            for call in mock_http.post.call_args_list
            if "text" in call.kwargs.get("json", {})
        )
        self.assertIn("row 25", sent_bodies)
        self.assertNotIn("Saved the full table", sent_bodies)

        stored = ProactiveOutbound.objects.get(tenant=self.tenant)
        self.assertIn("Saved the full table (26 rows)", stored.message_text)
        self.assertIn("| Name | Value |", stored.message_text)
        self.assertIn("| row 2 | value 2 |", stored.message_text)
        self.assertNotIn("| row 3 | value 3 |", stored.message_text)
        self.assertEqual(stored.journal_link["kind"], "project")
        self.assertTrue(Document.objects.filter(tenant=self.tenant, slug=stored.journal_link["slug"]).exists())

    def test_rate_limit(self):
        import time

        tid = str(self.tenant.id)
        _rate_counts[tid] = [time.time()] * 20  # Fill to limit

        resp = self.client.post(self.url, {"message": "hello"}, format="json", **self._headers())
        self.assertEqual(resp.status_code, 429)

    @patch("apps.router.cron_delivery.httpx.Client")
    def test_quick_reply_marker_stripped_before_send_and_persist(self, mock_client_cls):
        """Telegram gets clean text while its cross-channel row keeps pills."""
        mock_http = MagicMock()
        mock_resp = MagicMock()
        mock_resp.is_success = True
        mock_resp.status_code = 200
        mock_http.post.return_value = mock_resp
        mock_http.__enter__ = MagicMock(return_value=mock_http)
        mock_http.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_http

        resp = self.client.post(
            self.url,
            {"message": "Good morning!\n[[quick-replies: Snooze | Done]]"},
            format="json",
            **self._headers(),
        )
        self.assertEqual(resp.status_code, 200)

        sent_text = mock_http.post.call_args.kwargs["json"]["text"]
        self.assertNotIn("quick-replies", sent_text)
        self.assertIn("Good morning!", sent_text)

        from apps.router.models import ProactiveOutbound

        stored = ProactiveOutbound.objects.get(tenant=self.tenant)
        self.assertNotIn("quick-replies", stored.message_text)
        self.assertEqual(stored.quick_replies, ["Snooze", "Done"])

    @override_settings(LINE_CHANNEL_ACCESS_TOKEN="line-token")
    @patch("apps.router.cron_delivery.httpx.Client")
    def test_line_quick_reply_marker_stripped_before_send_and_persisted(self, mock_client_cls):
        self.user.telegram_chat_id = None
        self.user.line_user_id = "Ulinequickreply"
        self.user.save(update_fields=["telegram_chat_id", "line_user_id"])

        mock_http = MagicMock()
        mock_resp = MagicMock()
        mock_resp.is_success = True
        mock_resp.status_code = 200
        mock_http.post.return_value = mock_resp
        mock_http.__enter__ = MagicMock(return_value=mock_http)
        mock_http.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_http

        resp = self.client.post(
            self.url,
            {"message": "How was today?\n[[quick-replies: 👍 Good day | 🫤 Mixed | 👎 Rough]]"},
            format="json",
            **self._headers(),
        )

        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("quick-replies", str(mock_http.post.call_args.kwargs["json"]))
        stored = ProactiveOutbound.objects.get(tenant=self.tenant)
        self.assertEqual(stored.channel, "line")
        self.assertEqual(stored.quick_replies, ["👍 Good day", "🫤 Mixed", "👎 Rough"])
        self.assertNotIn("quick-replies", stored.message_text)

    @patch("apps.router.proactive_context._dispatch_ios_push")
    def test_app_quick_reply_marker_stripped_before_send_and_persisted(self, _push):
        from apps.router.models import DeviceToken

        DeviceToken.objects.create(tenant=self.tenant, user=self.user, token="q" * 64)

        resp = self.client.post(
            self.url,
            {"message": "What next?\n[[quick-replies: Add a note | How's my week?]]"},
            format="json",
            **self._headers(),
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["channel"], "app")
        stored = ProactiveOutbound.objects.get(tenant=self.tenant)
        self.assertEqual(stored.quick_replies, ["Add a note", "How's my week?"])
        self.assertEqual(stored.message_text, "What next?")

    @patch("apps.router.cron_delivery.httpx.Client")
    def test_journal_then_quick_replies_tail_parses_and_stores_both(self, mock_client_cls):
        """Parse order requires journal-link before the final quick-replies line."""
        mock_http = MagicMock()
        mock_resp = MagicMock()
        mock_resp.is_success = True
        mock_resp.status_code = 200
        mock_http.post.return_value = mock_resp
        mock_http.__enter__ = MagicMock(return_value=mock_http)
        mock_http.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_http

        resp = self.client.post(
            self.url,
            {
                "message": (
                    "Here's your morning report.\n\n"
                    "[[journal-link: daily|2026-07-13|Morning Report]]\n"
                    "[[quick-replies: How's my week? | Add a note]]"
                )
            },
            format="json",
            **self._headers(),
        )

        self.assertEqual(resp.status_code, 200)
        sent_text = mock_http.post.call_args.kwargs["json"]["text"]
        self.assertNotIn("[[journal-link:", sent_text)
        self.assertNotIn("[[quick-replies:", sent_text)
        stored = ProactiveOutbound.objects.get(tenant=self.tenant)
        self.assertEqual(
            stored.journal_link,
            {"kind": "daily", "slug": "2026-07-13", "title": "Morning Report"},
        )
        self.assertEqual(stored.quick_replies, ["How's my week?", "Add a note"])
        self.assertEqual(stored.message_text, "Here's your morning report.")

    @patch("apps.router.cron_delivery.httpx.Client")
    def test_journal_link_marker_stripped_from_send_and_persisted(self, mock_client_cls):
        """Unlike quick-replies, the journal deep-link IS persisted for cron/
        proactive sends: it rides ProactiveOutbound.journal_link and surfaces in
        the iOS ?since= feed, so a Telegram-delivered morning report gets a
        tappable chip in the app. The marker is still stripped from the sent
        Telegram text (no chip transport there)."""
        mock_http = MagicMock()
        mock_resp = MagicMock()
        mock_resp.is_success = True
        mock_resp.status_code = 200
        mock_http.post.return_value = mock_resp
        mock_http.__enter__ = MagicMock(return_value=mock_http)
        mock_http.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_http

        resp = self.client.post(
            self.url,
            {"message": "Here's your morning report.\n[[journal-link: daily|2026-07-13|Morning Report]]"},
            format="json",
            **self._headers(),
        )
        self.assertEqual(resp.status_code, 200)

        sent_text = mock_http.post.call_args.kwargs["json"]["text"]
        self.assertNotIn("journal-link", sent_text)
        self.assertIn("morning report", sent_text)

        from apps.router.models import ProactiveOutbound

        stored = ProactiveOutbound.objects.get(tenant=self.tenant)
        self.assertNotIn("journal-link", stored.message_text)
        self.assertEqual(
            stored.journal_link,
            {"kind": "daily", "slug": "2026-07-13", "title": "Morning Report"},
        )

    @patch("apps.router.cron_delivery.httpx.Client")
    def test_no_journal_link_marker_leaves_column_null(self, mock_client_cls):
        mock_http = MagicMock()
        mock_resp = MagicMock()
        mock_resp.is_success = True
        mock_resp.status_code = 200
        mock_http.post.return_value = mock_resp
        mock_http.__enter__ = MagicMock(return_value=mock_http)
        mock_http.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_http

        resp = self.client.post(self.url, {"message": "Plain check-in."}, format="json", **self._headers())
        self.assertEqual(resp.status_code, 200)

        from apps.router.models import ProactiveOutbound

        stored = ProactiveOutbound.objects.get(tenant=self.tenant)
        self.assertIsNone(stored.journal_link)
        self.assertIsNone(stored.quick_replies)


@override_settings(
    TELEGRAM_BOT_TOKEN="test-token",
    NBHD_INTERNAL_API_KEY="test-key",
)
class MorningBriefingJournalFallbackTest(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        User = get_user_model()
        self.user = User.objects.create_user(
            username="briefing-fallback",
            password="pass",
            timezone="Asia/Tokyo",
        )
        self.user.telegram_chat_id = 12345
        self.user.save(update_fields=["telegram_chat_id"])
        self.tenant = Tenant.objects.create(
            user=self.user,
            status=Tenant.Status.ACTIVE,
        )
        seed_internal_key(self.tenant)
        self.client = APIClient()
        self.url = f"/api/v1/integrations/runtime/{self.tenant.id}/send-to-user/"
        self.utc_now = datetime(2026, 7, 27, 15, 30, tzinfo=ZoneInfo("UTC"))
        self.local_slug = "2026-07-28"
        _rate_counts.clear()

    def _headers(self, job_name):
        return {
            "HTTP_X_NBHD_INTERNAL_KEY": "test-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
            "HTTP_X_NBHD_JOB_NAME": job_name,
        }

    def _configure_successful_telegram(self, mock_client_cls):
        mock_http = MagicMock()
        mock_resp = MagicMock()
        mock_resp.is_success = True
        mock_resp.status_code = 200
        mock_http.post.return_value = mock_resp
        mock_http.__enter__ = MagicMock(return_value=mock_http)
        mock_http.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_http

    def _create_today_document(self):
        return Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.DAILY,
            slug=self.local_slug,
            title=self.local_slug,
            markdown=f"# {self.local_slug}\n\n## Morning Report\nBriefing.\n",
        )

    def _post(self, *, job_name, message="Good morning."):
        with patch("django.utils.timezone.now", return_value=self.utc_now):
            return self.client.post(
                self.url,
                {"message": message},
                format="json",
                **self._headers(job_name),
            )

    def _assert_fallback_link(self):
        stored = ProactiveOutbound.objects.get(tenant=self.tenant)
        self.assertEqual(
            stored.journal_link,
            {
                "kind": "daily",
                "slug": self.local_slug,
                "title": "Morning Report",
            },
        )

    @patch("apps.router.cron_delivery.httpx.Client")
    def test_marker_present_is_stored_as_parsed_without_fallback(self, mock_client_cls):
        self._configure_successful_telegram(mock_client_cls)
        self._create_today_document()
        message = "Briefing.\n[[journal-link: daily|2026-07-27|Agent Title]]"

        with (
            patch("apps.cron.models.CronJob.objects.filter") as mock_cron_filter,
            self.assertNoLogs("apps.router.cron_delivery", level="WARNING"),
        ):
            response = self._post(job_name="Morning Briefing", message=message)

        self.assertEqual(response.status_code, 200)
        mock_cron_filter.assert_not_called()
        stored = ProactiveOutbound.objects.get(tenant=self.tenant)
        self.assertEqual(
            stored.journal_link,
            {
                "kind": "daily",
                "slug": "2026-07-27",
                "title": "Agent Title",
            },
        )

    @patch("apps.router.cron_delivery.httpx.Client")
    def test_name_header_gets_tenant_local_fallback(self, mock_client_cls):
        self._configure_successful_telegram(mock_client_cls)
        self._create_today_document()

        with self.assertLogs("apps.router.cron_delivery", level="WARNING") as logs:
            response = self._post(job_name="mOrNiNg BrIeFiNg")

        self.assertEqual(response.status_code, 200)
        self._assert_fallback_link()
        self.assertIn("briefing_missing_journal_link_fallback", logs.output[0])
        self.assertEqual(logs.records[0].tenant_id, str(self.tenant.id))

    @patch("apps.router.cron_delivery.httpx.Client")
    def test_cron_row_id_header_gets_fallback(self, mock_client_cls):
        self._configure_successful_telegram(mock_client_cls)
        self._create_today_document()
        job = CronJob.objects.create(tenant=self.tenant, name="Morning Briefing")

        with self.assertLogs("apps.router.cron_delivery", level="WARNING"):
            response = self._post(job_name=str(job.id))

        self.assertEqual(response.status_code, 200)
        self._assert_fallback_link()

    @patch("apps.router.cron_delivery.httpx.Client")
    def test_gateway_job_id_header_gets_fallback(self, mock_client_cls):
        self._configure_successful_telegram(mock_client_cls)
        self._create_today_document()
        job = CronJob.objects.create(
            tenant=self.tenant,
            name="Morning Briefing",
            gateway_job_id="gateway-briefing-id",
        )

        with self.assertLogs("apps.router.cron_delivery", level="WARNING"):
            response = self._post(job_name=job.gateway_job_id)

        self.assertEqual(response.status_code, 200)
        self._assert_fallback_link()

    @patch("apps.router.cron_delivery.httpx.Client")
    def test_json_data_id_header_gets_fallback(self, mock_client_cls):
        self._configure_successful_telegram(mock_client_cls)
        self._create_today_document()
        job = CronJob.objects.create(
            tenant=self.tenant,
            name="Morning Briefing",
            data={"id": "json-briefing-id"},
        )

        with self.assertLogs("apps.router.cron_delivery", level="WARNING"):
            response = self._post(job_name=job.data["id"])

        self.assertEqual(response.status_code, 200)
        self._assert_fallback_link()

    @patch("apps.router.cron_delivery.httpx.Client")
    def test_snapshot_daemon_id_gets_fallback_when_cron_row_ids_are_stale(self, mock_client_cls):
        self._configure_successful_telegram(mock_client_cls)
        self._create_today_document()
        daemon_job_id = "e5428d81-3333-4444-8888-123456789abc"
        CronJob.objects.create(
            tenant=self.tenant,
            name="Morning Briefing",
            gateway_job_id="5c383417-stale-gateway-id",
            data={},
        )
        self.tenant.cron_jobs_snapshot = {
            "jobs": [{"id": daemon_job_id, "name": "Morning Briefing"}],
            "snapshot_at": "2026-07-27T00:00:00Z",
        }
        self.tenant.save(update_fields=["cron_jobs_snapshot"])

        with self.assertLogs("apps.router.cron_delivery", level="WARNING") as logs:
            response = self._post(job_name=daemon_job_id)

        self.assertEqual(response.status_code, 200)
        self._assert_fallback_link()
        self.assertIn("briefing_missing_journal_link_fallback", logs.output[0])
        self.assertEqual(logs.records[0].tenant_id, str(self.tenant.id))

    @patch("apps.router.cron_delivery.httpx.Client")
    def test_snapshot_daemon_id_for_different_job_stays_chipless(self, mock_client_cls):
        self._configure_successful_telegram(mock_client_cls)
        self._create_today_document()
        daemon_job_id = "e5428d81-3333-4444-8888-123456789abc"
        self.tenant.cron_jobs_snapshot = {
            "jobs": [{"id": daemon_job_id, "name": "Evening Check-in"}],
            "snapshot_at": "2026-07-27T00:00:00Z",
        }
        self.tenant.save(update_fields=["cron_jobs_snapshot"])

        with self.assertNoLogs("apps.router.cron_delivery", level="WARNING"):
            response = self._post(job_name=daemon_job_id)

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(ProactiveOutbound.objects.get(tenant=self.tenant).journal_link)

    @patch("apps.router.cron_delivery.httpx.Client")
    def test_malformed_snapshots_do_not_block_delivery_or_attach_chip(self, mock_client_cls):
        self._configure_successful_telegram(mock_client_cls)
        self._create_today_document()
        malformed_snapshots = {
            "none": None,
            "non_dict": [],
            "jobs_not_list": {"jobs": {"id": "opaque-job-id", "name": "Morning Briefing"}},
            "non_dict_jobs": {"jobs": [None, "bad-entry", 42]},
        }

        for label, snapshot in malformed_snapshots.items():
            with self.subTest(snapshot=label):
                _rate_counts.clear()
                ProactiveOutbound.objects.filter(tenant=self.tenant).delete()
                with (
                    patch.object(
                        Tenant,
                        "cron_jobs_snapshot",
                        new_callable=PropertyMock,
                        return_value=snapshot,
                    ),
                    self.assertNoLogs("apps.router.cron_delivery", level="WARNING"),
                ):
                    response = self._post(
                        job_name=f"opaque-job-id-{label}",
                        message=f"Malformed snapshot: {label}",
                    )

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], "sent")
                self.assertIsNone(ProactiveOutbound.objects.get(tenant=self.tenant).journal_link)

    @patch("apps.router.cron_delivery.httpx.Client")
    def test_non_briefing_job_stays_chipless(self, mock_client_cls):
        self._configure_successful_telegram(mock_client_cls)
        self._create_today_document()

        with self.assertNoLogs("apps.router.cron_delivery", level="WARNING"):
            response = self._post(job_name="Evening Check-in")

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(ProactiveOutbound.objects.get(tenant=self.tenant).journal_link)

    @patch("apps.router.cron_delivery.httpx.Client")
    def test_briefing_without_today_document_stays_chipless(self, mock_client_cls):
        self._configure_successful_telegram(mock_client_cls)

        with self.assertNoLogs("apps.router.cron_delivery", level="WARNING"):
            response = self._post(job_name="Morning Briefing")

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(ProactiveOutbound.objects.get(tenant=self.tenant).journal_link)

    @patch("apps.router.cron_delivery.httpx.Client")
    def test_detection_exception_does_not_block_delivery(self, mock_client_cls):
        self._configure_successful_telegram(mock_client_cls)
        self._create_today_document()

        with (
            patch(
                "apps.cron.models.CronJob.objects.filter",
                side_effect=RuntimeError("detection failed"),
            ),
            self.assertNoLogs("apps.router.cron_delivery", level="WARNING"),
        ):
            response = self._post(job_name="opaque-job-id")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "sent")
        self.assertIsNone(ProactiveOutbound.objects.get(tenant=self.tenant).journal_link)
