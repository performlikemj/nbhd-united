"""Per-message PII transparency metadata for the iOS chat channel.

The platform obfuscates the user's real values behind ``[TYPE_N]`` placeholders
before a turn reaches the assistant, then rehydrates them on the way back. These
tests cover the metadata that lets the app SHOW the user which of their values
were obfuscated for a given turn:

  - the user turn records ``user_redactions`` (placeholders in the LLM-bound
    text, resolved to the real value each stands in for)
  - the assistant reply records ``reply_redactions`` (placeholders the assistant
    emitted, captured BEFORE they are rehydrated away), on the SAME
    representative row that receives ``reply_text`` — coalesced siblings stay
    null
  - both fields surface through the poll serializer AND the ``?since=`` feed as
    optional keys (older builds ignore them)
  - an on-device turn (never redacted) leaves both fields null

Redaction here is driven purely by the known-entity regex pass (Step 1 of
``_redact_user_message``), which is independent of the DeBERTa NER model — we
seed ``pii_entity_map`` with a known name and stub ``_detect_pii`` to return no
hits so the tests are deterministic and never load the ONNX model.
"""

from __future__ import annotations

import secrets
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.router.models import AppChatMessage, ChatThread, PendingMessage
from apps.router.pending_queue import drain_pending_messages_for_tenant_task
from apps.tenants.models import Tenant, User

# A made-up name seeded into the entity map so the known-entity regex pass
# redacts it without any NER model. [PERSON_5] mirrors the real in-prod shape.
_KNOWN_NAME = "Sautai"
_KNOWN_PLACEHOLDER = "[PERSON_5]"


def _make_user() -> User:
    return User.objects.create_user(
        username=f"ios_{secrets.token_hex(4)}",
        email=f"{secrets.token_hex(4)}@example.com",
        preferred_channel="telegram",
    )


def _make_tenant(user: User) -> Tenant:
    tenant = Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE,
        container_fqdn="oc-ios.example.com",
    )
    tenant.pii_entity_map = {_KNOWN_PLACEHOLDER: _KNOWN_NAME}
    tenant.save(update_fields=["pii_entity_map"])
    return tenant


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


@override_settings(NBHD_INTERNAL_API_KEY="test-key", NBHD_DISABLE_BACKGROUND_THREADS=True)
class ChatRedactionMetadataTest(TestCase):
    def setUp(self):
        self.user = _make_user()
        self.tenant = _make_tenant(self.user)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    # -- (a) user side ----------------------------------------------------

    @patch("apps.pii.redactor._detect_pii", return_value=[])
    @patch("apps.router.pending_queue.httpx.post")
    def test_user_turn_with_known_name_stores_user_redactions(self, mock_post, _mock_ner):
        mock_post.return_value = _ok_chat_response("noted")

        resp = self.client.post(
            "/api/v1/chat/messages/",
            {"text": "met Sautai for coffee", "client_msg_id": "u1"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        expected = [{"placeholder": _KNOWN_PLACEHOLDER, "value": _KNOWN_NAME}]
        # The POST response serializes the (in-memory) turn: user_redactions is
        # already set, reply hasn't landed on the in-memory object yet.
        self.assertEqual(resp.data["user_redactions"], expected)

        # The LLM-bound text was redacted, but the stored user_text stays verbatim.
        row = AppChatMessage.objects.get(tenant=self.tenant, client_msg_id="u1")
        self.assertEqual(row.user_redactions, expected)
        self.assertEqual(row.user_text, "met Sautai for coffee")
        # What actually went to the container carried the placeholder, not the name.
        content = mock_post.call_args.kwargs["json"]["messages"][0]["content"]
        self.assertIn(_KNOWN_PLACEHOLDER, content)
        self.assertNotIn(_KNOWN_NAME, content)

    @patch("apps.pii.redactor._detect_pii", return_value=[])
    @patch("apps.router.pending_queue.httpx.post")
    def test_user_turn_without_pii_leaves_user_redactions_null(self, mock_post, _mock_ner):
        mock_post.return_value = _ok_chat_response("noted")

        resp = self.client.post(
            "/api/v1/chat/messages/",
            {"text": "what's the weather like", "client_msg_id": "u0"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertIsNone(resp.data["user_redactions"])
        row = AppChatMessage.objects.get(tenant=self.tenant, client_msg_id="u0")
        self.assertIsNone(row.user_redactions)

    # -- (b) reply side ---------------------------------------------------

    @patch("apps.pii.redactor._detect_pii", return_value=[])
    @patch("apps.router.pending_queue.httpx.post")
    def test_reply_with_placeholder_rehydrates_and_stores_reply_redactions(self, mock_post, _mock_ner):
        # The container replies in placeholder space; the drain rehydrates it.
        mock_post.return_value = _ok_chat_response("Tell [PERSON_5] I said hi.")

        resp = self.client.post(
            "/api/v1/chat/messages/",
            {"text": "message Sautai", "client_msg_id": "r1"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)

        # Poll: the stored reply is rehydrated to the real name and the
        # transparency metadata records the placeholder it stood behind.
        poll = self.client.get("/api/v1/chat/messages/r1/")
        self.assertEqual(poll.status_code, 200, poll.content)
        self.assertEqual(poll.data["status"], "ready")
        self.assertEqual(poll.data["reply_text"], "Tell Sautai I said hi.")
        self.assertEqual(
            poll.data["reply_redactions"],
            [{"placeholder": _KNOWN_PLACEHOLDER, "value": _KNOWN_NAME}],
        )

    @patch("apps.router.pending_queue.httpx.post")
    def test_reply_with_unbound_placeholder_records_null_value(self, mock_post):
        # A placeholder with no binding (e.g. a stale delete) passes through
        # rehydration verbatim and is recorded with a null value.
        mock_post.return_value = _ok_chat_response("Ask [PERSON_99] about it.")
        thread = ChatThread.objects.create(tenant=self.tenant, user=self.user, is_main=True)
        self._make_pending("orphan", thread, user_text="hi")

        drain_pending_messages_for_tenant_task(str(self.tenant.id), "ios", str(thread.id))

        row = AppChatMessage.objects.get(tenant=self.tenant, client_msg_id="orphan")
        self.assertEqual(row.status, AppChatMessage.Status.READY)
        # Unknown placeholder is left verbatim in the reply text.
        self.assertEqual(row.reply_text, "Ask [PERSON_99] about it.")
        self.assertEqual(row.reply_redactions, [{"placeholder": "[PERSON_99]", "value": None}])

    # -- (b, coalesced) representative-row-only ---------------------------

    def _make_pending(self, client_msg_id: str, thread: ChatThread, *, user_text: str) -> AppChatMessage:
        """A PENDING AppChatMessage + its PendingMessage queue row for the iOS
        channel, keyed by the thread (the coalesce key), so a direct drain call
        folds sibling rows into one batch."""
        turn = AppChatMessage.objects.create(
            tenant=self.tenant,
            user=self.user,
            thread=thread,
            client_msg_id=client_msg_id,
            user_text=user_text,
            status=AppChatMessage.Status.PENDING,
        )
        PendingMessage.objects.create(
            tenant=self.tenant,
            channel=PendingMessage.Channel.IOS,
            channel_user_id=str(thread.id),
            payload={
                "message_text": user_text,
                "user_param": f"thread:{thread.id}",
                "user_timezone": "UTC",
                "client_msg_id": client_msg_id,
                "thread_id": str(thread.id),
            },
            user_text=user_text,
        )
        return turn

    @patch("apps.router.pending_queue.httpx.post")
    def test_coalesced_reply_redactions_only_on_representative_row(self, mock_post):
        mock_post.return_value = _ok_chat_response("Ping [PERSON_5] for both.")
        thread = ChatThread.objects.create(tenant=self.tenant, user=self.user, is_main=True)
        # Two rows for the same thread → coalesce into one batch; the combined
        # reply lands on the LAST client id (the representative row).
        self._make_pending("c1", thread, user_text="first")
        self._make_pending("c2", thread, user_text="second")

        result = drain_pending_messages_for_tenant_task(str(self.tenant.id), "ios", str(thread.id))
        self.assertEqual(result["batch_size"], 2)
        self.assertEqual(mock_post.call_count, 1)

        rep = AppChatMessage.objects.get(tenant=self.tenant, client_msg_id="c2")
        sib = AppChatMessage.objects.get(tenant=self.tenant, client_msg_id="c1")
        # Representative row: rehydrated reply + its redaction metadata.
        self.assertEqual(rep.reply_text, "Ping Sautai for both.")
        self.assertEqual(
            rep.reply_redactions,
            [{"placeholder": _KNOWN_PLACEHOLDER, "value": _KNOWN_NAME}],
        )
        # Sibling row: terminal READY, empty reply, and null metadata (the reply
        # is attached exactly once, not fanned out).
        self.assertEqual(sib.status, AppChatMessage.Status.READY)
        self.assertEqual(sib.reply_text, "")
        self.assertIsNone(sib.reply_redactions)

    # -- (c) serving surfaces --------------------------------------------

    @patch("apps.pii.redactor._detect_pii", return_value=[])
    @patch("apps.router.pending_queue.httpx.post")
    def test_serializer_and_since_feed_expose_fields(self, mock_post, _mock_ner):
        mock_post.return_value = _ok_chat_response("Hi [PERSON_5]!")

        resp = self.client.post(
            "/api/v1/chat/messages/",
            {"text": "greet Sautai", "client_msg_id": "s1"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        expected = [{"placeholder": _KNOWN_PLACEHOLDER, "value": _KNOWN_NAME}]

        # Poll serializer exposes both halves of the turn.
        poll = self.client.get("/api/v1/chat/messages/s1/")
        self.assertEqual(poll.data["user_redactions"], expected)
        self.assertEqual(poll.data["reply_redactions"], expected)

        # ?since= feed: role-split rows carry the matching optional key.
        feed = self.client.get("/api/v1/chat/messages/")
        self.assertEqual(feed.status_code, 200, feed.content)
        rows = feed.data["messages"]
        user_rows = [r for r in rows if r["role"] == "user" and r.get("client_msg_id") == "s1"]
        asst_rows = [r for r in rows if r["role"] == "assistant" and r.get("client_msg_id") == "s1"]
        self.assertEqual(len(user_rows), 1)
        self.assertEqual(len(asst_rows), 1)
        self.assertEqual(user_rows[0]["user_redactions"], expected)
        self.assertNotIn("reply_redactions", user_rows[0])
        self.assertEqual(asst_rows[0]["reply_redactions"], expected)
        self.assertNotIn("user_redactions", asst_rows[0])

    # -- (d) on-device turns are never redacted ---------------------------

    def test_on_device_turn_leaves_both_fields_null(self):
        resp = self.client.post(
            "/api/v1/chat/turns/",
            {
                "text": "I talked to Sautai locally",
                "reply_text": "That's nice.",
                "client_msg_id": "d1",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertIsNone(resp.data["user_redactions"])
        self.assertIsNone(resp.data["reply_redactions"])

        row = AppChatMessage.objects.get(tenant=self.tenant, client_msg_id="d1")
        self.assertEqual(row.source, AppChatMessage.Source.ON_DEVICE)
        self.assertIsNone(row.user_redactions)
        self.assertIsNone(row.reply_redactions)
