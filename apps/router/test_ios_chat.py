"""Tests for the iOS/rich-client chat ingress (route chat through tenant).

The iOS app becomes a channel into the tenant's OpenClaw runtime — same
USER.md/memory as Telegram/LINE — but with no push transport, so the reply
is persisted to ``AppChatMessage`` and the client polls for it. These tests
cover the additive PR1 slice:

  - POST a message → routes through the tenant (``thread:<id>`` user param,
    X-Channel ios) → reply persisted → poll returns it
  - idempotency on ``client_msg_id``
  - the shared "main" thread is the default and reused across messages
  - named threads get their own ``user_param`` (own OpenClaw session)
  - the budget gate blocks enqueue for an over-budget tenant

The drain runs inline on publish in the test path (see
``test_pending_queue.py``), so a POST drives the OC turn through to a
persisted reply within the request.
"""

from __future__ import annotations

import base64
import secrets
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.router.inbound_media import MAX_APP_DOCUMENT_BYTES, MAX_APP_IMAGE_BYTES, attachment_marker
from apps.router.models import AppChatMessage, ChatThread, PendingMessage
from apps.tenants.models import Tenant, User, UserSituation
from apps.tenants.throttling import ChatMessageSendHourThrottle

# Magic-valid but tiny image payloads for the ingress tests.
_JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 32
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_NOT_IMAGE = b"%PDF-1.4\nnot really an image"

# Magic-valid but tiny PDF payload; and a renamed ZIP that is NOT a PDF.
_PDF_BYTES = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n" + b"\x00" * 32
_NOT_PDF = b"PK\x03\x04" + b"\x00" * 32  # ZIP magic — must be rejected by the doc gate


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _make_user() -> User:
    return User.objects.create_user(
        username=f"ios_{secrets.token_hex(4)}",
        email=f"{secrets.token_hex(4)}@example.com",
        preferred_channel="telegram",
    )


def _make_tenant(user: User) -> Tenant:
    return Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE,
        container_fqdn="oc-ios.example.com",
    )


def _ok_chat_response(text: str = "ok"):
    resp = MagicMock()
    resp.status_code = 200
    resp.is_success = True
    resp.json.return_value = {
        "choices": [{"message": {"content": text}}],
        "usage": {},  # empty → _record_usage_safe is a no-op
        "model": "test",
    }
    resp.raise_for_status = MagicMock()
    return resp


def _snapshot_pending_on_post(tenant, text: str = "ok"):
    """Gateway-boundary capture for queue-payload-shape assertions.

    Delete-on-drain (privacy PR-3) hard-deletes delivered ``PendingMessage``
    rows before the ingress view returns, so a test can no longer fetch the
    queue row after the POST. The row DOES still exist while the drain's
    gateway POST is in flight — so tests that legitimately inspect the
    mid-flow payload shape (markers, is_image/is_document flags, redacted
    excerpt) snapshot it here, at the moment it is actually on the wire.

    Returns ``(side_effect, captured)``: assign ``side_effect`` to
    ``mock_post.side_effect``; after the request, ``captured["pmsg"]`` is the
    in-memory row instance (safe to inspect post-deletion).
    """
    captured: dict = {}

    def _post(url, *args, **kwargs):
        rows = list(PendingMessage.objects.filter(tenant=tenant).order_by("created_at"))
        if rows:
            captured["pmsg"] = rows[-1]
            captured["rows"] = rows
        return _ok_chat_response(text)

    return _post, captured


@override_settings(NBHD_INTERNAL_API_KEY="test-key")
class IOSChatRoutingTest(TestCase):
    def setUp(self):
        self.user = _make_user()
        self.tenant = _make_tenant(self.user)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    @patch("apps.router.pending_queue.httpx.post")
    def test_message_routes_through_tenant_and_persists_reply(self, mock_post):
        side_effect, captured = _snapshot_pending_on_post(self.tenant, "Of course I know you, MJ.")
        mock_post.side_effect = side_effect

        resp = self.client.post(
            "/api/v1/chat/messages/",
            {"text": "who am I?", "client_msg_id": "c1"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.data["status"], "pending")
        self.assertEqual(resp.data["client_msg_id"], "c1")

        # A PendingMessage was enqueued on the ios channel with a thread-scoped
        # user param (NOT a channel id) and the client_msg_id on its payload —
        # snapshotted at gateway-POST time, because delete-on-drain removes the
        # row the moment delivery completes.
        pmsg = captured["pmsg"]
        self.assertEqual(pmsg.channel, PendingMessage.Channel.IOS)
        main = ChatThread.objects.get(tenant=self.tenant, is_main=True)
        self.assertEqual(pmsg.channel_user_id, str(main.id))
        self.assertEqual(pmsg.payload["user_param"], f"thread:{main.id}")
        self.assertEqual(pmsg.payload["client_msg_id"], "c1")
        # Delivered → hard-deleted (PR-3): the transient queue keeps nothing.
        self.assertFalse(PendingMessage.objects.filter(tenant=self.tenant).exists())

        # The gateway POST carried the thread user param + the ios channel header.
        sent = mock_post.call_args.kwargs
        self.assertEqual(sent["json"]["user"], f"thread:{main.id}")
        self.assertEqual(sent["headers"]["X-Channel"], "ios")
        self.assertEqual(sent["headers"]["X-OpenClaw-Message-Channel"], "ios")

        # The reply was persisted; the client polls and gets it.
        poll = self.client.get("/api/v1/chat/messages/c1/")
        self.assertEqual(poll.status_code, 200, poll.content)
        self.assertEqual(poll.data["status"], "ready")
        self.assertIn("I know you", poll.data["reply_text"])

    @patch("apps.router.pending_queue.httpx.post")
    def test_idempotent_on_client_msg_id(self, mock_post):
        mock_post.return_value = _ok_chat_response("hi")

        first = self.client.post(
            "/api/v1/chat/messages/",
            {"text": "hello", "client_msg_id": "dup"},
            format="json",
        )
        self.assertEqual(first.status_code, 201, first.content)

        second = self.client.post(
            "/api/v1/chat/messages/",
            {"text": "hello again", "client_msg_id": "dup"},
            format="json",
        )
        # Second is a no-op replay → 200, returns the existing turn.
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(
            AppChatMessage.objects.filter(tenant=self.tenant, client_msg_id="dup").count(),
            1,
        )

    @patch("apps.orchestrator.workspace_envelope.push_user_md_in_background")
    @patch("apps.router.pending_queue.httpx.post")
    def test_structured_location_is_captured_and_coordinate_keys_are_ignored(self, mock_post, push_user_md):
        mock_post.return_value = _ok_chat_response("hi")
        self.tenant.situational_context_enabled = True
        self.tenant.save(update_fields=["situational_context_enabled"])

        resp = self.client.post(
            "/api/v1/chat/messages/",
            {
                "text": "hello from here",
                "client_msg_id": "location-1",
                "location": {
                    "place_label": "Fukuoka",
                    "lat": 33.59,
                    "lon": 130.40,
                    "accuracy": 20,
                },
            },
            format="json",
        )

        self.assertEqual(resp.status_code, 201, resp.content)
        situation = UserSituation.objects.get(tenant=self.tenant)
        self.assertEqual(situation.current_place_label, "Fukuoka")
        self.assertEqual(situation.current_place_source, "ios_chat")
        self.user.refresh_from_db()
        self.assertIsNone(self.user.location_lat)
        self.assertIsNone(self.user.location_lon)
        push_user_md.assert_called_once()

    @patch("apps.orchestrator.workspace_envelope.push_user_md_in_background")
    @patch("apps.router.pending_queue.httpx.post")
    def test_duplicate_client_msg_id_does_not_capture_location_twice(self, mock_post, push_user_md):
        mock_post.return_value = _ok_chat_response("hi")
        self.tenant.situational_context_enabled = True
        self.tenant.save(update_fields=["situational_context_enabled"])

        first = self.client.post(
            "/api/v1/chat/messages/",
            {
                "text": "first",
                "client_msg_id": "location-duplicate",
                "location": {"place_label": "Fukuoka"},
            },
            format="json",
        )
        second = self.client.post(
            "/api/v1/chat/messages/",
            {
                "text": "retry",
                "client_msg_id": "location-duplicate",
                "location": {"place_label": "Osaka"},
            },
            format="json",
        )

        self.assertEqual(first.status_code, 201, first.content)
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(UserSituation.objects.get(tenant=self.tenant).current_place_label, "Fukuoka")
        push_user_md.assert_called_once()

    @patch("apps.tenants.situation.record_place_observation")
    @patch("apps.router.pending_queue.httpx.post")
    def test_no_location_object_leaves_existing_chat_behavior_unchanged(self, mock_post, record_place):
        mock_post.return_value = _ok_chat_response("hi")
        resp = self.client.post(
            "/api/v1/chat/messages/",
            {"text": "ordinary message", "client_msg_id": "no-location"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        record_place.assert_not_called()

    @patch("apps.orchestrator.workspace_envelope.push_user_md_in_background")
    @patch("apps.router.pending_queue.httpx.post")
    def test_location_is_ignored_when_situational_context_flag_is_off(self, mock_post, push_user_md):
        mock_post.return_value = _ok_chat_response("hi")
        resp = self.client.post(
            "/api/v1/chat/messages/",
            {
                "text": "ordinary message",
                "client_msg_id": "location-flag-off",
                "location": {"place_label": "Fukuoka"},
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertFalse(UserSituation.objects.filter(tenant=self.tenant).exists())
        push_user_md.assert_not_called()

    def test_error_terminalization_is_exposed_and_same_id_repost_does_not_reenqueue(self):
        turn = AppChatMessage.objects.create(
            tenant=self.tenant,
            user=self.user,
            thread=ChatThread.objects.create(tenant=self.tenant, user=self.user, is_main=True, title="Main"),
            client_msg_id="terminal-retry",
            user_text="please retry",
            status=AppChatMessage.Status.PENDING,
            partial_text="residual partial",
        )
        queue_row = PendingMessage.objects.create(
            tenant=self.tenant,
            channel=PendingMessage.Channel.IOS,
            channel_user_id=str(turn.thread_id),
            payload={
                "message_text": "please retry",
                "user_param": f"thread:{turn.thread_id}",
                "user_timezone": "UTC",
                "client_msg_id": "terminal-retry",
            },
            delivery_attempts=3,
        )

        from apps.router.pending_queue import drain_pending_messages_for_tenant_task

        drain_pending_messages_for_tenant_task(str(self.tenant.id), "ios", str(turn.thread_id))

        detail = self.client.get("/api/v1/chat/messages/terminal-retry/")
        self.assertEqual(detail.status_code, 200, detail.content)
        self.assertEqual(detail.data["status"], "error")
        self.assertEqual(detail.data["error"], "dropped")
        self.assertEqual(detail.data["partial_text"], "")
        self.assertEqual(detail.data["partial_seq"], 0)

        history = self.client.get(f"/api/v1/chat/threads/{turn.thread_id}/messages/")
        self.assertEqual(history.status_code, 200, history.content)
        terminal = history.data["messages"][0]
        self.assertEqual(terminal["status"], "error")
        self.assertEqual(terminal["error"], "dropped")
        self.assertEqual(terminal["partial_text"], "")

        queue_count = PendingMessage.objects.filter(tenant=self.tenant).count()
        replay = self.client.post(
            "/api/v1/chat/messages/",
            {"text": "same attachment retry", "client_msg_id": "terminal-retry"},
            format="json",
        )
        self.assertEqual(replay.status_code, 200, replay.content)
        self.assertEqual(replay.data["status"], "error")
        self.assertEqual(replay.data["error"], "dropped")
        self.assertEqual(PendingMessage.objects.filter(tenant=self.tenant).count(), queue_count)
        self.assertTrue(PendingMessage.objects.filter(id=queue_row.id).exists())

    @patch("apps.router.pending_queue.httpx.post")
    def test_main_thread_is_default_and_shared(self, mock_post):
        mock_post.return_value = _ok_chat_response("ok")

        self.client.post("/api/v1/chat/messages/", {"text": "one", "client_msg_id": "a"}, format="json")
        self.client.post("/api/v1/chat/messages/", {"text": "two", "client_msg_id": "b"}, format="json")

        # Both default to the single shared main thread. The queue rows are
        # hard-deleted after drain (PR-3), so assert on the wire: both gateway
        # POSTs must carry the SAME thread-scoped user param.
        self.assertEqual(ChatThread.objects.filter(tenant=self.tenant, is_main=True).count(), 1)
        params = {c.kwargs["json"]["user"] for c in mock_post.call_args_list}
        self.assertEqual(len(params), 1)  # same thread → same user_param
        main = ChatThread.objects.get(tenant=self.tenant, is_main=True)
        self.assertEqual(params, {f"thread:{main.id}"})

    @patch("apps.router.pending_queue.httpx.post")
    def test_named_thread_has_own_session(self, mock_post):
        mock_post.return_value = _ok_chat_response("ok")

        created = self.client.post("/api/v1/chat/threads/", {"title": "Work"}, format="json")
        self.assertEqual(created.status_code, 201, created.content)
        thread_id = created.data["id"]
        self.assertFalse(created.data["is_main"])

        self.client.post(
            "/api/v1/chat/messages/",
            {"text": "work stuff", "thread_id": thread_id, "client_msg_id": "w1"},
            format="json",
        )
        # Queue rows are hard-deleted after drain (PR-3) — the wire POST is the
        # durable proof the named thread got its own OpenClaw session.
        self.assertEqual(mock_post.call_args.kwargs["json"]["user"], f"thread:{thread_id}")

    @patch("apps.router.chat_views.check_budget", return_value="personal")
    @patch("apps.router.pending_queue.httpx.post")
    def test_budget_gate_blocks_enqueue(self, mock_post, _mock_budget):
        resp = self.client.post(
            "/api/v1/chat/messages/",
            {"text": "spendy", "client_msg_id": "z1"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data["status"], "error")
        self.assertEqual(resp.data["error"], "budget_exhausted")
        # No work enqueued, no gateway call.
        self.assertEqual(PendingMessage.objects.filter(tenant=self.tenant).count(), 0)
        mock_post.assert_not_called()

    def test_requires_auth(self):
        anon = APIClient()
        resp = anon.post("/api/v1/chat/messages/", {"text": "hi"}, format="json")
        self.assertIn(resp.status_code, (401, 403))


@override_settings(NBHD_INTERNAL_API_KEY="test-key")
class IOSChatQuickReplyTest(TestCase):
    """The [[quick-replies: A | B | C]] marker end-to-end: the agent's raw
    reply text carries it, the drain (_clean_assistant_text_for_app) parses +
    strips it, and the polling client sees a clean reply_text plus a
    quick_replies list."""

    def setUp(self):
        self.user = _make_user()
        self.tenant = _make_tenant(self.user)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _ask(self, ai_reply: str, cid: str = "q1"):
        with patch("apps.router.pending_queue.httpx.post") as mock_post:
            mock_post.return_value = _ok_chat_response(ai_reply)
            resp = self.client.post(
                "/api/v1/chat/messages/",
                {"text": "save both?", "client_msg_id": cid},
                format="json",
            )
        self.assertEqual(resp.status_code, 201, resp.content)
        return self.client.get(f"/api/v1/chat/messages/{cid}/")

    def test_three_labels_parsed_stripped_and_stored(self):
        poll = self._ask("Save both changes?\n[[quick-replies: Save both | Change something | No thanks]]")
        self.assertEqual(poll.data["reply_text"], "Save both changes?")
        self.assertEqual(poll.data["quick_replies"], ["Save both", "Change something", "No thanks"])
        self.assertNotIn("quick-replies", poll.data["reply_text"])

    def test_one_label_parsed(self):
        poll = self._ask("Want a recap?\n[[quick-replies: Yes]]")
        self.assertEqual(poll.data["reply_text"], "Want a recap?")
        self.assertEqual(poll.data["quick_replies"], ["Yes"])

    def test_two_labels_parsed(self):
        poll = self._ask("Keep going?\n[[quick-replies: Yes | No]]")
        self.assertEqual(poll.data["quick_replies"], ["Yes", "No"])

    def test_no_marker_quick_replies_is_null(self):
        poll = self._ask("Just a normal reply, nothing special.")
        self.assertIsNone(poll.data["quick_replies"])
        self.assertEqual(poll.data["reply_text"], "Just a normal reply, nothing special.")

    def test_malformed_marker_stripped_but_no_buttons(self):
        # 4 labels — over the 3-label cap. Never shown raw; no buttons stored.
        poll = self._ask("Pick one:\n[[quick-replies: A | B | C | D]]")
        self.assertEqual(poll.data["reply_text"], "Pick one:")
        self.assertIsNone(poll.data["quick_replies"])
        self.assertNotIn("quick-replies", poll.data["reply_text"])

    def test_marker_mid_text_is_left_as_plain_text(self):
        # Not on the final line → not parsed at all; passes through verbatim
        # (rehydration/insight/chart stripping don't touch it either).
        reply = "Note: [[quick-replies: Yes | No]] is a debug artifact. Ignore it."
        poll = self._ask(reply)
        self.assertIsNone(poll.data["quick_replies"])
        self.assertIn("[[quick-replies: Yes | No]]", poll.data["reply_text"])

    def test_labels_rehydrated_at_both_seams(self):
        # F1: labels are parsed pre-rehydration (placeholder space, alongside
        # reply_text) but must be REHYDRATED to real values at both
        # owner-facing read seams — the detail poll AND the ?since= feed —
        # exactly like reply_text already is. A raw "[PERSON_1]" button would
        # both look wrong next to a rehydrated reply and, if tapped, send the
        # literal placeholder string back as the user's next message.
        self.tenant.pii_entity_map = {"[PERSON_1]": "Alice"}
        self.tenant.save(update_fields=["pii_entity_map"])

        poll = self._ask("Should I text them?\n[[quick-replies: Text [PERSON_1] | No thanks]]", cid="qr-rehydrate")
        self.assertEqual(poll.data["quick_replies"], ["Text Alice", "No thanks"])
        self.assertNotIn("[PERSON_1]", str(poll.data["quick_replies"]))

        since = self.client.get("/api/v1/chat/messages/")
        assistant_row = next(
            row
            for row in since.data["messages"]
            if row.get("client_msg_id") == "qr-rehydrate" and row["role"] == "assistant"
        )
        self.assertEqual(assistant_row["quick_replies"], ["Text Alice", "No thanks"])

    def test_rehydration_overflow_drops_buttons_but_reply_text_unaffected(self):
        # A label short enough pre-rehydration (placeholder space) can overflow
        # MAX_LABEL_LEN once the real value is substituted in. The whole set
        # must be DROPPED (never truncated — displayed text and tap-sent text
        # must stay identical) while reply_text rehydrates normally either way.
        self.tenant.pii_entity_map = {"[PERSON_1]": "A Very Long Full Legal Name Indeed"}
        self.tenant.save(update_fields=["pii_entity_map"])

        with self.assertLogs("apps.router.quick_replies", level="WARNING") as cm:
            poll = self._ask("Reach out to [PERSON_1]?\n[[quick-replies: Call [PERSON_1]]]", cid="qr-overflow")

        self.assertIsNone(poll.data["quick_replies"])
        self.assertEqual(poll.data["reply_text"], "Reach out to A Very Long Full Legal Name Indeed?")
        overflow_records = [r for r in cm.records if getattr(r, "reason", None) == "rehydration_overflow"]
        self.assertTrue(overflow_records)
        # The telemetry sample must stay PLACEHOLDER-space (no PII in logs) even
        # though the drop decision is made on the REHYDRATED length.
        for record in overflow_records:
            self.assertIn("[PERSON_1]", record.sample)
            self.assertNotIn("A Very Long Full Legal Name Indeed", record.sample)


@override_settings(NBHD_INTERNAL_API_KEY="test-key")
class IOSChatJournalLinkTest(TestCase):
    """The [[journal-link: kind|slug|title]] marker end-to-end: the agent's raw
    reply carries it, the drain (_clean_assistant_text_for_app) parses + strips
    it, and the polling client sees a clean reply_text plus a journal_link dict
    that iOS renders as a "View in Journal" chip."""

    def setUp(self):
        from apps.journal.models import Document

        self.user = _make_user()
        self.tenant = _make_tenant(self.user)
        Document.objects.bulk_create(
            [
                Document(
                    tenant=self.tenant,
                    kind=Document.Kind.DAILY,
                    slug="2026-07-13",
                    title="Morning Report",
                    markdown="# 2026-07-13",
                ),
                Document(
                    tenant=self.tenant,
                    kind=Document.Kind.GOAL,
                    slug="reconnect",
                    title="Reconnect",
                    markdown="# Reconnect",
                ),
            ]
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _ask(self, ai_reply: str, cid: str = "j1"):
        with patch("apps.router.pending_queue.httpx.post") as mock_post:
            mock_post.return_value = _ok_chat_response(ai_reply)
            resp = self.client.post(
                "/api/v1/chat/messages/",
                {"text": "log my note", "client_msg_id": cid},
                format="json",
            )
        self.assertEqual(resp.status_code, 201, resp.content)
        return self.client.get(f"/api/v1/chat/messages/{cid}/")

    def test_valid_link_parsed_stripped_and_stored(self):
        poll = self._ask("Logged today's note.\n[[journal-link: daily|2026-07-13|Morning Report]]")
        self.assertEqual(poll.data["reply_text"], "Logged today's note.")
        self.assertEqual(
            poll.data["journal_link"],
            {"kind": "daily", "slug": "2026-07-13", "title": "Morning Report"},
        )
        self.assertNotIn("journal-link", poll.data["reply_text"])

    def test_midmessage_link_with_final_quick_replies_extracts_both(self):
        with self.assertLogs("apps.router.journal_link", level="INFO") as cm:
            poll = self._ask(
                "It's live on the card. Here's the link:\n"
                "[[journal-link: daily|2026-07-13|Morning Report]]\n"
                "Good luck tomorrow.\n"
                "[[quick-replies: Open my journal | Thanks]]",
                cid="jl-mid-quick",
            )

        self.assertEqual(
            poll.data["reply_text"],
            "It's live on the card. Here's the link:\nGood luck tomorrow.",
        )
        self.assertEqual(
            poll.data["journal_link"],
            {"kind": "daily", "slug": "2026-07-13", "title": "Morning Report"},
        )
        self.assertEqual(poll.data["quick_replies"], ["Open my journal", "Thanks"])
        self.assertTrue(any(record.reason == "nonfinal_placement" for record in cm.records))
        self.assertNotIn("journal-link", poll.data["reply_text"])

    def test_no_marker_journal_link_is_null(self):
        poll = self._ask("Just a normal reply, nothing special.")
        self.assertIsNone(poll.data["journal_link"])
        self.assertEqual(poll.data["reply_text"], "Just a normal reply, nothing special.")

    def test_malformed_marker_stripped_but_no_link(self):
        # 'note' is not a real Document.Kind — never shown raw; no link stored.
        poll = self._ask("Saved.\n[[journal-link: note|some-slug|Title]]")
        self.assertEqual(poll.data["reply_text"], "Saved.")
        self.assertIsNone(poll.data["journal_link"])
        self.assertNotIn("journal-link", poll.data["reply_text"])

    def test_marker_mid_text_is_left_as_plain_text(self):
        reply = "Note: [[journal-link: daily|2026-07-13|Report]] is a debug artifact. Ignore it."
        poll = self._ask(reply)
        self.assertIsNone(poll.data["journal_link"])
        self.assertIn("[[journal-link: daily|2026-07-13|Report]]", poll.data["reply_text"])

    def test_title_rehydrated_at_both_seams(self):
        # title is parsed pre-rehydration (placeholder space, alongside
        # reply_text) but must be REHYDRATED at both owner-facing read seams —
        # the detail poll AND the ?since= feed — exactly like reply_text.
        self.tenant.pii_entity_map = {"[PERSON_1]": "Alice"}
        self.tenant.save(update_fields=["pii_entity_map"])

        poll = self._ask(
            "Saved your goal.\n[[journal-link: goal|reconnect|Reconnect with [PERSON_1]]]",
            cid="jl-rehydrate",
        )
        self.assertEqual(poll.data["journal_link"]["title"], "Reconnect with Alice")
        self.assertNotIn("[PERSON_1]", str(poll.data["journal_link"]))

        since = self.client.get("/api/v1/chat/messages/")
        assistant_row = next(
            row
            for row in since.data["messages"]
            if row.get("client_msg_id") == "jl-rehydrate" and row["role"] == "assistant"
        )
        self.assertEqual(assistant_row["journal_link"]["title"], "Reconnect with Alice")

    def test_marker_only_reply_still_emits_chip_row(self):
        # A reply that is ENTIRELY the marker line: reply_text strips to empty,
        # but the chip must not silently vanish from the feed.
        poll = self._ask("[[journal-link: daily|2026-07-13|Morning Report]]", cid="jl-only")
        self.assertEqual(poll.data["reply_text"], "")
        self.assertIsNotNone(poll.data["journal_link"])

        since = self.client.get("/api/v1/chat/messages/")
        assistant_row = next(
            row
            for row in since.data["messages"]
            if row.get("client_msg_id") == "jl-only" and row["role"] == "assistant"
        )
        self.assertEqual(assistant_row["journal_link"]["slug"], "2026-07-13")


@override_settings(NBHD_INTERNAL_API_KEY="test-key")
class IOSChatImageTest(TestCase):
    """Inbound image ingress: an app-uploaded photo is stored on the tenant
    share and referenced from the LLM-bound text via the ``[Photo attached:
    <path>]`` marker — the same path the Telegram poller uses — with the bytes
    NEVER inlined in the queue payload. The agent's built-in ``image`` tool then
    reads the local file (see ``CONTINUITY_image_upload.md``)."""

    _FAKE_STORE = (
        "/home/node/.openclaw/workspace/media/inbound/photo_test.jpg",
        "workspace/media/inbound/photo_test.jpg",
    )

    def setUp(self):
        self.user = _make_user()
        self.tenant = _make_tenant(self.user)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    @patch("apps.router.chat_views.store_inbound_image")
    @patch("apps.router.pending_queue.httpx.post")
    def test_image_only_turn_stores_and_marks(self, mock_post, mock_store):
        side_effect, captured = _snapshot_pending_on_post(self.tenant, "It's a cat.")
        mock_post.side_effect = side_effect
        mock_store.return_value = self._FAKE_STORE

        resp = self.client.post(
            "/api/v1/chat/messages/",
            {"image": _b64(_JPEG_BYTES), "client_msg_id": "img1"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertTrue(resp.data["has_image"])

        # Stored exactly once, with the DECODED bytes + the SNIFFED extension
        # (jpg from magic bytes, not a client-claimed mime).
        mock_store.assert_called_once()
        call_args = mock_store.call_args.args
        self.assertEqual(call_args[1], _JPEG_BYTES)
        self.assertEqual(call_args[2], "jpg")

        # Queue-payload shape, snapshotted at gateway-POST time (the row is
        # hard-deleted once the drain completes — PR-3).
        pmsg = captured["pmsg"]
        self.assertEqual(pmsg.channel, PendingMessage.Channel.IOS)
        # Container-path marker baked into the LLM-bound text; is_image set so
        # the row is a forced singleton (marker survives a cold-start burst).
        # Built via the shared attachment_marker helper — same call both
        # channels make — so this pins the exact untrusted-content framing,
        # not just a bare path.
        self.assertIn(
            attachment_marker("photo", "/home/node/.openclaw/workspace/media/inbound/photo_test.jpg"),
            pmsg.payload["message_text"],
        )
        self.assertIn("UNTRUSTED DATA", pmsg.payload["message_text"])
        self.assertTrue(pmsg.payload["is_image"])
        # Bytes NEVER ride the payload, and the user-facing excerpt has no marker.
        self.assertNotIn("image", pmsg.payload)
        self.assertNotIn("Photo attached", pmsg.user_text)
        # Delivered → hard-deleted (PR-3).
        self.assertFalse(PendingMessage.objects.filter(tenant=self.tenant).exists())

        # The model records the share-relative path for the turn.
        turn = AppChatMessage.objects.get(client_msg_id="img1")
        self.assertEqual(turn.attachment_path, "workspace/media/inbound/photo_test.jpg")

    @patch("apps.router.chat_views.store_inbound_image")
    @patch("apps.router.pending_queue.httpx.post")
    def test_image_with_caption_preserves_both(self, mock_post, mock_store):
        side_effect, captured = _snapshot_pending_on_post(self.tenant)
        mock_post.side_effect = side_effect
        mock_store.return_value = self._FAKE_STORE

        resp = self.client.post(
            "/api/v1/chat/messages/",
            {"text": "what is this?", "image": _b64(_PNG_BYTES), "client_msg_id": "img2"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(mock_store.call_args.args[2], "png")  # sniffed png

        # Snapshotted at gateway-POST time (row hard-deleted post-drain, PR-3).
        pmsg = captured["pmsg"]
        marked = pmsg.payload["message_text"]
        self.assertIn("[Photo attached:", marked)
        self.assertIn("what is this?", marked)
        # Caption is the user-facing excerpt, verbatim.
        self.assertEqual(pmsg.user_text, "what is this?")
        self.assertEqual(AppChatMessage.objects.get(client_msg_id="img2").user_text, "what is this?")

    def test_invalid_base64_rejected(self):
        resp = self.client.post(
            "/api/v1/chat/messages/",
            {"image": "!!! definitely not base64 !!!", "client_msg_id": "bad1"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["error"], "invalid_image")
        self.assertEqual(PendingMessage.objects.filter(tenant=self.tenant).count(), 0)

    def test_unsupported_type_rejected(self):
        resp = self.client.post(
            "/api/v1/chat/messages/",
            {"image": _b64(_NOT_IMAGE), "client_msg_id": "bad2"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["error"], "unsupported_image_type")

    def test_oversized_image_rejected(self):
        big = b"\xff\xd8\xff" + b"\x00" * (MAX_APP_IMAGE_BYTES + 50_000)
        resp = self.client.post(
            "/api/v1/chat/messages/",
            {"image": _b64(big), "client_msg_id": "big1"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["error"], "image_too_large")

    def test_neither_text_nor_image_rejected(self):
        resp = self.client.post("/api/v1/chat/messages/", {"client_msg_id": "none1"}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["error"], "empty_message")

    @patch("apps.router.chat_views.store_inbound_image")
    @patch("apps.router.pending_queue.httpx.post")
    def test_idempotent_image_retry_stores_once(self, mock_post, mock_store):
        mock_post.return_value = _ok_chat_response("ok")
        mock_store.return_value = self._FAKE_STORE

        body = {"image": _b64(_JPEG_BYTES), "client_msg_id": "dup-img"}
        first = self.client.post("/api/v1/chat/messages/", body, format="json")
        self.assertEqual(first.status_code, 201, first.content)
        second = self.client.post("/api/v1/chat/messages/", body, format="json")
        # Replay → 200, and the share write happened exactly ONCE.
        self.assertEqual(second.status_code, 200, second.content)
        mock_store.assert_called_once()
        self.assertEqual(AppChatMessage.objects.filter(client_msg_id="dup-img").count(), 1)

    @patch("apps.router.chat_views.check_budget", return_value="personal")
    @patch("apps.router.chat_views.store_inbound_image")
    @patch("apps.router.pending_queue.httpx.post")
    def test_over_budget_image_not_stored(self, mock_post, mock_store, _mock_budget):
        resp = self.client.post(
            "/api/v1/chat/messages/",
            {"image": _b64(_JPEG_BYTES), "client_msg_id": "ob1"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data["error"], "budget_exhausted")
        # The budget gate precedes the share write and the wake — no I/O, no send.
        mock_store.assert_not_called()
        mock_post.assert_not_called()
        self.assertEqual(PendingMessage.objects.filter(tenant=self.tenant).count(), 0)

    @patch("apps.router.chat_views.store_inbound_image", side_effect=RuntimeError("share down"))
    @patch("apps.router.pending_queue.httpx.post")
    def test_store_failure_degrades_to_text_turn(self, mock_post, mock_store):
        side_effect, captured = _snapshot_pending_on_post(self.tenant)
        mock_post.side_effect = side_effect
        resp = self.client.post(
            "/api/v1/chat/messages/",
            {"text": "look at this", "image": _b64(_JPEG_BYTES), "client_msg_id": "sf1"},
            format="json",
        )
        # The turn is NOT dropped: it's delivered as text and the agent is told
        # the photo failed (mirrors the poller's >5 MB fallback).
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertFalse(resp.data["has_image"])
        # Snapshotted at gateway-POST time (row hard-deleted post-drain, PR-3).
        pmsg = captured["pmsg"]
        self.assertIn("couldn't be processed", pmsg.payload["message_text"])
        self.assertIn("look at this", pmsg.payload["message_text"])
        # The "couldn't be processed" fallback is NOT untrusted content (there's
        # no file for the agent to read), so it stays a bare notice — no
        # attachment_marker framing.
        self.assertNotIn("UNTRUSTED DATA", pmsg.payload["message_text"])
        # Still a singleton (the bytes were valid; only the write failed).
        self.assertTrue(pmsg.payload["is_image"])
        self.assertEqual(AppChatMessage.objects.get(client_msg_id="sf1").attachment_path, "")

    def test_oversized_body_rejected_early(self):
        # DRF's JSON parser bypasses DATA_UPLOAD_MAX_MEMORY_SIZE, so the view
        # guards Content-Length before materializing the body (OOM defense).
        # The ceiling now covers a 10 MB PDF (base64 ≈ 13.4 MB), so the body
        # must exceed THAT to trip the 413 — a payload merely over the image
        # cap falls through to the per-field 400 (image_too_large) instead.
        resp = self.client.post(
            "/api/v1/chat/messages/",
            {"document": "A" * 14_500_000, "client_msg_id": "huge1"},
            format="json",
        )
        self.assertEqual(resp.status_code, 413)
        self.assertEqual(PendingMessage.objects.filter(tenant=self.tenant).count(), 0)

    @patch("apps.router.chat_views.store_inbound_image")
    @patch("apps.router.pending_queue.httpx.post")
    def test_line_wrapped_base64_accepted(self, mock_post, mock_store):
        # RFC 2045 line-wrapped base64 (embedded newlines) must still decode.
        mock_post.return_value = _ok_chat_response("ok")
        mock_store.return_value = self._FAKE_STORE
        raw = _b64(_JPEG_BYTES)
        wrapped = raw[:8] + "\n" + raw[8:]
        resp = self.client.post(
            "/api/v1/chat/messages/",
            {"image": wrapped, "client_msg_id": "wrap1"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        mock_store.assert_called_once()

    @patch("apps.router.chat_views.store_inbound_image")
    @patch("apps.router.pending_queue.httpx.post")
    def test_retry_with_corrupt_image_replays(self, mock_post, mock_store):
        # Idempotency runs BEFORE image decode: a retry with a known id must
        # replay the stored turn even if the resent image body is now corrupt.
        mock_post.return_value = _ok_chat_response("ok")
        mock_store.return_value = self._FAKE_STORE
        first = self.client.post(
            "/api/v1/chat/messages/",
            {"image": _b64(_JPEG_BYTES), "client_msg_id": "rr1"},
            format="json",
        )
        self.assertEqual(first.status_code, 201, first.content)
        second = self.client.post(
            "/api/v1/chat/messages/",
            {"image": "!!! corrupt !!!", "client_msg_id": "rr1"},
            format="json",
        )
        self.assertEqual(second.status_code, 200, second.content)  # replay, not 400
        mock_store.assert_called_once()  # not re-stored

    @patch("apps.pii.redactor._redact_user_message", return_value="REDACTED")
    @patch("apps.router.pending_queue.httpx.post")
    def test_coalesce_excerpt_is_redacted(self, mock_post, _mock_redact):
        # The queue-row excerpt feeds the coalesced-batch rebuild, so it must be
        # REDACTED (a raw excerpt would leak PII to the model on a cold-start
        # burst). The verbatim text stays on AppChatMessage for the display feed.
        side_effect, captured = _snapshot_pending_on_post(self.tenant)
        mock_post.side_effect = side_effect
        resp = self.client.post(
            "/api/v1/chat/messages/",
            {"text": "my number is 555-1234", "client_msg_id": "red1"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        # Snapshotted at gateway-POST time (row hard-deleted post-drain, PR-3).
        self.assertEqual(captured["pmsg"].user_text, "REDACTED")
        self.assertEqual(AppChatMessage.objects.get(client_msg_id="red1").user_text, "my number is 555-1234")

    def test_image_row_is_forced_singleton(self):
        # A coalesced batch rebuilds content from row.user_text (no marker), so
        # an image row MUST stay a singleton or the photo is silently dropped.
        from apps.router.pending_queue import _claim_pending_batch_for_key

        key = "thread-x"
        img = PendingMessage.objects.create(
            tenant=self.tenant,
            channel=PendingMessage.Channel.IOS,
            channel_user_id=key,
            payload={"message_text": "[Photo attached: /p.jpg]\nlook", "is_image": True},
        )
        PendingMessage.objects.create(
            tenant=self.tenant,
            channel=PendingMessage.Channel.IOS,
            channel_user_id=key,
            payload={"message_text": "and this too"},
        )
        batch, info = _claim_pending_batch_for_key(self.tenant, PendingMessage.Channel.IOS, key, 30.0)
        self.assertEqual([m.id for m in batch], [img.id])  # text NOT folded in
        self.assertEqual(info, {})

    def test_text_head_breaks_before_image_tail(self):
        from apps.router.pending_queue import _claim_pending_batch_for_key

        key = "thread-y"
        head = PendingMessage.objects.create(
            tenant=self.tenant,
            channel=PendingMessage.Channel.IOS,
            channel_user_id=key,
            payload={"message_text": "first"},
        )
        PendingMessage.objects.create(
            tenant=self.tenant,
            channel=PendingMessage.Channel.IOS,
            channel_user_id=key,
            payload={"message_text": "[Photo attached: /p.jpg]\nsecond", "is_image": True},
        )
        batch, _info = _claim_pending_batch_for_key(self.tenant, PendingMessage.Channel.IOS, key, 30.0)
        # The batch ends at the image boundary: only the text head is claimed.
        self.assertEqual([m.id for m in batch], [head.id])


@override_settings(NBHD_INTERNAL_API_KEY="test-key")
class IOSChatDocumentTest(TestCase):
    """Inbound PDF ingress: an app-uploaded document is stored on the tenant
    share as ``doc_<hash>.pdf`` and referenced from the LLM-bound text via the
    ``[Document attached: <path>]`` marker — bytes NEVER inlined in the queue
    payload. The agent's built-in ``pdf`` tool then reads the local file. Mirrors
    the image ingress; the same one-attachment-per-turn ``attachment_path``
    column is reused."""

    _FAKE_STORE = (
        "/home/node/.openclaw/workspace/media/inbound/doc_test.pdf",
        "workspace/media/inbound/doc_test.pdf",
    )

    def setUp(self):
        self.user = _make_user()
        self.tenant = _make_tenant(self.user)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    @patch("apps.router.chat_views.store_inbound_document")
    @patch("apps.router.pending_queue.httpx.post")
    def test_document_only_turn_stores_and_marks(self, mock_post, mock_store):
        side_effect, captured = _snapshot_pending_on_post(self.tenant, "It's a contract.")
        mock_post.side_effect = side_effect
        mock_store.return_value = self._FAKE_STORE

        resp = self.client.post(
            "/api/v1/chat/messages/",
            {"document": _b64(_PDF_BYTES), "client_msg_id": "doc1"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertTrue(resp.data["has_document"])
        self.assertFalse(resp.data["has_image"])

        # Stored exactly once, with the DECODED bytes + the SNIFFED extension
        # (pdf from magic bytes, not a client-claimed mime).
        mock_store.assert_called_once()
        call_args = mock_store.call_args.args
        self.assertEqual(call_args[1], _PDF_BYTES)
        self.assertEqual(call_args[2], "pdf")

        # Queue-payload shape, snapshotted at gateway-POST time (the row is
        # hard-deleted once the drain completes — PR-3).
        pmsg = captured["pmsg"]
        self.assertEqual(pmsg.channel, PendingMessage.Channel.IOS)
        # Container-path marker baked into the LLM-bound text; is_document set so
        # the row is a forced singleton (marker survives a cold-start burst).
        # Built via the shared attachment_marker helper — pins the exact
        # untrusted-content framing, not just a bare path. QStash is unconfigured
        # in tests, so async extraction is NOT enqueued and the marker keeps its
        # original in-turn form (the agent reads the PDF itself).
        self.assertIn(
            attachment_marker("document", "/home/node/.openclaw/workspace/media/inbound/doc_test.pdf"),
            pmsg.payload["message_text"],
        )
        self.assertIn("UNTRUSTED DATA", pmsg.payload["message_text"])
        self.assertNotIn("extraction is running in the background", pmsg.payload["message_text"])
        self.assertTrue(pmsg.payload["is_document"])
        # Bytes NEVER ride the payload, and the user-facing excerpt has no marker.
        self.assertNotIn("document", pmsg.payload)
        self.assertNotIn("Document attached", pmsg.user_text)
        # Delivered → hard-deleted (PR-3).
        self.assertFalse(PendingMessage.objects.filter(tenant=self.tenant).exists())

        turn = AppChatMessage.objects.get(client_msg_id="doc1")
        self.assertEqual(turn.attachment_path, "workspace/media/inbound/doc_test.pdf")

    @patch("apps.router.chat_views.store_inbound_document")
    @patch("apps.router.pending_queue.httpx.post")
    def test_document_with_caption_preserves_both(self, mock_post, mock_store):
        side_effect, captured = _snapshot_pending_on_post(self.tenant)
        mock_post.side_effect = side_effect
        mock_store.return_value = self._FAKE_STORE

        resp = self.client.post(
            "/api/v1/chat/messages/",
            {"text": "summarize this", "document": _b64(_PDF_BYTES), "client_msg_id": "doc2"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)

        # Snapshotted at gateway-POST time (row hard-deleted post-drain, PR-3).
        pmsg = captured["pmsg"]
        marked = pmsg.payload["message_text"]
        self.assertIn("[Document attached:", marked)
        self.assertIn("summarize this", marked)
        self.assertEqual(pmsg.user_text, "summarize this")
        self.assertEqual(AppChatMessage.objects.get(client_msg_id="doc2").user_text, "summarize this")

    def test_invalid_base64_document_rejected(self):
        resp = self.client.post(
            "/api/v1/chat/messages/",
            {"document": "!!! definitely not base64 !!!", "client_msg_id": "dbad1"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["error"], "invalid_document")
        self.assertEqual(PendingMessage.objects.filter(tenant=self.tenant).count(), 0)

    def test_non_pdf_document_rejected(self):
        # A renamed ZIP (or any non-PDF magic) must never be stored as .pdf.
        resp = self.client.post(
            "/api/v1/chat/messages/",
            {"document": _b64(_NOT_PDF), "client_msg_id": "dbad2"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["error"], "unsupported_document_type")

    def test_image_bytes_as_document_rejected(self):
        # The document gate is PDF-only: a JPEG in the document field is rejected.
        resp = self.client.post(
            "/api/v1/chat/messages/",
            {"document": _b64(_JPEG_BYTES), "client_msg_id": "dbad3"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["error"], "unsupported_document_type")

    def test_oversized_document_rejected(self):
        big = b"%PDF-1.7\n" + b"\x00" * (MAX_APP_DOCUMENT_BYTES + 50_000)
        resp = self.client.post(
            "/api/v1/chat/messages/",
            {"document": _b64(big), "client_msg_id": "dbig1"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["error"], "document_too_large")

    @patch("apps.router.chat_views.store_inbound_document")
    @patch("apps.router.chat_views.store_inbound_image")
    def test_image_and_document_together_rejected(self, mock_img, mock_doc):
        # One attachment per turn: attachment_path is a single column.
        resp = self.client.post(
            "/api/v1/chat/messages/",
            {"image": _b64(_JPEG_BYTES), "document": _b64(_PDF_BYTES), "client_msg_id": "both1"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["error"], "multiple_attachments")
        mock_img.assert_not_called()
        mock_doc.assert_not_called()
        self.assertEqual(PendingMessage.objects.filter(tenant=self.tenant).count(), 0)

    @patch("apps.router.chat_views.store_inbound_document")
    @patch("apps.router.pending_queue.httpx.post")
    def test_idempotent_document_retry_stores_once(self, mock_post, mock_store):
        mock_post.return_value = _ok_chat_response("ok")
        mock_store.return_value = self._FAKE_STORE

        body = {"document": _b64(_PDF_BYTES), "client_msg_id": "dup-doc"}
        first = self.client.post("/api/v1/chat/messages/", body, format="json")
        self.assertEqual(first.status_code, 201, first.content)
        second = self.client.post("/api/v1/chat/messages/", body, format="json")
        self.assertEqual(second.status_code, 200, second.content)
        mock_store.assert_called_once()
        self.assertEqual(AppChatMessage.objects.filter(client_msg_id="dup-doc").count(), 1)

    @patch("apps.router.chat_views.check_budget", return_value="personal")
    @patch("apps.router.chat_views.store_inbound_document")
    @patch("apps.router.pending_queue.httpx.post")
    def test_over_budget_document_not_stored(self, mock_post, mock_store, _mock_budget):
        resp = self.client.post(
            "/api/v1/chat/messages/",
            {"document": _b64(_PDF_BYTES), "client_msg_id": "dob1"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data["error"], "budget_exhausted")
        # The budget gate precedes the share write and the wake — no I/O, no send.
        mock_store.assert_not_called()
        mock_post.assert_not_called()
        self.assertEqual(PendingMessage.objects.filter(tenant=self.tenant).count(), 0)

    @patch("apps.router.chat_views.store_inbound_document", side_effect=RuntimeError("share down"))
    @patch("apps.router.pending_queue.httpx.post")
    def test_document_store_failure_degrades_to_text_turn(self, mock_post, mock_store):
        side_effect, captured = _snapshot_pending_on_post(self.tenant)
        mock_post.side_effect = side_effect
        resp = self.client.post(
            "/api/v1/chat/messages/",
            {"text": "read this", "document": _b64(_PDF_BYTES), "client_msg_id": "dsf1"},
            format="json",
        )
        # The turn is NOT dropped: it's delivered as text and the agent is told
        # the document failed (mirrors the image degrade path).
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertFalse(resp.data["has_document"])
        # Snapshotted at gateway-POST time (row hard-deleted post-drain, PR-3).
        pmsg = captured["pmsg"]
        self.assertIn("couldn't be processed", pmsg.payload["message_text"])
        self.assertIn("read this", pmsg.payload["message_text"])
        # No file was actually stored, so the fallback is a bare notice — no
        # attachment_marker framing.
        self.assertNotIn("UNTRUSTED DATA", pmsg.payload["message_text"])
        self.assertTrue(pmsg.payload["is_document"])
        self.assertEqual(AppChatMessage.objects.get(client_msg_id="dsf1").attachment_path, "")

    def test_document_row_is_forced_singleton(self):
        # A coalesced batch rebuilds content from row.user_text (no marker), so
        # a document row MUST stay a singleton or the PDF path is silently dropped.
        from apps.router.pending_queue import _claim_pending_batch_for_key

        key = "thread-d"
        doc = PendingMessage.objects.create(
            tenant=self.tenant,
            channel=PendingMessage.Channel.IOS,
            channel_user_id=key,
            payload={"message_text": "[Document attached: /d.pdf]\nread", "is_document": True},
        )
        PendingMessage.objects.create(
            tenant=self.tenant,
            channel=PendingMessage.Channel.IOS,
            channel_user_id=key,
            payload={"message_text": "and this too"},
        )
        batch, info = _claim_pending_batch_for_key(self.tenant, PendingMessage.Channel.IOS, key, 30.0)
        self.assertEqual([m.id for m in batch], [doc.id])  # text NOT folded in
        self.assertEqual(info, {})


@override_settings(NBHD_INTERNAL_API_KEY="test-key")
class IOSChatContextTest(TestCase):
    """The context-digest endpoint: what makes the PRIVATE on-device mode
    know who the user is. Data flows down to the device; no prompt text
    ever flows out to a model provider."""

    def setUp(self):
        self.user = _make_user()
        self.tenant = _make_tenant(self.user)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_returns_compact_digest(self):
        resp = self.client.get("/api/v1/chat/context/")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn("Current local time", resp.data["context_md"])
        self.assertEqual(resp.data["context_version"], 2)
        self.assertEqual(resp.data["max_chars"], 6000)
        self.assertIn("generated_at", resp.data)
        # Chat reads must never be HTTP-cached (same rule as message polls).
        self.assertEqual(resp["Cache-Control"], "no-store")

    def test_digest_contains_real_conversation_state(self):
        from apps.common.tenant_tz import tenant_today
        from apps.router.models import ConversationTurn

        ConversationTurn.objects.create(
            tenant=self.tenant,
            channel="telegram",
            channel_user_id="123",
            local_date=tenant_today(self.tenant),
            user_text="I aced the big interview today",
            reply_text="Congratulations!",
        )
        resp = self.client.get("/api/v1/chat/context/")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn("aced the big interview", resp.data["context_md"])

    def test_client_digest_omits_proactive_sends_and_versions_contract(self):
        from apps.common.tenant_tz import tenant_today
        from apps.router.models import ConversationTurn, ProactiveOutbound

        ConversationTurn.objects.create(
            tenant=self.tenant,
            channel="telegram",
            channel_user_id="123",
            local_date=tenant_today(self.tenant),
            user_text="Keep this ordinary conversation line",
            reply_text="It remains in the client digest.",
        )
        ProactiveOutbound.objects.create(
            tenant=self.tenant,
            channel="telegram",
            channel_user_id="123",
            message_text="This proactive line comes from the device store",
            job_name="morning_briefing",
        )

        resp = self.client.get("/api/v1/chat/context/")

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data["context_version"], 2)
        self.assertIn("Keep this ordinary conversation line", resp.data["context_md"])
        self.assertNotIn("Already sent to the user proactively", resp.data["context_md"])
        self.assertNotIn("This proactive line comes from the device store", resp.data["context_md"])

    def test_rehydrates_placeholders_and_skips_privacy_section(self):
        from apps.common.tenant_tz import tenant_today
        from apps.router.models import ConversationTurn

        self.tenant.pii_entity_map = {"[PERSON_1]": "Alice"}
        self.tenant.save(update_fields=["pii_entity_map"])
        ConversationTurn.objects.create(
            tenant=self.tenant,
            channel="telegram",
            channel_user_id="123",
            local_date=tenant_today(self.tenant),
            user_text="met [PERSON_1] for lunch",
        )

        resp = self.client.get("/api/v1/chat/context/")
        self.assertEqual(resp.status_code, 200, resp.content)
        # The device has no entity map: placeholders must arrive rehydrated…
        self.assertIn("met Alice for lunch", resp.data["context_md"])
        self.assertNotIn("[PERSON_1]", resp.data["context_md"])
        # …and the container-only placeholder instructions (which promise a
        # restoration layer that doesn't exist on this path) must be absent.
        self.assertNotIn("## Privacy Placeholders", resp.data["context_md"])

    def test_conversation_digest_survives_budget_pressure(self):
        from apps.orchestrator.envelope_registry import EnvelopeSection

        def section(key, order, body, heading=None):
            return EnvelopeSection(
                key=key,
                heading=heading or f"## {key}",
                render=lambda t: body,
                enabled=lambda t: True,
                refresh_on=(),
                order=order,
            )

        attributed_digest = (
            "Lines tagged [other chat: …] are from the user's OTHER conversations — background context only; "
            "never present them as part of the current conversation, and attribute the source chat when referencing them.\n"
            '- 23:43 — [other chat: "A long side conversation…"] user: today: real talk'
        )
        fakes = [
            section("bulky_one", 10, "z" * 600),
            section("bulky_two", 20, "z" * 600),
            section("bulky_three", 30, "z" * 600),
            section("conversation_digest", 65, attributed_digest, heading="## Conversation so far"),
        ]
        with patch("apps.orchestrator.workspace_envelope.all_sections", return_value=fakes):
            resp = self.client.get("/api/v1/chat/context/?max_chars=1000")

        self.assertEqual(resp.status_code, 200, resp.content)
        md = resp.data["context_md"]
        self.assertLessEqual(len(md), 1000)
        # The conversation digest renders LAST but is the most load-bearing
        # context for a client-side model — bulky early sections must not
        # starve it out of the budget.
        self.assertIn("today: real talk", md)
        self.assertIn('[other chat: "A long side conversation…"]', md)
        self.assertNotIn("bulky_three", md)

    def test_max_chars_is_clamped_and_respected(self):
        low = self.client.get("/api/v1/chat/context/?max_chars=50")
        self.assertEqual(low.data["max_chars"], 1000)
        self.assertLessEqual(len(low.data["context_md"]), 1000)

        high = self.client.get("/api/v1/chat/context/?max_chars=999999")
        self.assertEqual(high.data["max_chars"], 16000)

        junk = self.client.get("/api/v1/chat/context/?max_chars=banana")
        self.assertEqual(junk.data["max_chars"], 6000)

    def test_requires_auth(self):
        anon = APIClient()
        resp = anon.get("/api/v1/chat/context/")
        self.assertIn(resp.status_code, (401, 403))


@override_settings(NBHD_INTERNAL_API_KEY="test-key")
class IOSOnDeviceTurnRecordTest(TestCase):
    """Recording turns that already happened on the client's own model.

    The on-device assistant is a first-class channel: its turns land in
    thread history and the conversation digest, but nothing is enqueued to
    the tenant container and no model budget is consumed."""

    def setUp(self):
        self.user = _make_user()
        self.tenant = _make_tenant(self.user)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    @patch("apps.router.conversation_capture.schedule_user_md_refresh")
    def test_records_ready_turn_without_enqueue(self, mock_refresh):
        resp = self.client.post(
            "/api/v1/chat/turns/",
            {"text": "log my run", "reply_text": "Logged it.", "client_msg_id": "od1"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.data["status"], "ready")
        self.assertEqual(resp.data["source"], "on_device")
        self.assertEqual(resp.data["reply_text"], "Logged it.")
        self.assertIsNotNone(resp.data["replied_at"])

        # Nothing routed to the tenant container.
        self.assertEqual(PendingMessage.objects.filter(tenant=self.tenant).count(), 0)
        # The conversation digest gets the same debounced USER.md push a
        # captured Telegram/LINE turn triggers.
        mock_refresh.assert_called_once()

    def test_idempotent_on_client_msg_id(self):
        first = self.client.post(
            "/api/v1/chat/turns/",
            {"text": "one", "reply_text": "r", "client_msg_id": "dup-od"},
            format="json",
        )
        self.assertEqual(first.status_code, 201, first.content)
        second = self.client.post(
            "/api/v1/chat/turns/",
            {"text": "two", "reply_text": "r2", "client_msg_id": "dup-od"},
            format="json",
        )
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(
            AppChatMessage.objects.filter(tenant=self.tenant, client_msg_id="dup-od").count(),
            1,
        )

    def test_turn_lands_in_thread_history_and_digest(self):
        self.client.post(
            "/api/v1/chat/turns/",
            {"text": "planned tomorrow's workout offline", "reply_text": "Nice plan."},
            format="json",
        )
        main = ChatThread.objects.get(tenant=self.tenant, is_main=True)
        self.assertIsNotNone(main.last_active_at)

        history = self.client.get(f"/api/v1/chat/threads/{main.id}/messages/")
        self.assertEqual(history.status_code, 200, history.content)
        self.assertEqual(len(history.data["messages"]), 1)
        self.assertEqual(history.data["messages"][0]["source"], "on_device")

        from apps.router.conversation_capture import build_conversation_digest

        digest = build_conversation_digest(self.tenant)
        self.assertIn("planned tomorrow's workout", digest)

    @patch("apps.router.chat_views.check_budget", return_value="personal")
    def test_no_budget_gate(self, _mock_budget):
        # An over-budget tenant can still RECORD on-device turns — the reply
        # was produced on the device; no platform model spend is involved.
        resp = self.client.post(
            "/api/v1/chat/turns/",
            {"text": "offline while over budget", "reply_text": "ok"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.data["status"], "ready")

    def test_validation(self):
        empty = self.client.post("/api/v1/chat/turns/", {"reply_text": "r"}, format="json")
        self.assertEqual(empty.status_code, 400)

        # Over-long content is TRUNCATED, never rejected: the turn already
        # happened; losing the record entirely is worse than losing its tail.
        long_turn = self.client.post(
            "/api/v1/chat/turns/",
            {"text": "y" * 9000, "reply_text": "x" * 20000},
            format="json",
        )
        self.assertEqual(long_turn.status_code, 201, long_turn.content)
        self.assertEqual(len(long_turn.data["user_text"]), 8000)
        self.assertEqual(len(long_turn.data["reply_text"]), 16000)
        self.assertTrue(long_turn.data["reply_text"].endswith("… [message truncated]"))

        bad_id = self.client.post(
            "/api/v1/chat/turns/",
            {"text": "hi", "client_msg_id": "z" * 65},
            format="json",
        )
        self.assertEqual(bad_id.status_code, 400)
        self.assertEqual(bad_id.data["error"], "invalid_client_msg_id")

        bad_body = self.client.post("/api/v1/chat/turns/", ["not", "a", "dict"], format="json")
        self.assertEqual(bad_body.status_code, 400)
        self.assertEqual(bad_body.data["error"], "invalid_body")

    def test_occurred_at_backdates_outbox_delayed_turns(self):
        from datetime import timedelta

        from django.utils import timezone

        yesterday = timezone.now() - timedelta(days=1)
        resp = self.client.post(
            "/api/v1/chat/turns/",
            {
                "text": "chatted on the plane",
                "reply_text": "noted",
                "occurred_at": yesterday.isoformat(),
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        turn = AppChatMessage.objects.get(tenant=self.tenant, client_msg_id=resp.data["client_msg_id"])
        self.assertEqual(turn.created_at, yesterday)
        self.assertEqual(turn.replied_at, yesterday)

        # Unparsable / future / ancient timestamps fall back to delivery time.
        for bad in ("not-a-date", (timezone.now() + timedelta(days=2)).isoformat()):
            r = self.client.post(
                "/api/v1/chat/turns/",
                {"text": f"turn {bad}", "occurred_at": bad},
                format="json",
            )
            self.assertEqual(r.status_code, 201, r.content)
            row = AppChatMessage.objects.get(tenant=self.tenant, client_msg_id=r.data["client_msg_id"])
            self.assertGreater(row.created_at, timezone.now() - timedelta(minutes=1))

    def test_concurrent_duplicate_returns_existing(self):
        from django.db import IntegrityError

        first = self.client.post(
            "/api/v1/chat/turns/",
            {"text": "one", "client_msg_id": "race-od"},
            format="json",
        )
        self.assertEqual(first.status_code, 201, first.content)

        # Simulate losing the existence-check race: the row appears between
        # the .first() check and the INSERT.
        with (
            patch.object(AppChatMessage.objects, "filter") as mock_filter,
            patch.object(AppChatMessage.objects, "create", side_effect=IntegrityError("dup")),
        ):
            mock_filter.return_value.first.return_value = None
            resp = self.client.post(
                "/api/v1/chat/turns/",
                {"text": "one", "client_msg_id": "race-od"},
                format="json",
            )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data["client_msg_id"], "race-od")

    def test_requires_auth(self):
        anon = APIClient()
        resp = anon.post("/api/v1/chat/turns/", {"text": "hi"}, format="json")
        self.assertIn(resp.status_code, (401, 403))


@override_settings(NBHD_INTERNAL_API_KEY="test-key")
class ChatSinceFeedTest(TestCase):
    """The flat cross-channel ``GET /api/v1/chat/messages/?since=`` history feed
    (W2): unions app + Telegram/LINE + cron turns into one ascending, dedup-able,
    cursor-paginated stream so the iOS app sees every channel."""

    def setUp(self):
        from datetime import timedelta

        from django.utils import timezone

        from apps.router.models import ChatThread

        self.user = _make_user()
        self.tenant = _make_tenant(self.user)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.main = ChatThread.objects.create(tenant=self.tenant, user=self.user, is_main=True, title="Main")
        self._base = timezone.now() - timedelta(hours=2)
        self._td = timedelta

    def _at(self, minutes: int):
        return self._base + self._td(minutes=minutes)

    def _app_turn(
        self,
        *,
        cid,
        user_text,
        reply_text,
        minute,
        status="ready",
        attachment_path="",
        quick_replies=None,
        journal_link=None,
    ):
        m = AppChatMessage.objects.create(
            tenant=self.tenant,
            user=self.user,
            thread=self.main,
            client_msg_id=cid,
            user_text=user_text,
            reply_text=reply_text,
            status=status,
            attachment_path=attachment_path,
            quick_replies=quick_replies,
            journal_link=journal_link,
        )
        AppChatMessage.objects.filter(pk=m.pk).update(created_at=self._at(minute))
        return m

    def _conv_turn(self, *, channel, user_text, reply_text, minute):
        from apps.common.tenant_tz import tenant_today
        from apps.router.models import ConversationTurn

        t = ConversationTurn.objects.create(
            tenant=self.tenant,
            channel=channel,
            channel_user_id="123",
            local_date=tenant_today(self.tenant),
            user_text=user_text,
            reply_text=reply_text,
        )
        ConversationTurn.objects.filter(pk=t.pk).update(created_at=self._at(minute))
        return t

    def _cron_send(self, *, message_text, minute, journal_link=None, quick_replies=None):
        from apps.router.models import ProactiveOutbound

        p = ProactiveOutbound.objects.create(
            tenant=self.tenant,
            channel="telegram",
            channel_user_id="123",
            message_text=message_text,
            job_name="Morning Briefing",
            journal_link=journal_link,
            quick_replies=quick_replies,
        )
        ProactiveOutbound.objects.filter(pk=p.pk).update(created_at=self._at(minute))
        return p

    def _get(self, since=None, limit=None):
        params = {}
        if since is not None:
            params["since"] = since
        if limit is not None:
            params["limit"] = limit
        return self.client.get("/api/v1/chat/messages/", params)

    def test_requires_auth(self):
        resp = APIClient().get("/api/v1/chat/messages/")
        self.assertIn(resp.status_code, (401, 403))

    def test_empty_since_unions_all_channels_ascending(self):
        self._app_turn(cid="a1", user_text="ping", reply_text="pong", minute=1)
        self._conv_turn(channel="telegram", user_text="tg hi", reply_text="tg yo", minute=2)
        self._cron_send(message_text="Good morning!", minute=3)

        resp = self._get()
        self.assertEqual(resp.status_code, 200, resp.content)
        msgs = resp.data["messages"]
        # app(user, assistant) + telegram(user, assistant) + cron(assistant) = 5
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "user", "assistant", "assistant"])
        self.assertEqual([m["source"] for m in msgs], ["app", "app", "telegram", "telegram", "cron"])
        self.assertEqual(msgs[0]["text"], "ping")
        self.assertEqual(msgs[1]["text"], "pong")
        self.assertEqual(msgs[4]["text"], "Good morning!")
        # Strictly ascending created_at.
        stamps = [m["created_at"] for m in msgs]
        self.assertEqual(stamps, sorted(stamps))
        # Cursor returned for the next poll.
        self.assertIsNotNone(resp.data["cursor"])

    def test_cron_row_surfaces_journal_link_chip(self):
        # A Telegram-delivered morning report that carried a journal-link marker
        # stores it on ProactiveOutbound.journal_link; the cross-channel feed
        # surfaces it (rehydrated) as a chip on the source="cron" assistant row.
        self.tenant.pii_entity_map = {"[PERSON_1]": "Alice"}
        self.tenant.save(update_fields=["pii_entity_map"])
        self._cron_send(
            message_text="Morning, [PERSON_1].",
            minute=1,
            journal_link={"kind": "daily", "slug": "2026-07-13", "title": "Report for [PERSON_1]"},
        )

        msgs = self._get().data["messages"]
        cron_row = next(m for m in msgs if m["source"] == "cron")
        self.assertEqual(cron_row["text"], "Morning, Alice.")
        self.assertEqual(
            cron_row["journal_link"],
            {"kind": "daily", "slug": "2026-07-13", "title": "Report for Alice"},
        )

    def test_cron_row_without_journal_link_omits_key(self):
        self._cron_send(message_text="Plain check-in.", minute=1)
        cron_row = next(m for m in self._get().data["messages"] if m["source"] == "cron")
        self.assertNotIn("journal_link", cron_row)

    def test_cron_row_surfaces_rehydrated_quick_replies(self):
        self.tenant.pii_entity_map = {"[PERSON_1]": "Alice"}
        self.tenant.save(update_fields=["pii_entity_map"])
        self._cron_send(
            message_text="Want to follow up, [PERSON_1]?",
            minute=1,
            quick_replies=["Ask [PERSON_1]", "Not now"],
        )

        cron_row = next(m for m in self._get().data["messages"] if m["source"] == "cron")
        self.assertEqual(cron_row["quick_replies"], ["Ask Alice", "Not now"])

    def test_cron_marker_only_row_keeps_quick_replies(self):
        self._cron_send(
            message_text="",
            minute=1,
            quick_replies=["Tell me more"],
        )

        cron_row = next(m for m in self._get().data["messages"] if m["source"] == "cron")
        self.assertEqual(cron_row["text"], "")
        self.assertEqual(cron_row["quick_replies"], ["Tell me more"])

    def test_cron_row_drops_quick_replies_on_rehydration_overflow(self):
        self.tenant.pii_entity_map = {"[PERSON_1]": "A very long person name"}
        self.tenant.save(update_fields=["pii_entity_map"])
        self._cron_send(
            message_text="Want to follow up?",
            minute=1,
            quick_replies=["Ask [PERSON_1] today"],
        )

        with self.assertLogs("apps.router.quick_replies", level="WARNING"):
            cron_row = next(m for m in self._get().data["messages"] if m["source"] == "cron")
        self.assertNotIn("quick_replies", cron_row)

    def test_client_msg_id_on_both_device_rows_not_other_channels(self):
        self._app_turn(cid="a1", user_text="ping", reply_text="pong", minute=1)
        self._conv_turn(channel="telegram", user_text="tg hi", reply_text="tg yo", minute=2)

        msgs = self._get().data["messages"]
        app_user, app_asst, tg_user, tg_asst = msgs
        # BOTH halves of a device-originated turn carry the originating client_msg_id
        # so the client can dedup each optimistic row by (client_msg_id, role).
        self.assertEqual(app_user["client_msg_id"], "a1")
        self.assertEqual(app_asst["client_msg_id"], "a1")
        # Other-channel rows were never written locally → no correlation key.
        self.assertNotIn("client_msg_id", tg_user)
        self.assertNotIn("client_msg_id", tg_asst)

    def test_non_app_rows_map_to_main_thread(self):
        self._conv_turn(channel="line", user_text="hi", reply_text="yo", minute=1)
        self._cron_send(message_text="ping", minute=2)
        msgs = self._get().data["messages"]
        for m in msgs:
            self.assertEqual(m["thread_id"], str(self.main.id))

    def test_pending_app_turn_emits_only_user_row(self):
        # A turn still awaiting its reply shows the user's sent message but no
        # (empty) assistant row.
        self._app_turn(cid="p1", user_text="working?", reply_text="", minute=1, status="pending")
        msgs = self._get().data["messages"]
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["role"], "user")
        self.assertEqual(msgs[0]["text"], "working?")

    def test_error_app_turn_emits_only_user_row(self):
        self._app_turn(cid="e1", user_text="spendy", reply_text="", minute=1, status="error")
        msgs = self._get().data["messages"]
        self.assertEqual([m["role"] for m in msgs], ["user"])

    def test_ids_are_stable_across_calls(self):
        self._app_turn(cid="a1", user_text="ping", reply_text="pong", minute=1)
        first = [m["id"] for m in self._get().data["messages"]]
        second = [m["id"] for m in self._get().data["messages"]]
        self.assertEqual(first, second)
        self.assertEqual(len(set(first)), len(first))  # globally unique

    def test_same_timestamp_cluster_is_fully_paged(self):
        # Several turns sharing the EXACT created_at (e.g. an offline outbox
        # flushed with one occurred_at) must ALL be paged through — never
        # truncated off the fetch window and silently skipped. Regression for
        # the keyset cluster-loss bug.
        from apps.common.tenant_tz import tenant_today
        from apps.router.models import ConversationTurn

        ts = self._at(5)
        for i in range(3):
            t = ConversationTurn.objects.create(
                tenant=self.tenant,
                channel="telegram",
                channel_user_id="123",
                local_date=tenant_today(self.tenant),
                user_text=f"u{i}",
                reply_text=f"r{i}",
            )
            ConversationTurn.objects.filter(pk=t.pk).update(created_at=ts)

        # Single-shot read sees all 6 (3 turns x user+assistant).
        single = sorted(m["id"] for m in self._get(limit=100).data["messages"])
        self.assertEqual(len(single), 6)

        # A paginated walk at the smallest page size must return the SAME id set.
        seen = []
        cursor = None
        for _ in range(50):  # generous bound; converges well before this
            resp = self._get(since=cursor, limit=1)
            page = resp.data["messages"]
            if not page:
                break
            seen.extend(m["id"] for m in page)
            cursor = resp.data["cursor"]
        self.assertEqual(sorted(seen), single)
        self.assertEqual(len(seen), 6)  # no loss, no dupes

    def test_object_cursor_restarts_from_beginning(self):
        # A corrupted cursor that base64+JSON-decodes to an OBJECT (not a list)
        # must restart from the beginning, never 500 (which would wedge the
        # polling client). Regression for the uncaught-KeyError path.
        import base64
        import json

        self._app_turn(cid="a1", user_text="ping", reply_text="pong", minute=1)
        bad = base64.urlsafe_b64encode(json.dumps({"x": 1}).encode()).decode()
        resp = self._get(since=bad)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(len(resp.data["messages"]), 2)

    def test_cursor_pagination_walks_without_dupes(self):
        self._app_turn(cid="a1", user_text="ping", reply_text="pong", minute=1)
        self._conv_turn(channel="telegram", user_text="tg hi", reply_text="tg yo", minute=2)
        self._cron_send(message_text="Good morning!", minute=3)

        seen = []
        cursor = None
        for _ in range(10):  # generous bound; should converge in 3 pages
            resp = self._get(since=cursor, limit=2)
            self.assertEqual(resp.status_code, 200, resp.content)
            page = resp.data["messages"]
            if not page:
                break
            self.assertLessEqual(len(page), 2)  # server-bounded page size
            seen.extend(m["id"] for m in page)
            cursor = resp.data["cursor"]

        self.assertEqual(len(seen), 5)
        self.assertEqual(len(set(seen)), 5)  # no duplicates across pages

    def test_empty_page_echoes_cursor_and_does_not_advance(self):
        self._app_turn(cid="a1", user_text="ping", reply_text="pong", minute=1)
        first = self._get(limit=100)
        cursor = first.data["cursor"]
        self.assertEqual(len(first.data["messages"]), 2)

        # Re-poll from the tail: nothing new → empty page, SAME cursor echoed.
        again = self._get(since=cursor)
        self.assertEqual(again.data["messages"], [])
        self.assertEqual(again.data["cursor"], cursor)

    def test_limit_is_server_bounded(self):
        for i in range(5):
            self._app_turn(cid=f"a{i}", user_text=f"u{i}", reply_text=f"r{i}", minute=i + 1)
        # Ask for more than the cap → clamped (we only have 10 rows here, but the
        # request must not error and must honor the bound semantics).
        resp = self._get(limit=9999)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertLessEqual(len(resp.data["messages"]), 100)

    def test_malformed_cursor_restarts_from_beginning(self):
        self._app_turn(cid="a1", user_text="ping", reply_text="pong", minute=1)
        resp = self._get(since="not-a-valid-cursor")
        self.assertEqual(resp.status_code, 200, resp.content)
        # Lenient: garbage cursor → from the beginning, not a 4xx wedge.
        self.assertEqual(len(resp.data["messages"]), 2)

    def test_on_device_turns_appear_as_app_source(self):
        self._app_turn(cid="od1", user_text="logged offline", reply_text="noted", minute=1, status="ready")
        AppChatMessage.objects.filter(client_msg_id="od1").update(source="on_device")
        msgs = self._get().data["messages"]
        self.assertEqual([m["source"] for m in msgs], ["app", "app"])

    def test_tenant_isolation(self):
        # A different tenant's turns must never leak into this feed.
        other_user = _make_user()
        other_tenant = _make_tenant(other_user)
        from apps.common.tenant_tz import tenant_today
        from apps.router.models import ConversationTurn

        ConversationTurn.objects.create(
            tenant=other_tenant,
            channel="telegram",
            channel_user_id="999",
            local_date=tenant_today(other_tenant),
            user_text="secret",
            reply_text="leak?",
        )
        self._app_turn(cid="a1", user_text="mine", reply_text="ok", minute=1)
        msgs = self._get().data["messages"]
        texts = [m["text"] for m in msgs]
        self.assertNotIn("secret", texts)
        self.assertNotIn("leak?", texts)

    def test_feed_uses_one_query_per_channel_table(self):
        # Round-trip budget: the DB is in Sydney while Django runs in US-West, so
        # every query is a ~152ms cross-Pacific hop. The feed must issue at most
        # ONE query per channel table — boundary + forward folded into a single
        # UNION ALL — not two. Regression lock for the 6→3 query cut.
        from apps.router.chat_history import build_since_page

        self._app_turn(cid="a1", user_text="ping", reply_text="pong", minute=1)
        self._conv_turn(channel="telegram", user_text="tg hi", reply_text="tg yo", minute=2)
        self._cron_send(message_text="Good morning!", minute=3)

        # Steady-state poll: a real (non-epoch) cursor → one UNION ALL per table.
        _, cursor = build_since_page(self.tenant, str(self.main.id), cursor=None, limit=100)
        with self.assertNumQueries(3):
            build_since_page(self.tenant, str(self.main.id), cursor=cursor, limit=100)

        # From-the-beginning: the boundary is provably empty, so it's skipped and
        # each table stays a single bounded forward read.
        with self.assertNumQueries(3):
            build_since_page(self.tenant, str(self.main.id), cursor=None, limit=100)

    def test_cross_channel_same_timestamp_cluster_paged(self):
        # A tie-cluster spanning DIFFERENT channel tables at the exact same
        # microsecond must still page through losslessly. The per-table UNION
        # keeps its boundary slice unbounded, so a cluster member is never
        # truncated off the fetch window. Regression for the UNION fold.
        self._app_turn(cid="a1", user_text="au", reply_text="ar", minute=7)
        self._conv_turn(channel="telegram", user_text="cu", reply_text="cr", minute=7)
        self._cron_send(message_text="cron at seven", minute=7)

        # Single-shot read: app(user, asst) + telegram(user, asst) + cron(asst) = 5.
        single = sorted(m["id"] for m in self._get(limit=100).data["messages"])
        self.assertEqual(len(single), 5)

        # A paginated walk at the smallest page size returns the SAME id set.
        seen, cursor = [], None
        for _ in range(50):  # generous bound; converges well before this
            resp = self._get(since=cursor, limit=1)
            page = resp.data["messages"]
            if not page:
                break
            seen.extend(m["id"] for m in page)
            cursor = resp.data["cursor"]
        self.assertEqual(sorted(seen), single)
        self.assertEqual(len(seen), 5)  # no loss, no dupes

    @override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
    def test_main_thread_id_cached_skips_db_on_hit(self):
        # The immutable main-thread id is cached so steady-state polls skip the
        # get_or_create round trip to Sydney. First call warms it; the second
        # must touch the DB zero times.
        from django.core.cache import cache

        from apps.router.chat_views import _main_thread_id_cached

        cache.clear()
        first = _main_thread_id_cached(self.tenant, self.user)
        self.assertEqual(first, str(self.main.id))
        with self.assertNumQueries(0):
            second = _main_thread_id_cached(self.tenant, self.user)
        self.assertEqual(second, first)

    @override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
    def test_main_thread_cache_busted_on_delete(self):
        # Deleting the is_main thread must invalidate the cached id (post_delete
        # signal), so a delete+recreate can never serve a dangling id past the
        # next poll. Without this, the feed would label non-app rows with a
        # non-existent thread id for up to the cache TTL.
        from django.core.cache import cache

        from apps.router.chat_views import _main_thread_cache_key, _main_thread_id_cached

        cache.clear()
        first = _main_thread_id_cached(self.tenant, self.user)
        self.assertEqual(cache.get(_main_thread_cache_key(self.tenant.id)), first)

        self.main.delete()
        self.assertIsNone(cache.get(_main_thread_cache_key(self.tenant.id)))

        # Next call re-derives + re-creates the main thread with a fresh id.
        second = _main_thread_id_cached(self.tenant, self.user)
        self.assertNotEqual(second, first)

    # -- attachment flags (cross-device / reinstall attachment history) --------
    #
    # #1071 added has_image/has_document to the per-message DETAIL path but NOT
    # to this poll feed, so a user's own image/PDF turns rendered as plain text
    # on a second device or after a reinstall (which rebuild history from the
    # feed, never the detail path). These lock the feed emitting the same flags,
    # off the same shared source of truth (AppChatMessage.attachment_flags).

    def test_feed_image_turn_flags_ride_user_row(self):
        # An image turn surfaces has_image=True on its USER row (the attachment
        # is the user's inbound), so a second device renders the right bubble.
        self._app_turn(
            cid="img1",
            user_text="look at this",
            reply_text="nice shot",
            minute=1,
            attachment_path="workspace/media/inbound/photo_ab12.jpg",
        )
        user_row, asst_row = self._get().data["messages"]
        self.assertEqual(user_row["role"], "user")
        self.assertTrue(user_row["has_image"])
        self.assertFalse(user_row["has_document"])
        # The reply row carries neither flag but still emits the keys.
        self.assertFalse(asst_row["has_image"])
        self.assertFalse(asst_row["has_document"])

    def test_feed_document_turn_inverts_flags(self):
        self._app_turn(
            cid="doc1",
            user_text="read this",
            reply_text="on it",
            minute=1,
            attachment_path="workspace/media/inbound/doc_cd34.pdf",
        )
        user_row = self._get().data["messages"][0]
        self.assertTrue(user_row["has_document"])
        self.assertFalse(user_row["has_image"])

    def test_feed_text_turn_flags_both_false_but_present(self):
        # No attachment → both flags false, and ALWAYS emitted so the wire shape
        # stays uniform (new client defaults absent→false; older builds ignore).
        self._app_turn(cid="t1", user_text="just text", reply_text="ok", minute=1)
        for row in self._get().data["messages"]:
            self.assertIn("has_image", row)
            self.assertIn("has_document", row)
            self.assertFalse(row["has_image"])
            self.assertFalse(row["has_document"])

    def test_feed_other_channel_rows_carry_false_flags(self):
        # Telegram/LINE and cron rows have no attachment_path → both flags
        # present and false, matching the detail-path always-emit behaviour.
        self._conv_turn(channel="telegram", user_text="tg", reply_text="yo", minute=1)
        self._cron_send(message_text="morning", minute=2)
        rows = self._get().data["messages"]
        self.assertEqual(len(rows), 3)  # tg(user, asst) + cron(asst)
        for row in rows:
            self.assertIn("has_image", row)
            self.assertIn("has_document", row)
            self.assertFalse(row["has_image"])
            self.assertFalse(row["has_document"])

    def test_attachment_flags_property_is_case_insensitive(self):
        # The extension check lowercases the path — a '.PDF' upload is a document,
        # not an image. Pins the shared property both paths read.
        m = self._app_turn(
            cid="up1",
            user_text="x",
            reply_text="y",
            minute=1,
            attachment_path="workspace/media/inbound/DOC.PDF",
        )
        self.assertEqual(m.attachment_flags, (False, True))

    def test_detail_and_feed_flags_agree(self):
        # The per-message DETAIL serializer and this FEED both read
        # AppChatMessage.attachment_flags, so they can never disagree on a turn's
        # attachment type. Prove it for image / pdf / none — this is the whole
        # point of the shared helper (anti-drift).
        from apps.router.chat_views import _serialize_message

        cases = [
            ("workspace/media/inbound/photo.jpg", True, False),
            ("workspace/media/inbound/doc.pdf", False, True),
            ("", False, False),
        ]
        for i, (path, want_img, want_doc) in enumerate(cases):
            m = self._app_turn(
                cid=f"agree{i}",
                user_text="hi",
                reply_text="yo",
                minute=10 + i,
                attachment_path=path,
            )
            detail = _serialize_message(m)
            self.assertEqual((detail["has_image"], detail["has_document"]), (want_img, want_doc))
            feed_user = next(
                row
                for row in self._get().data["messages"]
                if row.get("client_msg_id") == f"agree{i}" and row["role"] == "user"
            )
            self.assertEqual((feed_user["has_image"], feed_user["has_document"]), (want_img, want_doc))

    def test_detail_and_feed_quick_replies_agree(self):
        # Same anti-drift shape as test_detail_and_feed_flags_agree, but for
        # quick_replies: the detail serializer always emits the key (None when
        # empty); the feed OMITS the key when empty (mirrors user_redactions/
        # reply_redactions), so compare via .get() on the feed side.
        from apps.router.chat_views import _serialize_message

        cases = [
            (["Yes", "No"], ["Yes", "No"]),
            (None, None),
        ]
        for i, (stored, want) in enumerate(cases):
            m = self._app_turn(cid=f"qr{i}", user_text="hi", reply_text="yo", minute=20 + i, quick_replies=stored)
            detail = _serialize_message(m)
            self.assertEqual(detail["quick_replies"], want)
            feed_assistant = next(
                row
                for row in self._get().data["messages"]
                if row.get("client_msg_id") == f"qr{i}" and row["role"] == "assistant"
            )
            self.assertEqual(feed_assistant.get("quick_replies"), want)

    def test_detail_and_feed_journal_link_agree(self):
        # Same anti-drift shape: the detail serializer always emits the key
        # (None when empty); the feed OMITS the key when empty, so compare via
        # .get() on the feed side.
        from apps.router.chat_views import _serialize_message

        cases = [
            (
                {"kind": "daily", "slug": "2026-07-13", "title": "Report"},
                {"kind": "daily", "slug": "2026-07-13", "title": "Report"},
            ),
            (None, None),
        ]
        for i, (stored, want) in enumerate(cases):
            m = self._app_turn(cid=f"jl{i}", user_text="hi", reply_text="yo", minute=30 + i, journal_link=stored)
            detail = _serialize_message(m)
            self.assertEqual(detail["journal_link"], want)
            feed_assistant = next(
                row
                for row in self._get().data["messages"]
                if row.get("client_msg_id") == f"jl{i}" and row["role"] == "assistant"
            )
            self.assertEqual(feed_assistant.get("journal_link"), want)


@override_settings(NBHD_INTERNAL_API_KEY="test-key", NBHD_DISABLE_BACKGROUND_THREADS=True)
class IOSDrainDropPushTest(TestCase):
    """Drain-level terminal drops must not strand an iOS turn as PENDING.

    When a queued iOS turn is dropped past the delivery-attempts cap or aged out
    as stale, the apology helpers have no channel-native push for iOS — so they
    must flip the AppChatMessage to ERROR and fire the generic 'couldn't finish'
    APNs push, or the client polls a spinner forever with no notification."""

    def setUp(self):
        from apps.router.models import ChatThread

        self.user = _make_user()
        self.tenant = _make_tenant(self.user)
        self.thread = ChatThread.objects.create(tenant=self.tenant, user=self.user, is_main=True, title="Main")

    def _pending_turn(self, cid):
        from apps.router.models import PendingMessage

        turn = AppChatMessage.objects.create(
            tenant=self.tenant,
            user=self.user,
            thread=self.thread,
            client_msg_id=cid,
            user_text="do the thing",
            status="pending",
        )
        # Unsaved is fine: the apology helper only reads .channel / .payload.
        pmsg = PendingMessage(
            tenant=self.tenant,
            channel=PendingMessage.Channel.IOS,
            channel_user_id=str(self.thread.id),
            payload={"client_msg_id": cid},
        )
        return turn, pmsg

    def test_dropped_ios_turn_errors_and_pushes(self):
        from apps.router.pending_queue import _send_apology_for_dropped_pending_message

        turn, pmsg = self._pending_turn("d1")
        with (
            patch("apps.router.push_views.notify_app_reply_error") as mock_notify,
            self.captureOnCommitCallbacks(execute=True),
        ):
            _send_apology_for_dropped_pending_message(self.tenant, pmsg)
        turn.refresh_from_db()
        self.assertEqual(turn.status, "error")
        self.assertEqual(turn.error, "dropped")
        mock_notify.assert_called_once_with(self.tenant, ["d1"])

    def test_stale_ios_turn_errors_and_pushes(self):
        from apps.router.pending_queue import _send_apology_for_stale_pending_message

        turn, pmsg = self._pending_turn("s1")
        with (
            patch("apps.router.push_views.notify_app_reply_error") as mock_notify,
            self.captureOnCommitCallbacks(execute=True),
        ):
            _send_apology_for_stale_pending_message(self.tenant, pmsg, 700.0)
        turn.refresh_from_db()
        self.assertEqual(turn.status, "error")
        self.assertEqual(turn.error, "stale")
        mock_notify.assert_called_once_with(self.tenant, ["s1"])


@override_settings(NBHD_INTERNAL_API_KEY="test-key")
class IOSChatFreshnessStampTest(TestCase):
    """iOS chat turns must stamp tenants.last_message_at — it is the idle-
    hibernation freshness signal, and before this stamp an iOS-only tenant
    looked permanently idle (hibernated mid-conversation)."""

    def setUp(self):
        self.user = _make_user()
        self.tenant = _make_tenant(self.user)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    @patch("apps.router.pending_queue.httpx.post")
    def test_chat_post_updates_last_message_at(self, mock_post):
        from django.utils import timezone

        mock_post.return_value = _ok_chat_response()
        self.assertIsNone(self.tenant.last_message_at)

        before = timezone.now()
        resp = self.client.post(
            "/api/v1/chat/messages/",
            {"text": "good morning", "client_msg_id": "stamp1"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)

        self.tenant.refresh_from_db()
        self.assertIsNotNone(self.tenant.last_message_at)
        self.assertGreaterEqual(self.tenant.last_message_at, before)


@override_settings(NBHD_INTERNAL_API_KEY="test-key")
class SinceFeedLateReplyTest(TestCase):
    """A slow assistant reply must not fall behind the since-cursor.

    The reply row used to sort by the USER turn's created_at. If a cron or
    proactive row landed (and was served, advancing the client's cursor)
    while the turn was still pending, the reply — completing later but
    stamped earlier — fell behind the strictly-monotonic watermark and was
    never served. The reply now sorts by replied_at."""

    def setUp(self):
        self.user = _make_user()
        self.tenant = _make_tenant(self.user)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_reply_landing_after_served_interleaved_row_is_still_served(self):
        from datetime import timedelta

        from django.utils import timezone

        from apps.router.models import ProactiveOutbound

        t0 = timezone.now() - timedelta(minutes=5)
        main = ChatThread.objects.create(tenant=self.tenant, user=self.user, is_main=True, title="Main")
        turn = AppChatMessage.objects.create(
            tenant=self.tenant,
            user=self.user,
            thread=main,
            client_msg_id="slowturn",
            user_text="hello?",
            status=AppChatMessage.Status.PENDING,
        )
        AppChatMessage.objects.filter(id=turn.id).update(created_at=t0)

        # An interleaved proactive row lands AFTER the user turn, while the
        # reply is still pending; the client fetches and its cursor advances
        # past it.
        nudge = ProactiveOutbound.objects.create(
            tenant=self.tenant,
            channel=ProactiveOutbound.Channel.TELEGRAM,
            channel_user_id="u1",
            message_text="don't forget your workout",
            job_name="morning-briefing",
        )
        ProactiveOutbound.objects.filter(id=nudge.id).update(created_at=t0 + timedelta(minutes=1))
        first = self.client.get("/api/v1/chat/messages/", {"limit": "50"})
        self.assertEqual(first.status_code, 200, first.content)
        cursor = first.data["cursor"]
        texts = [r["text"] for r in first.data["messages"]]
        self.assertIn("don't forget your workout", texts)

        # The slow reply completes AFTER the cursor advanced past the nudge.
        AppChatMessage.objects.filter(id=turn.id).update(
            status=AppChatMessage.Status.READY,
            reply_text="here I am — sorry for the wait",
            replied_at=t0 + timedelta(minutes=2),
        )

        nxt = self.client.get("/api/v1/chat/messages/", {"since": cursor, "limit": "50"})
        self.assertEqual(nxt.status_code, 200, nxt.content)
        texts = [r["text"] for r in nxt.data["messages"]]
        self.assertIn(
            "here I am — sorry for the wait",
            texts,
            "late-landing reply fell behind the since watermark and was dropped",
        )


@override_settings(
    NBHD_INTERNAL_API_KEY="test-key",
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "ios-chat-throttle-test",
        }
    },
)
class IOSChatSendThrottleTest(TestCase):
    """The chat SEND path (POST /chat/messages/) is per-user throttled — an
    abuse ceiling that matters now the body admits ~13.6MB attachments. The GET
    ?since= poll on the SAME view must stay unthrottled (clients hit it ~every
    30s from multiple devices). Cache overridden to LocMem so throttle history
    actually stores regardless of the ambient backend."""

    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        self.user = _make_user()
        self.tenant = _make_tenant(self.user)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    @patch.object(ChatMessageSendHourThrottle, "rate", "2/hour")
    @patch("apps.router.pending_queue.httpx.post")
    def test_send_throttled_after_limit(self, mock_post):
        mock_post.return_value = _ok_chat_response("ok")
        # Two sends allowed at 2/hour; the third is rejected with 429.
        for i in range(2):
            r = self.client.post(
                "/api/v1/chat/messages/",
                {"text": f"hi {i}", "client_msg_id": f"thr{i}"},
                format="json",
            )
            self.assertEqual(r.status_code, 201, r.content)
        blocked = self.client.post(
            "/api/v1/chat/messages/",
            {"text": "hi 3", "client_msg_id": "thr3"},
            format="json",
        )
        self.assertEqual(blocked.status_code, 429, blocked.content)
        # The blocked send never created a turn.
        self.assertFalse(AppChatMessage.objects.filter(client_msg_id="thr3").exists())

    @patch.object(ChatMessageSendHourThrottle, "rate", "2/hour")
    def test_poll_get_never_throttled(self):
        # The ?since= feed shares the view but must NOT be throttled — well past
        # the send limit, every poll still returns 200.
        for _ in range(5):
            r = self.client.get("/api/v1/chat/messages/")
            self.assertEqual(r.status_code, 200, r.content)
