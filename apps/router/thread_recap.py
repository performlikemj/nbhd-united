"""Cold-session conversation recap for iOS chat threads.

Why this exists
---------------

Each iOS chat thread runs as its own OpenClaw session (``user="thread:<id>"``)
inside the tenant's container. Session transcripts live on an EmptyDir
(``sessions-scratch``) that is **deliberately ephemeral** — it is never moved
to the tenant file share (a hard invariant: SMB durability semantics corrupt
live-written state). So when the container hibernates (≥2h idle) or restarts,
the OpenClaw session transcript for the thread is wiped.

Nothing rehydrated it. The user came back two days later, said "just wanted to
jump back into this fable 5 usage" in the SAME thread, and the assistant
answered "What's fable 5? Not ringing a bell." — because its in-session memory
of the thread was gone, even though the full history sat in Postgres
(``app_chat_messages``) the whole time.

This module is the deterministic fix. On a probably-wiped session it builds a
bounded ``[conversation-recap]`` marker block from the thread's own recent
``AppChatMessage`` rows and prepends it to the turn, so the agent picks the
conversation back up instead of greeting the user cold.

Cold-session detection
----------------------

The OC session for a thread is wiped iff the container restarted after the
thread's last delivered turn. Django's authoritative restart signal is
``Tenant.last_wake_at`` — stamped by ``wake_hibernated_tenant`` every time a
hibernated container is woken. We inject the recap when::

    last_turn = max(replied_at) over the thread's READY rows (pre-this-batch)
    inject iff last_turn is not None
            and tenant.last_wake_at is not None
            and tenant.last_wake_at > last_turn

This is naturally exactly-once per wake: once the first post-wake turn is
delivered its ``replied_at`` moves ``last_turn`` past ``last_wake_at``, so the
next turn no longer qualifies. Drain retries recompute the identical condition
(the in-flight batch is still PENDING and excluded), so they are idempotent.

Deploy-time image bumps do NOT stamp ``last_wake_at`` (they are not
hibernation wakes), so a recap is not injected on the rare deploy restart in
v1 — an acceptable miss, noted here rather than hidden.

PII posture
-----------

The container operates in PII-placeholder space. ``reply_text`` is already
stored placeholder-space (pseudonymize-at-rest), so it is emitted verbatim.
``user_text`` is stored **verbatim (real values)** so the owner-facing
``?since=`` feed can show exactly what the user typed — so we re-run it through
the same ``redact_user_message`` seam the live turn uses before it enters the
recap, reproducing the redaction the model already saw for that turn. We never
rehydrate here — real names must not re-enter the agent turn.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from django.db.models import Max

from apps.router.models import AppChatMessage

logger = logging.getLogger(__name__)

# The last N user↔assistant exchanges of the thread to replay. Enough to
# re-anchor a substantive conversation without ballooning the first post-wake
# turn.
RECAP_MAX_EXCHANGES = 8

# Per-side soft cap (user text and assistant text each). Trimmed on a word
# boundary with an ellipsis so a long turn contributes a readable gist, not a
# wall of text.
RECAP_SIDE_CHAR_CAP = 280

# Hard cap on the whole assembled block. If the trimmed exchanges still exceed
# this, the OLDEST exchanges are dropped until it fits (recency wins). Bounds
# the added prompt to ~3 KB on the first turn after a wake only.
RECAP_TOTAL_CHAR_CAP = 2800

_RECAP_INSTRUCTION = (
    "[conversation-recap: your runtime restarted since this thread's last "
    "message, so your in-session memory of this conversation was wiped. The "
    "exchange below is the most recent history of THIS SAME thread, oldest "
    "first. Continue from it — do not greet the user as if this is a new "
    "conversation, and do not re-ask anything these turns already settled.]"
)


def _truncate_on_word(text: str, limit: int) -> str:
    """Collapse whitespace and trim ``text`` to ``limit`` chars, preferring a
    word boundary but hard-cutting when the nearest boundary is too early (a
    long unbroken run — URL, base64 — would otherwise collapse to almost
    nothing). Appends an ellipsis when trimmed. Placeholder tokens like
    ``[PERSON_1]`` carry no internal space, so a word-boundary break never
    splits one apart."""
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    cut = collapsed[:limit]
    space = cut.rfind(" ")
    # Only honour the boundary when it retains most of the budget; otherwise
    # keep the hard cut at ``limit`` so a space-less blob still fills the cap.
    if space >= int(limit * 0.6):
        cut = cut[:space]
    return cut.rstrip() + "…"


def _render_exchange(user_text: str, reply_text: str) -> str:
    """One exchange as two labelled lines ('you' == the assistant/reader)."""
    return f"- user: {user_text}\n  you: {reply_text}"


def build_thread_recap_block(
    tenant,
    thread_id: str,
    *,
    exclude_client_msg_ids: Iterable[str] = (),
) -> str:
    """Return a ``[conversation-recap]`` block for a probably-wiped iOS session,
    or ``""`` when no recap is warranted.

    ``thread_id`` is the ChatThread id (iOS ``channel_user_id``).
    ``exclude_client_msg_ids`` are the current drain batch's rows, excluded so
    the block never replays the very turn being delivered.

    Self-protecting: any failure returns ``""`` — a missing recap must never
    break message delivery.
    """
    try:
        last_wake_at = getattr(tenant, "last_wake_at", None)
        # No recorded wake → the container never came back from hibernation
        # since we started tracking, so the session is warm. Nothing to do.
        if last_wake_at is None:
            return ""

        exclude_ids = [cid for cid in exclude_client_msg_ids if cid]
        base_qs = AppChatMessage.objects.filter(
            tenant=tenant,
            thread_id=thread_id,
            status=AppChatMessage.Status.READY,
        )
        if exclude_ids:
            base_qs = base_qs.exclude(client_msg_id__in=exclude_ids)

        # last_turn = the thread's most recent delivered turn (any READY row,
        # including empty-reply coalesce siblings — they still carry replied_at).
        last_turn = base_qs.aggregate(m=Max("replied_at"))["m"]
        if last_turn is None:
            # First-ever turn in this thread (no prior delivered history).
            return ""
        # Warm session: the last turn happened at/after the last wake, so the
        # transcript survived. No recap.
        if last_wake_at <= last_turn:
            return ""

        # Cold session. Pull the last N exchanges with actual assistant text
        # (empty reply_text = a coalesce sibling; the representative row of the
        # same turn carries the combined reply), oldest → newest.
        #
        # ENCRYPTION-AT-REST Phase 2 DEBT (CONTINUITY_encryption-phase1.md §1):
        # AppChatMessage.reply_text is registered for AES-GCM encryption. The
        # predicate below is correct TODAY (the column is plaintext; "" is the
        # real stored value for coalesce siblings) — hence the sanctioned noqa.
        # When the column flips, this whole module must move to the apps.crypto
        # read path: BOTH this ``exclude(reply_text="")`` (ciphertext never
        # equals "" — the filter would silently stop matching) AND the
        # ``.values("user_text", "reply_text")`` fetch below it (which would
        # otherwise emit raw ciphertext into the recap block). The Phase 2
        # sweep must not skip this file.
        rows = list(
            base_qs.exclude(reply_text="")  # noqa: encrypted-predicate
            .order_by("-created_at", "-replied_at")
            .values("user_text", "reply_text")[:RECAP_MAX_EXCHANGES]
        )
        rows.reverse()
        if not rows:
            return ""

        from apps.pii.redactor import redact_user_message

        exchanges: list[str] = []
        for row in rows:
            # reply_text is already placeholder-space (pseudonymize-at-rest);
            # user_text is verbatim → redact it to placeholder-space so the
            # recap leaks no real PII the live turn would have hidden.
            user_txt = _truncate_on_word(
                redact_user_message(row["user_text"] or "", tenant),
                RECAP_SIDE_CHAR_CAP,
            )
            reply_txt = _truncate_on_word(row["reply_text"] or "", RECAP_SIDE_CHAR_CAP)
            if not (user_txt or reply_txt):
                continue
            exchanges.append(_render_exchange(user_txt, reply_txt))

        if not exchanges:
            return ""

        def _assemble(items: list[str]) -> str:
            return _RECAP_INSTRUCTION + "\n" + "\n".join(items) + "\n"

        # Enforce the total cap by dropping the oldest exchanges first.
        while len(exchanges) > 1 and len(_assemble(exchanges)) > RECAP_TOTAL_CHAR_CAP:
            exchanges.pop(0)

        return _assemble(exchanges)
    except Exception:
        logger.exception("thread_recap: failed to build recap block (non-fatal)")
        return ""
