"""Per-tenant vocabulary for speech-to-text, across all voice channels.

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
why the term collection lives in a shared helper here. The two SERVER-side
transcription sites — the Telegram poller and the LINE webhook, both OpenAI
``whisper-1`` — previously sent no vocabulary at all; they now pass
``build_transcription_prompt`` as the Whisper ``prompt`` (decoder bias, not an
instruction), hardening them against the same garble class.

PII boundary
------------
For Whisper the audio already goes to OpenAI, so the hint adds no new *audio*
egress; for iOS the terms stay on the user's own device. Either way we
deliberately draw ONLY from sources the user or the PII arbiter have already
declared non-identifying:

* ``pii_denylist`` keys — brands / projects / jargon explicitly marked
  "not PII for me" (this is exactly where "rakuten" lands once denylisted, so
  the same signal that stops the redaction confusion also fixes the spelling).
* workspace names — user-authored labels.
* the user's own display name.

Contact names living in ``pii_entity_map`` are intentionally excluded: those
are third-party PERSON entities the PII module works to keep out of provider
prompts, and the transcription win does not justify widening their egress.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

# Whisper caps the prompt at ~224 tokens; iOS contextualStrings likewise wants
# a short list. Stay well under both with coarse budgets.
_MAX_TERMS = 48
_MAX_PROMPT_CHARS = 600


def collect_transcription_vocab(tenant: Tenant | None) -> list[str]:
    """Ordered, de-duplicated transcription vocabulary for a tenant.

    The single source of truth for *which terms* bias speech-to-text, shared by
    the server-side Whisper prompt (Telegram/LINE) and the iOS vocabulary
    endpoint (``GET /api/v1/chat/transcription-vocab/`` →
    ``SFSpeechRecognitionRequest.contextualStrings``).

    Order: denylisted brands / jargon, then workspace names, then the user's
    own name — capped by term count and total length to respect the consumers'
    budgets. Never raises: any lookup failure degrades to ``[]`` so
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

    # 2. Workspace names — user-authored labels (e.g. project names).
    for name in _workspace_names(tenant):
        _add(name)

    # 3. The user's own display name.
    user = getattr(tenant, "user", None)
    _add(getattr(user, "display_name", "") if user is not None else "")

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


def _workspace_names(tenant: Tenant) -> list[str]:
    """Best-effort list of the tenant's workspace names (empty on any error)."""
    try:
        return [n for n in tenant.workspaces.values_list("name", flat=True)[:_MAX_TERMS] if n]
    except Exception:
        return []
