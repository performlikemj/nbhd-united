"""Tier-0 status snapshot + Tier-2 "fast Siri responder".

See ``HER_SIRI_ARCHITECTURE.md`` (nbhd-ios) §2/§4. These JWT-authed control-plane
endpoints answer a Siri / App-Intents ask WITHOUT waking the per-tenant OpenClaw
container:

* ``SiriQuickStatusView`` (GET ``/api/v1/siri/status/``) — a deterministic,
  no-LLM "right now" snapshot (goals, tasks, fuel, finance, recent journal,
  conversation digest), assembled from the same source the on-device tier reads
  and PII-rehydrated for a user-facing spoken summary. Tier 0.

* ``SiriRespondView`` (POST ``/api/v1/siri/respond/``) — a free-form ask
  answered by a small **fast model** over that snapshot in a few seconds, using
  the platform (zero-data-retention) OpenRouter key. **Tier 2 is the
  classifier:** if the model can't answer from the snapshot — it needs the full
  agent's tools / memory / reasoning — it emits an escalate sentinel and we route
  the ask to the full tenant agent (Tier 3) async via the shared
  ``enqueue_tenant_turn`` chokepoint, returning a ``client_msg_id`` the client
  polls. This is the ONLY place the tenant container is woken.

Neither endpoint calls the container for the fast path; no per-tenant model
budget is consumed answering from the snapshot (only an escalation does, and that
goes through the budget-gated enqueue path).
"""

from __future__ import annotations

import logging
import re
import uuid

from django.conf import settings
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.router.chat_views import (
    _get_or_create_main_thread,
    _no_store,
    _serialize_message,
    enqueue_tenant_turn,
)
from apps.tenants.throttling import SiriRespondMinuteThrottle, SiriStatusMinuteThrottle

logger = logging.getLogger(__name__)

# Bound on a spoken Siri ask. Voice utterances are short; a longer payload is a
# misuse (or a paste) and shouldn't reach the model.
_MAX_ASK_CHARS = 1000

# Context budget for the fast model. The snapshot is a compact digest; this keeps
# the prompt (and latency) tight for a sub-5s spoken turn.
_SIRI_CONTEXT_CHARS = 4000

# The fast model returns EXACTLY this sentinel when it cannot answer from the
# snapshot and the ask needs the full agent. A sentinel (not brittle JSON
# parsing) keeps the contract robust across small models.
_ESCALATE_SENTINEL = "[[ESCALATE]]"

# Ordered fast-model candidates for the Tier-2 responder. Defaults to the same
# low-latency "fast" slot used for scheduled/worker tasks (DeepSeek V4 Flash),
# with the reasoning model as a fallback. Overridable via settings — NOT a
# per-tenant field (reuse the model already configured for the fleet).
_DEFAULT_SIRI_FAST_MODELS = [
    "openrouter/deepseek/deepseek-v4-flash",
    "openrouter/deepseek/deepseek-v4-pro",
]

_SIRI_SYSTEM = (
    "You are NBHD, the user's personal assistant, answering through Siri by voice. "
    "Answer the user's question using ONLY the context below, concisely and warmly, "
    "in 1-2 spoken sentences. Plain spoken text only: no markdown, no lists, no emoji, "
    "no headings. Never invent facts that are not in the context. "
    "If the question CANNOT be answered from the context — it needs tools, a search, "
    "fresh data, or deeper reasoning — reply with EXACTLY this and nothing else: "
    f"{_ESCALATE_SENTINEL}\n\n"
    "=== CONTEXT (the user's current state) ===\n"
)


def _siri_fast_models() -> list[str]:
    models = getattr(settings, "SIRI_FAST_MODELS", None) or _DEFAULT_SIRI_FAST_MODELS
    return [m for m in models if m]


def _rehydrated_snapshot(tenant, *, max_chars: int) -> str:
    """Compact current-state digest, PII-rehydrated to real values.

    Same source/shape as ``ChatContextView`` (the on-device tier) so Tier 0/1/2
    share one notion of "what's going on". Fail-open on rehydrate error: serving
    placeholder-space text is better than serving none.
    """
    from apps.orchestrator.workspace_envelope import (
        CONTEXT_DIGEST_MAX_CHARS,
        CONTEXT_DIGEST_MIN_CHARS,
        render_context_digest,
    )

    capped = max(CONTEXT_DIGEST_MIN_CHARS, min(max_chars, CONTEXT_DIGEST_MAX_CHARS))
    md = render_context_digest(tenant, max_chars=capped)

    entity_map = getattr(tenant, "pii_entity_map", None)
    if entity_map:
        try:
            from apps.pii.redactor import rehydrate_text

            md = rehydrate_text(md, entity_map)
        except Exception:
            logger.exception("siri: PII rehydrate failed (non-fatal)")
    return md


class SiriQuickStatusView(APIView):
    """GET: a deterministic, no-LLM 'right now' snapshot for a Siri status ask."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [SiriStatusMinuteThrottle]

    def get(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        from django.utils import timezone

        snapshot = _rehydrated_snapshot(tenant, max_chars=_SIRI_CONTEXT_CHARS)
        return _no_store(
            Response(
                {
                    "snapshot_md": snapshot,
                    "generated_at": timezone.now().isoformat(),
                }
            )
        )


class SiriRespondView(APIView):
    """POST: answer a free-form Siri ask with the fast model, or escalate.

    Body: ``{"intent": "<the spoken question>", "client_msg_id": "<optional>"}``.

    Returns one of:
    * ``{"answered": true, "text": "<spoken answer>", "model": "<id>"}`` — the
      fast model answered from the snapshot. Nothing was persisted; this is a
      read, like a status query.
    * ``{"answered": false, "escalated": true, "client_msg_id": "<id>",
      "turn": {...}}`` — routed to the full tenant agent (Tier 3). The client
      polls ``GET /api/v1/chat/messages/<client_msg_id>/`` for the reply (and may
      surface a budget error there if the tenant is over budget).
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [SiriRespondMinuteThrottle]

    def post(self, request):
        tenant = getattr(request.user, "tenant", None)
        if not tenant:
            return Response({"error": "no_tenant"}, status=status.HTTP_404_NOT_FOUND)
        if not isinstance(request.data, dict):
            return Response({"error": "invalid_body"}, status=status.HTTP_400_BAD_REQUEST)

        intent = str(request.data.get("intent") or "").strip()
        if not intent:
            return Response({"error": "empty_intent"}, status=status.HTTP_400_BAD_REQUEST)
        if len(intent) > _MAX_ASK_CHARS:
            return Response({"error": "intent_too_long"}, status=status.HTTP_400_BAD_REQUEST)

        snapshot = _rehydrated_snapshot(tenant, max_chars=_SIRI_CONTEXT_CHARS)

        answer = self._fast_answer(intent, snapshot)
        if answer is not None:
            return _no_store(Response({"answered": True, "escalated": False, "text": answer}))

        # Could not answer fast → escalate to the full tenant agent (Tier 3).
        return self._escalate(request, tenant, intent)

    def _fast_answer(self, intent: str, snapshot: str) -> str | None:
        """Return the spoken answer, or None to signal escalation.

        None on: the escalate sentinel, an empty/blank model reply, or any
        model/transport failure — every "can't answer fast" path falls through
        to the full agent rather than erroring the user.
        """
        from apps.common.openrouter import chat_completion

        messages = [
            {"role": "system", "content": _SIRI_SYSTEM + snapshot},
            {"role": "user", "content": intent},
        ]
        try:
            data, _model = chat_completion(_siri_fast_models(), messages, timeout=8, max_tokens=300)
        except Exception:
            logger.warning("siri respond: fast model failed; escalating", exc_info=True)
            return None

        try:
            reply = (data["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError):
            return None

        if not reply:
            return None
        # Sentinel may arrive alone or with a trailing reason; treat any leading
        # sentinel as escalation. Case-insensitive so a mixed-case `[[escalate]]`
        # still escalates.
        if reply.upper().startswith(_ESCALATE_SENTINEL):
            return None
        # Defensive: a model that ignored the "plain text" instruction shouldn't
        # leak a stray (mixed-case) sentinel mid-reply into the spoken answer.
        cleaned = re.sub(re.escape(_ESCALATE_SENTINEL), "", reply, flags=re.IGNORECASE).strip()
        return cleaned or None

    def _escalate(self, request, tenant, intent: str) -> Response:
        client_msg_id = str(request.data.get("client_msg_id") or "").strip() or uuid.uuid4().hex
        if len(client_msg_id) > 64:
            return Response({"error": "invalid_client_msg_id"}, status=status.HTTP_400_BAD_REQUEST)
        thread = _get_or_create_main_thread(tenant, request.user)
        turn, _created = enqueue_tenant_turn(
            tenant=tenant,
            user=request.user,
            text=intent,
            thread=thread,
            client_msg_id=client_msg_id,
        )
        return _no_store(
            Response(
                {
                    "answered": False,
                    "escalated": True,
                    "client_msg_id": turn.client_msg_id,
                    "turn": _serialize_message(turn),
                }
            )
        )
