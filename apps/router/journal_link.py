"""Shared parser for the journal deep-link marker.

The agent includes a marker on its OWN line to attach a
tappable "View in Journal" chip pointing at a specific journal document:

    [[journal-link: daily|2026-07-13|Morning Report]]

The three fields are ``kind|slug|title``:

* ``kind`` — a real ``apps.journal.models.Document.Kind`` value
  (``daily``/``weekly``/``monthly``/``goal``/``project``/``tasks``/``ideas``/
  ``memory``). Anything else is agent misuse and the whole marker is dropped.
* ``slug`` — the document's slug (an ISO date like ``2026-07-13`` for daily
  notes, or a slugified identifier). It MUST be the value the journal tool
  echoed / today's ISO date — never invented — because iOS navigates by it.
* ``title`` — a short human-readable label for the chip.

Mirrors :mod:`apps.router.quick_replies` in spirit, but scans every line because
journal-link markers must never be shown raw. Ordinary prose that references
the shape inline still passes through untouched. The marker name is case-
insensitive, every marker-only line is stripped, and malformed markers are
logged as telemetry warnings instead of raising — fail-open, never leak. When
more than one valid marker is present, the last valid one wins.

Only the iOS app path turns the parsed link into a stored structured field
(``AppChatMessage.journal_link`` / ``ProactiveOutbound.journal_link``);
Telegram/LINE discard it and keep only the stripped text.

``title`` is parsed (and length-validated) in PII-PLACEHOLDER space, before the
reply is ever rehydrated — a document titled "Reconnect with [PERSON_1]" would
be echoed placeholder-space by the agent. See :func:`rehydrate_journal_link`,
the shared helper the two owner-facing read seams (``chat_views._serialize_message``
and ``chat_history._app_rows`` / ``_proactive_rows``) both call so a stored
``[PERSON_1]`` in a title always resolves to the same real name at both seams,
and never ships raw. (``kind`` is an enum and ``slug`` is slugified/an ISO date,
so neither can carry PII — only ``title`` is rehydrated.)
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Cap on the chip title (placeholder-space at parse time). A title over this is
# treated as agent misuse and the marker is dropped (like an over-long
# quick-reply label). Kept well under Document.title's 256 so a long rehydrated
# real name still has headroom before the read-seam truncation kicks in.
MAX_TITLE_LEN = 80
# Matches ``apps.journal.models.Document.slug`` max_length — a slug that can't
# be a real Document slug can't resolve to a document on tap.
MAX_SLUG_LEN = 128

# Slugs are ISO dates (2026-07-13) or Django-slugified identifiers; both live in
# ``[A-Za-z0-9._-]``. Rejecting anything else keeps a stray ``|``-free but
# otherwise junk slug (spaces, brackets, control bytes) from ever reaching the
# iOS navigation contract.
_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# The whole trimmed line must match — no leading/trailing prose on that line —
# so a marker embedded mid-sentence remains ordinary text. A model sometimes
# wraps the marker itself in inline/fenced backticks; those unambiguous same-line
# variants are accepted too.
_JOURNAL_LINK_MARKER_RE = re.compile(r"^\[\[journal-link:\s*(.*)\]\]$", re.IGNORECASE)
_BACKTICK_WRAPPED_MARKER_RE = re.compile(
    r"^(?P<ticks>`|```)\[\[journal-link:\s*(.*)\]\](?P=ticks)$",
    re.IGNORECASE,
)
_JOURNAL_LINK_CANDIDATE_RE = re.compile(r"^(?:`|```)?\[\[journal-link:", re.IGNORECASE)


def extract_journal_link(
    text: str,
    *,
    tenant_id=None,
    channel: str = "",
) -> tuple[str, dict | None]:
    """Parse + strip standalone ``[[journal-link: kind|slug|title]]`` lines.

    Returns ``(text_without_marker, journal_link)``. ``journal_link`` is
    ``None`` when no valid marker is present. Marker-only lines are recognized
    anywhere in the reply (including single- or triple-backtick-wrapped forms),
    removed from delivered text, and the last valid marker supplies a
    ``{"kind", "slug", "title"}`` dict in PII-placeholder space. Inline prose
    containing marker syntax is not a marker-only line and remains unchanged.

    A malformed or invalid marker line (bad delimiters, not exactly three
    ``|``-separated fields, an unknown ``kind``, an empty / over-long / bad-
    charset ``slug``, or an empty / over-long ``title``) is still stripped and
    telemetry-logged. Production callers supply ``tenant_id``; the winning
    candidate then receives the same single indexed document-existence check,
    and a missing document drops the link. Omitting ``tenant_id`` is parser-only
    mode for isolated validation tests.
    """
    if not text:
        return text, None

    lines = text.split("\n")
    last_content_index = next(
        (index for index in range(len(lines) - 1, -1, -1) if lines[index].strip()),
        -1,
    )
    kept_lines: list[str] = []
    valid_candidates: list[dict] = []
    found_marker = False

    for index, line in enumerate(lines):
        sample = line.strip()
        is_marker, payload = _marker_payload(sample)
        if not is_marker:
            kept_lines.append(line)
            continue

        found_marker = True
        if index != last_content_index:
            _log_nonfinal_placement(
                tenant_id=tenant_id,
                channel=channel,
                sample=sample,
            )

        if payload is None:
            _log_malformed(
                tenant_id=tenant_id,
                channel=channel,
                reason="bad_syntax",
                sample=sample,
            )
            continue

        # Split on the FIRST two pipes only, so a title that itself contains a
        # "|" survives intact (kind/slug can't contain one).
        parts = payload.split("|", 2)
        if len(parts) != 3:
            _log_malformed(
                tenant_id=tenant_id,
                channel=channel,
                reason="field_count",
                sample=sample,
            )
            continue

        kind, slug, title = (part.strip() for part in parts)
        reason = _validation_error(kind, slug, title)
        if reason:
            _log_malformed(
                tenant_id=tenant_id,
                channel=channel,
                reason=reason,
                sample=sample,
            )
            continue
        valid_candidates.append({"kind": kind, "slug": slug, "title": title, "sample": sample})

    if not found_marker:
        return text, None

    remainder = re.sub(r"\n{3,}", "\n\n", "\n".join(kept_lines)).rstrip()
    if not valid_candidates:
        return remainder, None

    winner = valid_candidates[-1]
    if tenant_id is None:
        return remainder, {key: winner[key] for key in ("kind", "slug", "title")}

    from apps.journal.models import Document

    try:
        exists = Document.objects.filter(
            tenant_id=tenant_id,
            kind=winner["kind"],
            slug=winner["slug"],
        ).exists()
    except Exception:
        logger.exception("journal_link: document existence check failed; dropping link")
        return remainder, None
    if not exists:
        _log_malformed(
            tenant_id=tenant_id,
            channel=channel,
            reason="missing_document",
            sample=winner["sample"],
        )
        return remainder, None
    return remainder, {key: winner[key] for key in ("kind", "slug", "title")}


def _marker_payload(line: str) -> tuple[bool, str | None]:
    """Return ``(is_marker_line, payload)`` for a trimmed reply line."""
    match = _JOURNAL_LINK_MARKER_RE.match(line)
    if match:
        return True, match.group(1)
    match = _BACKTICK_WRAPPED_MARKER_RE.match(line)
    if match:
        return True, match.group(2)
    if _JOURNAL_LINK_CANDIDATE_RE.match(line):
        return True, None
    return False, None


def _validation_error(kind: str, slug: str, title: str) -> str | None:
    """Return a short reason string if the parsed fields are invalid, else None."""
    from apps.journal.models import Document

    if kind not in set(Document.Kind.values):
        return "bad_kind"
    if not slug or len(slug) > MAX_SLUG_LEN or not _SLUG_RE.match(slug):
        return "bad_slug"
    if not title or len(title) > MAX_TITLE_LEN:
        return "bad_title"
    return None


def _log_malformed(*, tenant_id, channel: str, reason: str, sample: str) -> None:
    logger.warning(
        "journal_link_marker_malformed",
        extra={
            "tenant_id": str(tenant_id) if tenant_id is not None else None,
            "channel": channel,
            "reason": reason,
            "sample": sample[:200],
        },
    )


def _log_nonfinal_placement(*, tenant_id, channel: str, sample: str) -> None:
    logger.info(
        "journal_link_marker_nonfinal",
        extra={
            "tenant_id": str(tenant_id) if tenant_id is not None else None,
            "channel": channel,
            "reason": "nonfinal_placement",
            "sample": sample[:200],
        },
    )


def rehydrate_journal_link(
    journal_link: dict | None,
    entity_map: dict | None,
    *,
    tenant_id=None,
    channel: str = "",
) -> dict | None:
    """Rehydrate a stored ``journal_link`` from PII-placeholder space to real
    values for owner-facing egress.

    Only ``title`` can carry PII (``kind`` is an enum, ``slug`` is slugified /
    an ISO date), so only ``title`` is rehydrated. Call this from BOTH owner-
    facing read seams (``chat_views._serialize_message`` and
    ``chat_history._app_rows`` / ``_proactive_rows``) so they can't drift on how
    a stored title resolves — mirrors :func:`rehydrate_quick_replies`.

    Unlike a quick-reply label (whose displayed text is re-sent verbatim on
    tap, so an overflow after rehydration DROPS the whole set), the chip title
    is display-only — the tap navigates by ``kind`` + ``slug``. A title that
    overflows ``MAX_TITLE_LEN`` once a placeholder resolves to a long real name
    is therefore TRUNCATED (display trimmed) rather than dropped: the link stays
    tappable, which is the useful part. A telemetry warning is still logged.

    Fails open on an unexpected rehydration error: serves the link with the
    title unchanged (still placeholder-space — a stale display label, not a
    security leak), mirroring how ``reply_text`` rehydration fails open at the
    same seams.
    """
    if not journal_link:
        return None
    title = journal_link.get("title") or ""
    from apps.router.reply_text import finalize_outbound_text

    title = finalize_outbound_text(
        title,
        entity_map,
        tenant_id=tenant_id,
        channel=f"{channel}_journal_link",
    )

    if len(title) > MAX_TITLE_LEN:
        # sample is the PLACEHOLDER-space title, never the rehydrated one — the
        # latter holds the real value (a name); this warning lands in Azure
        # console logs (Sentry's PII-off setting doesn't govern custom `extra`
        # fields on our own logger calls).
        _log_malformed(
            tenant_id=tenant_id,
            channel=channel,
            reason="rehydration_overflow",
            sample=journal_link.get("title") or "",
        )
        title = title[:MAX_TITLE_LEN].rstrip()
    return {**journal_link, "title": title}


# The literal opener this streaming heuristic watches for. Checked against at
# least 3 characters so a bare "[[" — which could be the start of ANY marker
# ([[chart:, [[button:, [[quick-replies:, [[journal-link:...) — isn't mistaken
# for this one; the third character alone ("j") already disambiguates.
_STREAMING_MARKER_OPENER = "[[journal-link:"
_STREAMING_MIN_MATCH = 3


def strip_streaming_journal_link_marker(text: str) -> str:
    """Hide an in-progress ``[[journal-link: ...`` marker from the LIVE
    streaming ``partial_text`` bubble — whether it's mid-typing (unclosed) or
    already complete. ``partial_text`` is cumulative text-so-far written on
    every model-call step (see ``ChatProgressEventView``), so this runs on
    every write; a marker that hasn't started yet (no trailing ``[[``) is a
    no-op. Purely cosmetic — real parsing/validation only ever happens on the
    terminal reply via :func:`extract_journal_link`.
    """
    if not text:
        return text
    idx = text.rfind("[[")
    if idx == -1:
        return text
    tail = text[idx:]
    check_len = min(len(tail), len(_STREAMING_MARKER_OPENER))
    if check_len < _STREAMING_MIN_MATCH:
        return text
    if tail[:check_len].lower() != _STREAMING_MARKER_OPENER[:check_len].lower():
        return text
    cut = idx - 1 if idx > 0 and text[idx - 1] == "\n" else idx
    return text[:cut]
