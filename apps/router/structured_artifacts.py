"""Detect and externalize oversized GFM tables from persisted replies."""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

_DELIMITER_CELL_RE = re.compile(r"^\s*:?-{3,}:?\s*$")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_HEADING_RE = re.compile(r"^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$")


@dataclass(frozen=True)
class ArtifactThresholds:
    individual_rows: int = 25
    individual_chars: int = 6000
    aggregate_rows: int = 40
    aggregate_chars: int = 8000


DEFAULT_THRESHOLDS = ArtifactThresholds()


@dataclass(frozen=True)
class TableSpan:
    start: int
    end: int
    text: str
    row_count: int
    char_count: int
    heading: str | None = None


@dataclass(frozen=True)
class ExternalizationResult:
    stored_text: str
    journal_link: dict | None
    moved: bool
    row_count: int
    table_count: int
    document_id: str | None
    failure_reason: str | None


@dataclass(frozen=True)
class _LineInfo:
    start: int
    end: int
    content: str
    eligible: bool
    heading_before: str | None


def _split_cells(line: str) -> tuple[list[str], bool]:
    cells: list[str] = []
    current: list[str] = []
    active_ticks = 0
    saw_pipe = False
    i = 0
    while i < len(line):
        char = line[i]
        if char == "`":
            j = i + 1
            while j < len(line) and line[j] == "`":
                j += 1
            run = j - i
            if active_ticks == 0:
                active_ticks = run
            elif active_ticks == run:
                active_ticks = 0
            current.append(line[i:j])
            i = j
            continue
        if char == "|" and active_ticks == 0:
            slash_count = 0
            j = i - 1
            while j >= 0 and line[j] == "\\":
                slash_count += 1
                j -= 1
            if slash_count % 2 == 0:
                cells.append("".join(current))
                current = []
                saw_pipe = True
                i += 1
                continue
        current.append(char)
        i += 1
    cells.append("".join(current))

    stripped = line.strip()
    if saw_pipe and stripped.startswith("|"):
        cells = cells[1:]
    if saw_pipe and stripped.endswith("|") and cells:
        cells = cells[:-1]
    return cells, saw_pipe


def _scan_lines(text: str) -> list[_LineInfo]:
    raw_lines = text.splitlines(keepends=True)
    infos: list[_LineInfo] = []
    offset = 0
    fence_char = ""
    fence_len = 0
    nearest_heading: str | None = None

    for raw in raw_lines:
        content = raw.rstrip("\r\n")
        eligible = True
        fence_match = _FENCE_RE.match(content)
        if fence_char:
            eligible = False
            if fence_match:
                marker, suffix = fence_match.groups()
                if marker[0] == fence_char and len(marker) >= fence_len and not suffix.strip():
                    fence_char = ""
                    fence_len = 0
        elif fence_match:
            marker, _suffix = fence_match.groups()
            fence_char = marker[0]
            fence_len = len(marker)
            eligible = False
        elif content.startswith("\t") or content.startswith("    "):
            eligible = False

        infos.append(
            _LineInfo(
                start=offset,
                end=offset + len(raw),
                content=content,
                eligible=eligible,
                heading_before=nearest_heading,
            )
        )
        if eligible:
            heading_match = _HEADING_RE.match(content)
            if heading_match:
                nearest_heading = heading_match.group(1).strip()
        offset += len(raw)

    if text and (not raw_lines or offset < len(text)):
        content = text[offset:]
        infos.append(
            _LineInfo(
                start=offset,
                end=len(text),
                content=content,
                eligible=not fence_char and not content.startswith(("\t", "    ")),
                heading_before=nearest_heading,
            )
        )
    return infos


def find_gfm_tables(text: str) -> list[TableSpan]:
    """Return GFM table spans outside fenced and indented code blocks."""
    infos = _scan_lines(text)
    tables: list[TableSpan] = []
    i = 0
    while i + 1 < len(infos):
        header = infos[i]
        delimiter = infos[i + 1]
        if not header.eligible or not delimiter.eligible:
            i += 1
            continue
        header_cells, header_has_pipe = _split_cells(header.content)
        delimiter_cells, delimiter_has_pipe = _split_cells(delimiter.content)
        if (
            not header_has_pipe
            or not delimiter_has_pipe
            or not header_cells
            or len(header_cells) != len(delimiter_cells)
            or not all(_DELIMITER_CELL_RE.fullmatch(cell) for cell in delimiter_cells)
        ):
            i += 1
            continue

        body_end = i + 2
        while body_end < len(infos):
            row = infos[body_end]
            row_cells, row_has_pipe = _split_cells(row.content)
            if not row.eligible or not row_has_pipe or not row_cells:
                break
            body_end += 1

        end = infos[body_end - 1].end
        table_text = text[header.start : end].rstrip("\r\n")
        tables.append(
            TableSpan(
                start=header.start,
                end=end,
                text=table_text,
                row_count=body_end - (i + 2),
                char_count=len(table_text),
                heading=header.heading_before,
            )
        )
        i = body_end
    return tables


def select_large_tables(tables: list[TableSpan], defaults: ArtifactThresholds = DEFAULT_THRESHOLDS) -> list[TableSpan]:
    """Select tables exceeding an individual or aggregate persistence budget."""
    if not tables:
        return []
    total_rows = sum(table.row_count for table in tables)
    total_chars = sum(table.char_count for table in tables)
    if total_rows > defaults.aggregate_rows or total_chars > defaults.aggregate_chars:
        return list(tables)
    return [
        table
        for table in tables
        if table.row_count > defaults.individual_rows or table.char_count > defaults.individual_chars
    ]


def _pointer(*, table_count: int, row_count: int, document_title: str) -> str:
    if table_count == 1:
        return f"Saved the full table ({row_count} rows) to your Journal as “{document_title}”."
    return f"Saved the full tables ({table_count} tables, {row_count} rows) to your Journal as “{document_title}”."


def replace_selected_tables(text: str, spans: list[TableSpan], document_title: str) -> str:
    """Replace selected table spans with one deterministic Journal pointer."""
    if not spans:
        return text
    spans = sorted(spans, key=lambda span: span.start)
    pointer = _pointer(
        table_count=len(spans),
        row_count=sum(span.row_count for span in spans),
        document_title=document_title,
    )
    parts: list[str] = []
    cursor = 0
    for index, span in enumerate(spans):
        parts.append(text[cursor : span.start])
        if index == 0:
            parts.append(pointer)
            if span.end > span.start and text[span.end - 1 : span.end] in {"\n", "\r"}:
                parts.append("\n")
        cursor = span.end
    parts.append(text[cursor:])
    return "".join(parts)


def _tenant_now(tenant):
    tz_name = getattr(getattr(tenant, "user", None), "timezone", None) or "UTC"
    try:
        return timezone.now().astimezone(ZoneInfo(tz_name))
    except ZoneInfoNotFoundError:
        return timezone.now().astimezone(ZoneInfo("UTC"))


def _document_title(tenant, selected: list[TableSpan]) -> str:
    heading = selected[0].heading if selected else None
    if heading:
        return f"Table — {heading}"[:80].rstrip()
    return _tenant_now(tenant).strftime("Table from chat — %Y-%m-%d %H:%M")[:80]


def proactive_artifact_dedup_key(*, tenant, job_name: str, cleaned_message: str) -> str:
    """Build the design fallback for proactive sends without a delivery id."""
    local_date = _tenant_now(tenant).date().isoformat()
    return hashlib.sha256(f"{job_name} | {local_date} | {cleaned_message}".encode()).hexdigest()


def _telemetry(
    *,
    tenant,
    source: str,
    selected: list[TableSpan],
    flag_state: bool,
    moved: bool,
    failure_reason: str | None,
    failure_class: str | None = None,
) -> None:
    row_count = sum(table.row_count for table in selected)
    char_count = sum(table.char_count for table in selected)
    logger.info(
        "reply_artifact_telemetry source=%s flag_state=%s moved=%s table_count=%d row_count=%d char_count=%d failure_reason=%s failure_class=%s",
        source,
        flag_state,
        moved,
        len(selected),
        row_count,
        char_count,
        failure_reason or "-",
        failure_class or "-",
        extra={
            "event": "reply_artifact_telemetry",
            "tenant_id": str(tenant.id),
            "source": source,
            "flag_state": flag_state,
            "moved": moved,
            "table_count": len(selected),
            "row_count": row_count,
            "char_count": char_count,
            "failure_reason": failure_reason,
            "failure_class": failure_class,
        },
    )


def externalize_large_structured_reply(
    *,
    tenant,
    text: str,
    source: str,
    dedup_key: str,
    journal_link: dict | None = None,
    defaults: ArtifactThresholds = DEFAULT_THRESHOLDS,
) -> ExternalizationResult:
    """Move oversized GFM tables to Journal when the tenant flag is enabled."""
    tables = find_gfm_tables(text)
    selected = select_large_tables(tables, defaults)
    row_count = sum(table.row_count for table in selected)
    if not selected:
        return ExternalizationResult(text, journal_link, False, 0, 0, None, None)

    flag_state = bool(getattr(tenant, "experimental_reply_artifacts_to_journal", False))
    if not flag_state:
        _telemetry(
            tenant=tenant,
            source=source,
            selected=selected,
            flag_state=False,
            moved=False,
            failure_reason="flag_disabled",
        )
        return ExternalizationResult(text, journal_link, False, row_count, len(selected), None, "flag_disabled")

    from apps.journal.models import Document
    from apps.journal.reply_artifacts import (
        artifact_journal_link,
        document_contains_tables,
        upsert_reply_artifact,
    )

    try:
        with transaction.atomic():
            document = None
            if journal_link:
                document = Document.objects.filter(
                    tenant=tenant,
                    kind=journal_link.get("kind"),
                    slug=journal_link.get("slug"),
                ).first()
                if document is not None and not document_contains_tables(document, selected):
                    logger.info(
                        "reply_artifact_chip_collision tenant=%s source=%s kind=%s slug=%s",
                        tenant.id,
                        source,
                        journal_link.get("kind"),
                        journal_link.get("slug"),
                    )
                    document = None
            if document is None:
                document = upsert_reply_artifact(
                    tenant=tenant,
                    source=source,
                    dedup_key=dedup_key,
                    title=_document_title(tenant, selected),
                    markdown=text,
                )
    except Exception as exc:
        _telemetry(
            tenant=tenant,
            source=source,
            selected=selected,
            flag_state=True,
            moved=False,
            failure_reason="journal_write_failed",
            failure_class=type(exc).__name__,
        )
        return ExternalizationResult(
            text,
            journal_link,
            False,
            row_count,
            len(selected),
            None,
            "journal_write_failed",
        )

    generated_link = artifact_journal_link(document)
    shortened = replace_selected_tables(text, selected, generated_link["title"])
    _telemetry(
        tenant=tenant,
        source=source,
        selected=selected,
        flag_state=True,
        moved=True,
        failure_reason=None,
    )
    return ExternalizationResult(
        shortened,
        generated_link,
        True,
        row_count,
        len(selected),
        str(document.id),
        None,
    )
