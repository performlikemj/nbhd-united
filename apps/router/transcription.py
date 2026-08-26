"""OpenRouter ZDR speech-to-text and its optional non-PII vocabulary.

Speech recognizers transcribe a short clip with no knowledge of the speaker's
world, so distinctive proper nouns — brands, project names, people — come back
*phonetically approximated*. That is how the July 2026 incident happened: MJ's
voice note about a **Rakuten** (楽天) meeting came back as "Rocketen", was
written verbatim into his journal, carried forward by the daily summary, and
frozen into ``Tenant.pii_entity_map`` when memory_sync's redaction pass ran NER
over the note (an unknown proper noun mislabels as PERSON; COMPANY_NAME is
deliberately not detected).

Channel attribution matters here: the incident clip arrived via the **iOS app**,
whose voice input is transcribed ON DEVICE by Apple's speech recognizer — the
text reaches Django already garbled (``apps/router/chat_views.py`` chat ingress
accepts text only; there is no server-side audio path for iOS). The fix for that
channel is the iOS app feeding this same vocabulary into
``SFSpeechRecognitionRequest.contextualStrings`` — it fetches the terms from
``GET /api/v1/chat/transcription-vocab/`` (``TranscriptionVocabView``), which is
why the term collection lives here. Server-side Telegram and LINE audio use
the shared :func:`transcribe_audio` OpenRouter path. OpenRouter currently
accepts but ignores the generic STT ``prompt`` field, so server requests send
no prompt rather than leaking identity fields for no transcription benefit.

PII boundary
------------
For Whisper the audio already goes to OpenAI, so the hint adds no new *audio*
egress; for iOS the terms stay on the user's own device. Either way we
deliberately draw ONLY from sources the user or the PII arbiter have already
declared non-identifying:

* ``pii_denylist`` keys — brands / projects / jargon explicitly marked
  "not PII for me" (this is exactly where "rakuten" lands once denylisted).

Contact names living in ``pii_entity_map`` are intentionally excluded: those
are third-party PERSON entities the PII module works to keep out of provider
prompts, and the transcription win does not justify widening their egress.
"""

from __future__ import annotations

import base64
import logging
from typing import TYPE_CHECKING

import requests
from django.conf import settings
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.openrouter import OPENROUTER_TRANSCRIPTIONS_URL, build_openrouter_body
from apps.integrations.internal_auth import InternalAuthError, validate_internal_runtime_request

if TYPE_CHECKING:
    from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

# Whisper caps the prompt at ~224 tokens; iOS contextualStrings likewise wants
# a short list. Stay well under both with coarse budgets.
_MAX_TERMS = 48
_MAX_PROMPT_CHARS = 600
_MAX_AUDIO_BYTES = 25 * 1024 * 1024


def transcribe_audio(
    audio_data: bytes,
    *,
    audio_format: str,
    tenant: Tenant | None = None,
    timeout: int = 60,
) -> str:
    """Transcribe audio through OpenRouter with mandatory ZDR routing.

    The JSON/base64 request shape works for Telegram, LINE, and the internal
    container shim. There is deliberately no direct-provider fallback.
    """
    del tenant  # Reserved for safe vocabulary routing if OpenRouter supports it.
    if not audio_data:
        raise ValueError("audio data is empty")
    if len(audio_data) > _MAX_AUDIO_BYTES:
        raise ValueError("audio data exceeds the 25 MB limit")

    key = str(getattr(settings, "OPENROUTER_API_KEY", "") or "").strip()
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY not configured")

    model = str(
        getattr(settings, "OPENROUTER_STT_MODEL", "openai/whisper-large-v3-turbo") or "openai/whisper-large-v3-turbo"
    ).strip()
    normalized_format = _normalize_audio_format(audio_format)
    body = build_openrouter_body(
        model,
        input_audio={
            "data": base64.b64encode(audio_data).decode("ascii"),
            "format": normalized_format,
        },
    )
    response = requests.post(
        OPENROUTER_TRANSCRIPTIONS_URL,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    text = payload.get("text") if isinstance(payload, dict) else None
    if not isinstance(text, str) or not text.strip():
        raise ValueError("OpenRouter transcription response is missing text")
    return text.strip()


def _normalize_audio_format(value: str) -> str:
    normalized = str(value or "").strip().lower().lstrip(".")
    aliases = {"mpeg": "mp3", "mp4": "m4a", "x-m4a": "m4a", "oga": "ogg"}
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"wav", "mp3", "flac", "m4a", "ogg", "webm", "aac"}:
        raise ValueError("unsupported audio format")
    return normalized


def collect_transcription_vocab(tenant: Tenant | None) -> list[str]:
    """Ordered, de-duplicated transcription vocabulary for a tenant.

    The single source of truth for *which terms* bias speech-to-text, shared by
    the server-side Whisper prompt (Telegram/LINE) and the iOS vocabulary
    endpoint (``GET /api/v1/chat/transcription-vocab/`` →
    ``SFSpeechRecognitionRequest.contextualStrings``).

    Only arbiter/user-vetted denylist brands and jargon are included. Identity
    fields and workspace names are excluded. The result is capped by term count
    and total length. Never raises: any lookup failure degrades to ``[]`` so
    transcription proceeds exactly as it does without a hint.
    """
    if tenant is None:
        return []
    try:
        return _collect_vocabulary(tenant)
    except Exception:
        logger.debug("collect_transcription_vocab: vocabulary lookup failed", exc_info=True)
        return []


def build_transcription_prompt(tenant: Tenant | None) -> str | None:
    """Return a Whisper ``prompt`` biasing decoding toward the tenant's known
    non-PII proper nouns, or ``None`` when there is no useful vocabulary.

    Used by the two server-side Whisper call sites (Telegram poller, LINE
    webhook). Never raises — see ``collect_transcription_vocab``.
    """
    terms = collect_transcription_vocab(tenant)
    if not terms:
        return None
    # A short natural-language frame reads better to Whisper than a bare list
    # and keeps the terms in "these words may appear" decoder context.
    return "The speaker may mention: " + ", ".join(terms) + "."


def _collect_vocabulary(tenant: Tenant) -> list[str]:
    """Assemble the raw term list (see ``collect_transcription_vocab``)."""
    seen: set[str] = set()
    terms: list[str] = []

    def _add(raw: object) -> None:
        if not isinstance(raw, str):
            return
        term = raw.strip()
        # Single characters carry no spelling signal.
        if len(term) < 2:
            return
        key = term.casefold()
        if key in seen:
            return
        seen.add(key)
        terms.append(term)

    # 1. Denylist keys: the user/arbiter-vetted "not PII" vocabulary. Keys are
    #    canonical (casefolded); title-case bare single tokens so the recognizer
    #    is biased toward the natural proper-noun spelling ("rakuten" -> "Rakuten").
    denylist = getattr(tenant, "pii_denylist", None) or {}
    if isinstance(denylist, dict):
        for key in denylist:
            _add(_titleish(key))

    # Enforce the budget.
    out: list[str] = []
    total = 0
    for term in terms:
        if len(out) >= _MAX_TERMS:
            break
        total += len(term) + 2
        if total > _MAX_PROMPT_CHARS:
            break
        out.append(term)
    return out


def _titleish(key: str) -> str:
    """Title-case a bare lowercase token so it reads as a proper noun; leave
    multi-word or already-cased strings untouched.
    """
    if key and key.islower() and " " not in key:
        return key[:1].upper() + key[1:]
    return key


class InternalTranscriptionView(APIView):
    """Per-tenant authenticated transcription seam for runtime containers."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        tenant_id = str(request.headers.get("X-NBHD-Tenant-Id", "") or "").strip()
        try:
            validate_internal_runtime_request(
                provided_key=request.headers.get("X-NBHD-Internal-Key", ""),
                provided_tenant_id=tenant_id,
            )
        except InternalAuthError as exc:
            return Response(
                {"error": "internal_auth_failed", "detail": str(exc)},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        from apps.tenants.models import Tenant

        try:
            tenant = Tenant.objects.get(id=tenant_id)
        except Tenant.DoesNotExist:
            return Response({"error": "tenant_not_found"}, status=status.HTTP_404_NOT_FOUND)

        try:
            audio_data, audio_format = _audio_from_internal_request(request)
            text = transcribe_audio(audio_data, audio_format=audio_format, tenant=tenant)
        except ValueError as exc:
            logger.info(
                "internal_transcription_rejected tenant=%s error_type=%s",
                tenant_id,
                type(exc).__name__,
            )
            return Response(
                {"error": "invalid_audio", "detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception as exc:
            logger.warning(
                "internal_transcription_failed tenant=%s error_type=%s",
                tenant_id,
                type(exc).__name__,
            )
            return Response(
                {"error": "transcription_failed", "detail": "Couldn't transcribe this voice note."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        logger.info(
            "internal_transcription_succeeded tenant=%s audio_bytes=%d transcript_chars=%d",
            tenant_id,
            len(audio_data),
            len(text),
        )
        return Response({"text": text}, status=status.HTTP_200_OK)


def _audio_from_internal_request(request) -> tuple[bytes, str]:
    uploaded = request.FILES.get("file")
    if uploaded is not None:
        audio_data = uploaded.read(_MAX_AUDIO_BYTES + 1)
        suffix = str(getattr(uploaded, "name", "")).rsplit(".", 1)
        audio_format = suffix[-1] if len(suffix) == 2 else str(getattr(uploaded, "content_type", "")).split("/")[-1]
        return audio_data, _normalize_audio_format(audio_format)

    input_audio = request.data.get("input_audio") if hasattr(request.data, "get") else None
    if not isinstance(input_audio, dict):
        raise ValueError("file or input_audio is required")
    encoded = input_audio.get("data")
    if not isinstance(encoded, str) or not encoded:
        raise ValueError("input_audio.data is required")
    try:
        audio_data = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("input_audio.data must be valid base64") from exc
    return audio_data, _normalize_audio_format(str(input_audio.get("format", "")))
