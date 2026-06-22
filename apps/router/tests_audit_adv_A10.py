"""Adversarial-audit cluster A10 regression tests.

router-chat#1 — iOS coalesced-batch reply fan-out.

When several iOS messages land on one thread before the first reply (the
cold-start burst), ``_claim_pending_batch_for_key`` folds the contiguous
PENDING rows into ONE coalesced batch and ``_drain_ios_batch`` gets ONE
combined reply. The bug: ``_store_ios_turn_reply`` wrote that single reply
onto EVERY ``client_msg_id`` in the batch, so the ?since= feed, the thread
history, and the USER.md digest each emitted the SAME assistant text N times
(3 quick messages → the assistant bubble repeated 3×).

The fix attaches the combined reply to a single representative row (the last
in the batch) and flips the rest to a terminal READY state with empty
``reply_text`` so the polling client stops waiting but no duplicate assistant
row is rendered.
"""

from __future__ import annotations

import secrets

from django.test import TestCase

from apps.router.chat_history import build_since_page
from apps.router.conversation_capture import build_conversation_digest
from apps.router.models import AppChatMessage, ChatThread, PendingMessage
from apps.router.pending_queue import _store_ios_turn_reply
from apps.tenants.models import Tenant, User


def _make_tenant() -> Tenant:
    user = User.objects.create_user(
        username=f"a10_{secrets.token_hex(4)}",
        email=f"{secrets.token_hex(4)}@example.com",
        preferred_channel="telegram",
    )
    return Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE,
        container_fqdn="oc-a10.example.com",
    )


def _make_batch(tenant: Tenant, thread: ChatThread, client_ids: list[str]) -> list[PendingMessage]:
    """Mirror the coalesced-batch shape: one PENDING AppChatMessage + one
    PendingMessage per client_msg_id, all on the same ios thread."""
    batch: list[PendingMessage] = []
    for cid in client_ids:
        AppChatMessage.objects.create(
            tenant=tenant,
            user=tenant.user,
            thread=thread,
            client_msg_id=cid,
            user_text=f"msg {cid}",
            status=AppChatMessage.Status.PENDING,
        )
        batch.append(
            PendingMessage.objects.create(
                tenant=tenant,
                channel=PendingMessage.Channel.IOS,
                channel_user_id=str(thread.id),
                payload={"client_msg_id": cid, "user_param": f"thread:{thread.id}"},
                user_text=f"msg {cid}",
            )
        )
    return batch


def _assistant_rows(tenant: Tenant, thread: ChatThread) -> list[dict]:
    messages, _ = build_since_page(tenant, str(thread.id), cursor=None, limit=100)
    return [m for m in messages if m["role"] == "assistant"]


class IOSCoalescedReplyFanoutTest(TestCase):
    def setUp(self):
        self.tenant = _make_tenant()
        self.thread = ChatThread.objects.create(tenant=self.tenant, user=self.tenant.user, is_main=True)

    def test_coalesced_batch_emits_one_assistant_row(self):
        batch = _make_batch(self.tenant, self.thread, ["c1", "c2", "c3"])

        _store_ios_turn_reply(self.tenant, batch, "Here is the one combined reply.")

        # Exactly one assistant row in the ?since= feed (not three).
        assistant = _assistant_rows(self.tenant, self.thread)
        self.assertEqual(len(assistant), 1, assistant)
        self.assertEqual(assistant[0]["text"], "Here is the one combined reply.")

        # The reply lands on the representative (last) row; the others are
        # terminal READY with empty reply_text so the client stops polling.
        rep = AppChatMessage.objects.get(tenant=self.tenant, client_msg_id="c3")
        self.assertEqual(rep.status, AppChatMessage.Status.READY)
        self.assertEqual(rep.reply_text, "Here is the one combined reply.")
        for cid in ("c1", "c2"):
            other = AppChatMessage.objects.get(tenant=self.tenant, client_msg_id=cid)
            self.assertEqual(other.status, AppChatMessage.Status.READY)
            self.assertEqual(other.reply_text, "")
            self.assertIsNotNone(other.replied_at)

        # All three user messages still show up — only the assistant reply is
        # de-duplicated, not the user bubbles.
        user_rows = [
            m
            for m in build_since_page(self.tenant, str(self.thread.id), cursor=None, limit=100)[0]
            if m["role"] == "user"
        ]
        self.assertEqual(len(user_rows), 3)

    def test_digest_does_not_duplicate_assistant_text(self):
        batch = _make_batch(self.tenant, self.thread, ["d1", "d2", "d3"])
        _store_ios_turn_reply(self.tenant, batch, "Single digest reply.")

        digest = build_conversation_digest(self.tenant)
        # The USER.md digest reads AppChatMessage per row; the reply must appear
        # once, not once per coalesced message.
        self.assertEqual(digest.count("Single digest reply."), 1, digest)

    def test_singleton_batch_unchanged(self):
        batch = _make_batch(self.tenant, self.thread, ["s1"])
        _store_ios_turn_reply(self.tenant, batch, "Solo reply.")

        assistant = _assistant_rows(self.tenant, self.thread)
        self.assertEqual(len(assistant), 1, assistant)
        self.assertEqual(assistant[0]["text"], "Solo reply.")

        row = AppChatMessage.objects.get(tenant=self.tenant, client_msg_id="s1")
        self.assertEqual(row.status, AppChatMessage.Status.READY)
        self.assertEqual(row.reply_text, "Solo reply.")
