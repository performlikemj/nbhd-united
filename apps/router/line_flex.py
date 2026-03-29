"""LINE Flex Message builder.

Converts structured agent responses (markdown-like) into branded LINE Flex Messages.
Falls back to plain text only for content that exceeds Flex size limits.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any


# ── Brand Palette ───────────────────────────────────────────────────────────

COLORS = {
    "ink": "#12232c",
    "ink_muted": "#3d4f58",
    "ink_faint": "#6b7b84",
    "signal": "#5fbaaf",
    "signal_text": "#0f766e",
    "mist": "#f6f4ee",
    "white": "#ffffff",
    "separator": "#e8e4dc",
    "emerald_bg": "#ecfdf5",
    "emerald_text": "#065f46",
    "rose_bg": "#fff1f2",
    "rose_text": "#9f1239",
    "amber_bg": "#fffbeb",
    "amber_text": "#92400e",
}

CLIPBOARD_MAX = 5000  # safe limit for clipboard action text

_TONE_MAP = {
    "success": {"bg": COLORS["emerald_bg"], "fg": COLORS["emerald_text"], "icon": "\u2713"},
    "error": {"bg": COLORS["rose_bg"], "fg": COLORS["rose_text"], "icon": "\u2717"},
    "warning": {"bg": COLORS["amber_bg"], "fg": COLORS["amber_text"], "icon": "\u26a0"},
}


# ── Bullet helpers ─────────────────────────────────────────────────────────

# Standard bullet markers: -, •, *
_STD_BULLET_RE = re.compile(r"^([\s]*)(?:[-\u2022*])\s+(.+)", re.MULTILINE)
# Numbered list items: 1. or 1)
_NUM_BULLET_RE = re.compile(r"^([\s]*)\d+[.)]\s+(.+)", re.MULTILINE)
# Emoji bullet: line starts with optional whitespace then an emoji followed by a space
_EMOJI_BULLET_RE = re.compile(r"^([\s]*)(\S)\s+(.+)", re.MULTILINE)


def _is_emoji_char(ch: str) -> bool:
    """Check if a character is an emoji (Unicode Symbol, Other)."""
    return not ch.isascii() and unicodedata.category(ch) == "So"


def _is_bullet_line(line: str) -> bool:
    """Check if a line is any kind of bullet (standard, numbered, or emoji)."""
    stripped = line.strip()
    if not stripped:
        return False
    if re.match(r"^[-\u2022*]\s+", stripped):
        return True
    if re.match(r"^\d+[.)]\s+", stripped):
        return True
    if _is_emoji_char(stripped[0]) and len(stripped) > 2 and stripped[1] == " ":
        return True
    return False


def _parse_list_items_with_depth(content: str) -> list[tuple[int, str]]:
    """Extract list items with indentation depth.

    Returns list of (depth, text) tuples.  depth 0 = top-level,
    depth 1 = indented (2+ spaces or tab before the bullet marker).
    """
    items: list[tuple[int, str]] = []
    for line in content.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue

        # Measure leading whitespace for nesting depth
        leading = len(line) - len(line.lstrip())
        depth = 1 if leading >= 2 else 0

        # Standard bullet
        m = re.match(r"^[-\u2022*]\s+(.+)", stripped)
        if m:
            items.append((depth, _strip_md_inline(m.group(1).strip())))
            continue

        # Numbered
        m = re.match(r"^\d+[.)]\s+(.+)", stripped)
        if m:
            items.append((depth, _strip_md_inline(m.group(1).strip())))
            continue

        # Emoji bullet
        if _is_emoji_char(stripped[0]) and len(stripped) > 2 and stripped[1] == " ":
            items.append((depth, _strip_md_inline(stripped)))
            continue

    return items


# ── Detection ───────────────────────────────────────────────────────────────

def classify_content(text: str) -> str:
    """Classify content type for template selection.

    Returns: 'short', 'structured', or 'plain_text'.
    """
    if not text or not text.strip():
        return "short"

    if len(text) < 200 and "\n" not in text:
        return "short"

    # Has markdown headers
    if re.search(r"^#{1,3}\s+.+", text, re.MULTILINE):
        return "structured"

    # Has bullet lists (3+ items) — standard or emoji bullets
    bullet_count = sum(1 for line in text.split("\n") if _is_bullet_line(line))
    if bullet_count >= 3:
        return "structured"

    # Has section-like structure: short header lines followed by content
    # (catches emoji headers, plain text headers like "Today", etc.)
    sections = _parse_sections(text)
    if len(sections) >= 2 and any(s.get("title") for s in sections):
        return "structured"

    # Multiple sections separated by double newlines with substantial content
    para_sections = [s.strip() for s in text.split("\n\n") if s.strip()]
    if len(para_sections) >= 4 and len(text) > 500:
        return "structured"

    # Medium-length messages still get a short bubble
    if len(text) <= 2000:
        return "short"

    return "plain_text"


def should_use_flex(text: str) -> bool:
    """Determine if a response warrants Flex formatting."""
    return classify_content(text) != "plain_text"


# ── Parsing ─────────────────────────────────────────────────────────────────

def _is_header_line(stripped: str) -> bool:
    """Check if a non-markdown line looks like a section header.

    Recognizes (when preceded by a blank line):
    - Emoji-prefixed: 🔴 Worth knowing:
    - Plain text headers: Today, Yesterday, Action Required

    Note: Markdown headers (## Title) are handled separately and don't
    need blank-line gating. This function is only called for non-## lines.
    """
    if not stripped:
        return False

    # Emoji-prefixed (Unicode category So = Symbol, Other)
    if (not stripped[0].isascii()
            and unicodedata.category(stripped[0]) == "So"
            and re.match(r"^\S+\s+.+$", stripped)):
        return True

    # Plain text header: short, starts uppercase, looks like a label
    # Must be short (< 40 chars), few words (< 6), and not a full sentence
    if (stripped[0].isascii()
            and stripped[0].isupper()
            and len(stripped) < 40
            and len(stripped.split()) < 6
            and not stripped.endswith(".")
            and not stripped.endswith("!")):
        return True

    return False


def _parse_sections(text: str) -> list[dict]:
    """Parse text into sections with titles and content.

    Uses a two-pass approach:
    1. Identify header lines (markdown, emoji-prefixed, or plain text headers
       that appear after a blank line)
    2. Group content under each header

    Returns a list of dicts with 'title' (optional) and 'content'.
    """
    lines = text.split("\n")

    # Pre-scan: identify which lines are emoji-prefixed to detect emoji
    # bullet *lists* (consecutive emoji lines) vs emoji *headers* (isolated
    # emoji line followed by different content).
    emoji_line_indices: set[int] = set()
    for i, line in enumerate(lines):
        s = line.strip()
        if (s and _is_emoji_char(s[0])
                and len(s) > 2 and s[1] == " "):
            emoji_line_indices.add(i)

    def _is_emoji_list_item(idx: int) -> bool:
        """True if this emoji-prefixed line is part of a cluster (≥2 nearby)."""
        if idx not in emoji_line_indices:
            return False
        # Check neighbours (skip blanks)
        for direction in (-1, 1):
            j = idx + direction
            while 0 <= j < len(lines):
                if not lines[j].strip():
                    j += direction
                    continue
                if j in emoji_line_indices:
                    return True
                break
        return False

    # Pass 1: identify header line indices
    header_indices: set[int] = set()
    prev_blank = False  # first line needs blank line AFTER it to qualify as header

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Blank lines and horizontal rules are boundaries, not headers
        if not stripped or re.match(r"^[-*_]{3,}$", stripped):
            prev_blank = True
            continue

        # Bullet/numbered/emoji-list items are never headers
        if re.match(r"^\s*[-\u2022*]\s|^\s*\d+[.)]\s", stripped):
            prev_blank = False
            continue
        if _is_emoji_list_item(i):
            prev_blank = False
            continue

        # Markdown headers (## Title) — always a header
        if re.match(r"^#{1,3}\s+", stripped):
            header_indices.add(i)
        # Emoji headers (🔴 Title) — only when NOT part of an emoji bullet list
        elif (not stripped[0].isascii()
                and unicodedata.category(stripped[0]) == "So"
                and re.match(r"^\S+\s+.+$", stripped)):
            header_indices.add(i)
        # Plain text headers — only after a blank line (to avoid false positives)
        elif prev_blank and _is_header_line(stripped):
            header_indices.add(i)

        prev_blank = False

    # Pass 2: group lines into sections
    sections: list[dict] = []
    current_title: str | None = None
    current_lines: list[str] = []

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Skip horizontal rules
        if re.match(r"^[\s]*[-*_]{3,}\s*$", line):
            continue

        if i in header_indices:
            # Save previous section
            if current_lines or current_title:
                sections.append({
                    "title": current_title,
                    "content": "\n".join(current_lines).strip(),
                })
            # Extract title text (strip markdown # prefix if present)
            md_match = re.match(r"^#{1,3}\s+(.+)", stripped)
            current_title = md_match.group(1).strip() if md_match else stripped
            current_lines = []
        else:
            current_lines.append(line)

    # Save last section
    if current_lines or current_title:
        sections.append({
            "title": current_title,
            "content": "\n".join(current_lines).strip(),
        })

    return [s for s in sections if s.get("title") or s.get("content")]


def _strip_md_inline(text: str) -> str:
    """Strip inline markdown (bold, italic, code, links) for Flex text."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"_(.+?)_", r"\1", text)
    text = re.sub(r"`(.+?)`", r"\1", text)
    # Remove markdown links — they'll be rendered as tappable link components
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    return text


def _extract_links(text: str) -> list[tuple[str, str]]:
    """Extract links from text. Returns list of (label, url) tuples.

    Handles markdown links [label](url) and bare https:// URLs.
    """
    links: list[tuple[str, str]] = []
    seen_urls: set[str] = set()

    # Markdown links: [label](url)
    for label, url in re.findall(r"\[([^\]]+)\]\(([^)]+)\)", text):
        url = url.strip()
        if url not in seen_urls:
            links.append((label.strip(), url))
            seen_urls.add(url)

    # Bare URLs (not already captured as markdown link targets)
    for url in re.findall(r"(?<!\()https?://[^\s\)\]]+", text):
        url = url.rstrip(".,;:!?")
        if url not in seen_urls:
            # Use domain as label
            domain = re.sub(r"https?://(?:www\.)?", "", url).split("/")[0]
            links.append((domain, url))
            seen_urls.add(url)

    return links


def _link_component(label: str, url: str) -> dict:
    """Build a tappable link row (box with URI action)."""
    return {
        "type": "box",
        "layout": "baseline",
        "margin": "sm",
        "spacing": "sm",
        "action": {
            "type": "uri",
            "label": label[:40],
            "uri": url,
        },
        "contents": [
            _text_component(
                "\U0001f517", size="xs", color=COLORS["signal_text"],
                wrap=False, flex=0,
            ),
            _text_component(
                label[:80], size="sm", color=COLORS["signal_text"],
                flex=1,
            ),
        ],
    }


def _parse_list_items(content: str) -> list[str]:
    """Extract bullet or numbered list items from content (flat, no depth)."""
    return [text for _depth, text in _parse_list_items_with_depth(content)]


# ── Flex Components ─────────────────────────────────────────────────────────

def _text_component(
    text: str,
    *,
    size: str = "sm",
    color: str | None = None,
    weight: str = "regular",
    wrap: bool = True,
    margin: str | None = None,
    flex: int | None = None,
    line_spacing: str | None = None,
) -> dict:
    """Create a Flex text component."""
    comp: dict[str, Any] = {
        "type": "text",
        "text": text[:2000],  # LINE text component limit
        "size": size,
        "color": color or COLORS["ink_muted"],
        "weight": weight,
        "wrap": wrap,
    }
    if margin:
        comp["margin"] = margin
    if flex is not None:
        comp["flex"] = flex
    if line_spacing:
        comp["lineSpacing"] = line_spacing
    return comp


def _separator(margin: str = "lg", color: str | None = None) -> dict:
    sep: dict[str, Any] = {"type": "separator", "margin": margin}
    if color:
        sep["color"] = color
    return sep


def _copy_footer(raw_text: str) -> dict:
    """Subtle footer with a clipboard tap target styled as faint helper text."""
    clean = _strip_md_inline(raw_text.strip())
    if len(clean) > CLIPBOARD_MAX:
        clean = clean[: CLIPBOARD_MAX - 1] + "\u2026"
    return {
        "type": "box",
        "layout": "vertical",
        "paddingTop": "8px",
        "paddingBottom": "12px",
        "paddingStart": "16px",
        "paddingEnd": "16px",
        "action": {
            "type": "clipboard",
            "label": "Copy text",
            "clipboardText": clean,
        },
        "contents": [
            {
                "type": "text",
                "text": "Tap to copy",
                "size": "xxs",
                "color": COLORS["ink_faint"],
                "align": "end",
            },
        ],
    }


def _section_box(title: str | None, content: str, is_first: bool = False) -> list[dict]:
    """Build Flex components for a single section."""
    components: list[dict] = []

    if title:
        components.append(_text_component(
            _strip_md_inline(title),
            size="md",
            color=COLORS["ink"],
            weight="bold",
        ))

    if not content:
        return components

    # Extract links before stripping markdown (we need raw markdown/URLs)
    links = _extract_links(content)

    # Check for list items in content (with nesting depth)
    list_items_with_depth = _parse_list_items_with_depth(content)
    if list_items_with_depth:
        # Non-list content before the list
        non_list_lines = [
            line for line in content.split("\n")
            if line.strip() and not _is_bullet_line(line)
        ]
        non_list = "\n".join(non_list_lines).strip()
        if non_list:
            components.append(_text_component(
                _strip_md_inline(non_list),
                color=COLORS["ink_muted"],
                margin="sm",
                line_spacing="4px",
            ))

        for depth, item in list_items_with_depth:
            bullet_box: dict[str, Any] = {
                "type": "box",
                "layout": "baseline",
                "margin": "sm",
                "spacing": "sm",
                "contents": [
                    _text_component(
                        "\u2022", size="xs" if depth > 0 else "sm",
                        color=COLORS["ink_faint"] if depth > 0 else COLORS["signal"],
                        wrap=False, flex=0,
                    ),
                    _text_component(
                        item, size="xs" if depth > 0 else "sm",
                        color=COLORS["ink_muted"],
                        flex=1, line_spacing="4px",
                    ),
                ],
            }
            # Indent nested items
            if depth > 0:
                bullet_box["paddingStart"] = "16px"
            components.append(bullet_box)
    else:
        # Strip bare URLs from display text when we have link components
        display_text = _strip_md_inline(content)
        if links:
            display_text = re.sub(r"https?://[^\s]+", "", display_text)
            display_text = re.sub(r"\n{2,}", "\n", display_text).strip()
        if display_text:
            components.append(_text_component(
                display_text,
                color=COLORS["ink_muted"],
                margin="sm" if title else None,
                line_spacing="4px",
            ))

    # Add tappable link rows
    for label, url in links:
        components.append(_link_component(label, url))

    return components


# ── Bubble Builders ─────────────────────────────────────────────────────────

def build_short_bubble(text: str, alt_text: str = "Message from your assistant") -> dict:
    """Build a compact bubble with accent bar for short messages."""
    links = _extract_links(text)
    display_text = _strip_md_inline(text.strip())
    if links:
        display_text = re.sub(r"https?://[^\s]+", "", display_text)
        display_text = re.sub(r"\n{2,}", "\n", display_text).strip()

    body_contents: list[dict] = [
        {
            "type": "box",
            "layout": "horizontal",
            "spacing": "lg",
            "contents": [
                {
                    "type": "box",
                    "layout": "vertical",
                    "width": "4px",
                    "backgroundColor": COLORS["signal"],
                    "cornerRadius": "2px",
                    "contents": [{"type": "filler"}],
                },
                _text_component(
                    display_text or text.strip(),
                    size="sm",
                    color=COLORS["ink"],
                    flex=1,
                    line_spacing="4px",
                ),
            ],
        },
    ]

    # Add tappable link rows below the accent bar
    for label, url in links:
        body_contents.append(_link_component(label, url))

    bubble = {
        "type": "bubble",
        "size": "mega",
        "styles": {
            "body": {"backgroundColor": COLORS["mist"]},
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "16px",
            "spacing": "md",
            "contents": body_contents,
        },
        "footer": _copy_footer(text),
    }

    return {
        "type": "flex",
        "altText": alt_text[:400],
        "contents": bubble,
    }


def build_status_bubble(
    text: str,
    tone: str = "success",
    alt_text: str = "Message from your assistant",
) -> dict:
    """Build a compact status bubble (success/error/warning)."""
    style = _TONE_MAP.get(tone, _TONE_MAP["success"])
    return {
        "type": "flex",
        "altText": alt_text[:400],
        "contents": {
            "type": "bubble",
            "size": "mega",
            "styles": {
                "body": {"backgroundColor": style["bg"]},
            },
            "body": {
                "type": "box",
                "layout": "horizontal",
                "paddingAll": "16px",
                "spacing": "md",
                "contents": [
                    _text_component(
                        style["icon"], size="lg", color=style["fg"],
                        wrap=False, flex=0,
                    ),
                    _text_component(
                        _strip_md_inline(text.strip()),
                        size="sm", color=style["fg"],
                        flex=1, line_spacing="4px",
                    ),
                ],
            },
        },
    }


def build_flex_bubble(text: str, alt_text: str = "Message from your assistant") -> dict:
    """Build a branded Flex bubble from structured text.

    Dispatches to the appropriate template based on content classification.
    Returns a LINE Flex message object ready for the Push/Reply API.
    """
    content_type = classify_content(text)

    if content_type == "short":
        return build_short_bubble(text, alt_text)

    # Structured content: full branded bubble
    sections = _parse_sections(text)

    if not sections:
        sections = [{"title": None, "content": _strip_md_inline(text)}]

    # Determine if first section title becomes the bubble header
    header_block = None
    body_sections = sections

    if sections[0].get("title"):
        header_title = _strip_md_inline(sections[0]["title"])
        header_content_text = sections[0].get("content", "").strip()

        header_block = {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "16px",
            "paddingBottom": "14px",
            "contents": [
                _text_component(
                    header_title,
                    size="lg",
                    color=COLORS["white"],
                    weight="bold",
                ),
            ],
        }

        # If the first section also has body content, keep it in the body
        if header_content_text:
            body_sections = [{"title": None, "content": header_content_text}] + sections[1:]
        else:
            body_sections = sections[1:]

    # Build body contents
    body_contents: list[dict] = []

    for i, section in enumerate(body_sections):
        if i > 0:
            body_contents.append(_separator(color=COLORS["separator"]))

        components = _section_box(
            section.get("title"),
            section.get("content", ""),
            is_first=(i == 0),
        )
        body_contents.extend(components)

    # Trim to avoid oversized Flex (LINE has ~50KB limit per message)
    if len(body_contents) > 30:
        body_contents = body_contents[:30]

    # If no body contents at all after header extraction, add placeholder
    if not body_contents:
        body_contents = [_text_component(" ", color=COLORS["ink_muted"])]

    # Assemble bubble
    bubble: dict[str, Any] = {
        "type": "bubble",
        "size": "mega",
        "styles": {
            "body": {"backgroundColor": COLORS["mist"]},
        },
    }

    if header_block:
        bubble["styles"]["header"] = {"backgroundColor": COLORS["signal_text"]}
        bubble["header"] = header_block

    bubble["body"] = {
        "type": "box",
        "layout": "vertical",
        "spacing": "md",
        "paddingAll": "16px",
        "contents": body_contents,
    }

    bubble["footer"] = _copy_footer(text)

    return {
        "type": "flex",
        "altText": alt_text[:400],
        "contents": bubble,
    }


def build_flex_carousel(
    items: list[dict[str, str]],
    alt_text: str = "Message from your assistant",
) -> dict:
    """Build a Flex carousel from a list of items.

    Each item should have 'title' and optionally 'content', 'action_label', 'action_data'.
    """
    bubbles = []
    for item in items[:12]:  # LINE carousel max
        body_contents = []
        if item.get("title"):
            body_contents.append(_text_component(
                item["title"],
                size="md",
                color=COLORS["ink"],
                weight="bold",
            ))
        if item.get("content"):
            body_contents.append(_text_component(
                item["content"],
                size="sm",
                color=COLORS["ink_muted"],
                margin="sm",
            ))

        bubble: dict[str, Any] = {
            "type": "bubble",
            "size": "kilo",
            "styles": {
                "body": {"backgroundColor": COLORS["mist"]},
            },
            "body": {
                "type": "box",
                "layout": "vertical",
                "spacing": "sm",
                "contents": body_contents or [_text_component("(empty)")],
            },
        }

        # Add action button in footer if specified
        if item.get("action_label") and item.get("action_data"):
            bubble["footer"] = {
                "type": "box",
                "layout": "vertical",
                "contents": [{
                    "type": "button",
                    "style": "primary",
                    "color": COLORS["signal_text"],
                    "action": {
                        "type": "postback",
                        "label": item["action_label"][:20],
                        "data": item["action_data"],
                        "displayText": item["action_label"],
                    },
                }],
            }

        bubbles.append(bubble)

    return {
        "type": "flex",
        "altText": alt_text[:400],
        "contents": {
            "type": "carousel",
            "contents": bubbles,
        },
    }


# ── Quick Reply ─────────────────────────────────────────────────────────────

def extract_quick_reply_buttons(text: str) -> tuple[str, list[dict] | None]:
    """Extract inline button markers from text, return cleaned text + quick reply items.

    Looks for [[button:label|callback_data]] patterns.
    Returns (cleaned_text, quick_reply_items_or_None).
    """
    pattern = r"\[\[button:([^|]+)\|([^\]]+)\]\]"
    matches = re.findall(pattern, text)

    if not matches:
        return text, None

    # Remove button markers from text
    cleaned = re.sub(pattern, "", text).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)

    items = []
    for label, data in matches[:13]:  # LINE max 13 quick reply buttons
        items.append({
            "type": "action",
            "action": {
                "type": "postback",
                "label": label.strip()[:20],  # LINE label max 20 chars
                "data": data.strip(),
                "displayText": label.strip(),
            },
        })

    return cleaned, items if items else None


def attach_quick_reply(message: dict, items: list[dict]) -> dict:
    """Attach quick reply items to a LINE message object."""
    message["quickReply"] = {"items": items}
    return message


def telegram_keyboard_to_quick_reply(
    keyboard: list[list[dict[str, str]]],
) -> list[dict]:
    """Convert a Telegram inline keyboard to LINE Quick Reply items.

    Telegram keyboards are 2D (rows of buttons), each with "text" and
    "callback_data".  LINE Quick Reply is a flat list of up to 13 postback
    actions with labels capped at 20 chars.
    """
    items: list[dict] = []
    for row in keyboard:
        for btn in row:
            label = btn.get("text", "")[:20]
            data = btn.get("callback_data", "")
            items.append({
                "type": "action",
                "action": {
                    "type": "postback",
                    "label": label,
                    "data": data,
                    "displayText": btn.get("text", ""),
                },
            })
            if len(items) >= 13:
                return items
    return items
