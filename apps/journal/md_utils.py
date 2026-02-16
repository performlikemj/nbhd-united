"""Markdown parser/serializer for daily notes.

Daily notes follow this format:

    # 2026-02-15

    ## 09:30 — MJ
    Started working on the demo video edit.
    Energy: 7 | Mood: 😊

    ## 23:00 — Evening Check-in (Agent)
    ### What happened today
    - Timezone feature merged

    ### Decisions
    - Journaling module will mirror OpenClaw model
"""
from __future__ import annotations

import re
from typing import Any


# Pattern for ## HH:MM — Author or ## HH:MM — Section Title (Author)
_ENTRY_HEADER_RE = re.compile(
    r"^##\s+"
    r"(?P<time>\d{1,2}:\d{2})"           # required time
    r"(?:\s*—\s*|\s+)"                    # separator
    r"(?P<title>.+?)"                     # author or "Section Title (Author)"
    r"\s*$"
)

_SECTION_PAREN_RE = re.compile(r"^(?P<section>.+?)\s*\((?P<author>[^)]+)\)$")
_ENERGY_RE = re.compile(r"Energy:\s*(\d+)", re.IGNORECASE)
_MOOD_RE = re.compile(r"Mood:\s*(\S+)", re.IGNORECASE)
_SUBSECTION_RE = re.compile(r"^###\s+(.+)$")


def _normalise_author(raw: str) -> str:
    """Map free-text author labels to 'human' or 'agent'."""
    lower = raw.strip().lower()
    if lower in ("agent", "ai", "assistant", "bot"):
        return "agent"
    return "human"


def _slugify_section(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.strip().lower()).strip("-")


def parse_daily_note(markdown: str) -> list[dict[str, Any]]:
    """Parse a daily-note markdown document into a list of entry dicts.

    .. deprecated::
        Use ``apps.journal.services.parse_daily_sections`` for the sections-based
        model instead.

    Each entry dict has keys:
        time (str|None), author (str), content (str),
        mood (str|None), energy (int|None),
        section (str|None), subsections (dict|None)
    """
    if not markdown or not markdown.strip():
        return []

    lines = markdown.split("\n")
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    current_lines: list[str] = []

    def _flush():
        nonlocal current, current_lines
        if current is not None:
            _finalise_entry(current, current_lines)
            entries.append(current)
            current = None
            current_lines = []

    for line in lines:
        # Skip the top-level date header
        if line.startswith("# ") and not line.startswith("## "):
            continue

        m = _ENTRY_HEADER_RE.match(line)
        if m:
            _flush()
            time_str = m.group("time")
            title = m.group("title").strip()

            # Check for "Section Title (Author)" pattern
            pm = _SECTION_PAREN_RE.match(title)
            if pm:
                section = _slugify_section(pm.group("section"))
                author = _normalise_author(pm.group("author"))
            else:
                section = None
                author = _normalise_author(title)

            current = {
                "time": time_str,
                "author": author,
                "content": "",
                "mood": None,
                "energy": None,
                "section": section,
                "subsections": None,
            }
            current_lines = []
            continue

        if current is not None:
            current_lines.append(line)

    _flush()
    return entries


def _finalise_entry(entry: dict[str, Any], lines: list[str]):
    """Extract mood, energy, subsections from raw body lines."""
    # Check for subsections (### headers)
    subsection_indices = [
        i for i, ln in enumerate(lines) if _SUBSECTION_RE.match(ln)
    ]

    if subsection_indices:
        # Content before first subsection
        pre = "\n".join(lines[: subsection_indices[0]]).strip()
        entry["content"] = pre

        subsections: dict[str, str] = {}
        for idx, si in enumerate(subsection_indices):
            m = _SUBSECTION_RE.match(lines[si])
            name = _slugify_section(m.group(1))  # type: ignore[union-attr]
            end = subsection_indices[idx + 1] if idx + 1 < len(subsection_indices) else len(lines)
            body = "\n".join(lines[si + 1 : end]).strip()
            subsections[name] = body
        entry["subsections"] = subsections if subsections else None
    else:
        body = "\n".join(lines).strip()

        # Extract energy/mood from metadata line
        clean_lines = []
        for ln in body.split("\n"):
            em = _ENERGY_RE.search(ln)
            mm = _MOOD_RE.search(ln)
            if em:
                entry["energy"] = int(em.group(1))
            if mm:
                entry["mood"] = mm.group(1)
            if em or mm:
                # If the line is ONLY metadata, skip it
                stripped = _ENERGY_RE.sub("", ln)
                stripped = _MOOD_RE.sub("", stripped)
                stripped = stripped.replace("|", "").strip()
                if not stripped:
                    continue
            clean_lines.append(ln)

        entry["content"] = "\n".join(clean_lines).strip()


def serialise_entry(entry: dict[str, Any]) -> str:
    """Serialise a single entry dict back to markdown lines."""
    parts = []

    # Header
    time_part = entry.get("time") or ""
    author_label = "Agent" if entry.get("author") == "agent" else (entry.get("author_label") or "MJ")
    section = entry.get("section")

    if section:
        section_title = section.replace("-", " ").title()
        header = f"## {time_part} — {section_title} ({author_label})".strip()
    else:
        header = f"## {time_part} — {author_label}".strip()
    # Clean up double spaces
    header = re.sub(r"\s+", " ", header).replace("## —", "##")
    parts.append(header)

    # Content
    content = entry.get("content", "")
    if content:
        parts.append(content)

    # Metadata line
    meta_parts = []
    if entry.get("energy") is not None:
        meta_parts.append(f"Energy: {entry['energy']}")
    if entry.get("mood") is not None:
        meta_parts.append(f"Mood: {entry['mood']}")
    if meta_parts:
        parts.append(" | ".join(meta_parts))

    # Subsections
    subsections = entry.get("subsections")
    if subsections:
        for name, body in subsections.items():
            title = name.replace("-", " ").title()
            parts.append(f"### {title}")
            parts.append(body)

    return "\n".join(parts)


def serialise_daily_note(date_str: str, entries: list[dict[str, Any]]) -> str:
    """Serialise a full daily note from date + entries list.

    .. deprecated::
        Use ``apps.journal.services.materialize_sections_markdown`` instead.
    """
    parts = [f"# {date_str}"]
    for entry in entries:
        parts.append("")
        parts.append(serialise_entry(entry))

    return "\n".join(parts) + "\n"


def append_entry_markdown(
    existing_md: str,
    *,
    time: str,
    author: str,
    content: str,
    mood: str | None = None,
    energy: int | None = None,
    date_str: str | None = None,
) -> str:
    """Append a new entry to existing markdown, returning the updated doc.

    .. deprecated::
        Use ``apps.journal.services.append_log_to_note`` instead.
    """
    author_label = "Agent" if author == "agent" else "MJ"
    lines = []

    if not existing_md or not existing_md.strip():
        if date_str:
            lines.append(f"# {date_str}")
            lines.append("")

    header = f"## {time} — {author_label}"
    lines.append(header)
    lines.append(content)

    meta_parts = []
    if energy is not None:
        meta_parts.append(f"Energy: {energy}")
    if mood is not None:
        meta_parts.append(f"Mood: {mood}")
    if meta_parts:
        lines.append(" | ".join(meta_parts))

    new_block = "\n".join(lines)

    if existing_md and existing_md.strip():
        # Ensure there's a blank line before the new entry
        base = existing_md.rstrip()
        return base + "\n\n" + new_block + "\n"
    else:
        return new_block + "\n"
