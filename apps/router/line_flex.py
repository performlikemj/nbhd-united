"""LINE Flex Message builder.

Converts structured agent responses (markdown-like) into branded LINE Flex Messages.
Falls back to plain text only for content that exceeds Flex size limits.
"""
from __future__ import annotations

import re
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

    # Has bullet lists (3+ items)
    bullets = re.findall(r"^[\s]*[-\u2022*]\s+.+", text, re.MULTILINE)
    if len(bullets) >= 3:
        return "structured"

    # Has numbered lists (3+ items)
    numbered = re.findall(r"^[\s]*\d+[.)]\s+.+", text, re.MULTILINE)
    if len(numbered) >= 3:
        return "structured"

    # Multiple sections separated by double newlines with substantial content
    sections = [s.strip() for s in text.split("\n\n") if s.strip()]
    if len(sections) >= 4 and len(text) > 500:
        return "structured"

    # Medium-length messages still get a short bubble
    if len(text) <= 2000:
        return "short"

    return "plain_text"


def should_use_flex(text: str) -> bool:
    """Determine if a response warrants Flex formatting."""
    return classify_content(text) != "plain_text"


# ── Parsing ─────────────────────────────────────────────────────────────────

def _parse_sections(text: str) -> list[dict]:
    """Parse markdown-like text into sections.

    Returns a list of dicts with 'title' (optional) and 'content'.
    """
    lines = text.split("\n")
    sections: list[dict] = []
    current_title: str | None = None
    current_lines: list[str] = []

    for line in lines:
        header_match = re.match(r"^#{1,3}\s+(.+)", line)
        if not header_match:
            # Also treat emoji-prefixed lines as section headers
            # (e.g. "🔴 Worth knowing:", "📬 Newsletters:")
            emoji_match = re.match(r"^([^\x00-\x7F]\S*\s+.+?)$", line)
            if emoji_match and not re.match(r"^\s*[-\u2022*]\s", line):
                header_match = emoji_match

        if header_match:
            # Save previous section
            if current_lines or current_title:
                sections.append({
                    "title": current_title,
                    "content": "\n".join(current_lines).strip(),
                })
            current_title = header_match.group(1) if header_match.lastindex and header_match.group(1) else header_match.group(0)
            current_title = current_title.strip()
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
    """Extract bullet or numbered list items from content."""
    items = re.findall(r"^[\s]*(?:[-\u2022*]|\d+[.)])\s+(.+)", content, re.MULTILINE)
    return [_strip_md_inline(item.strip()) for item in items]


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

    # Check for list items in content
    list_items = _parse_list_items(content)
    if list_items:
        # Non-list content before the list
        non_list = re.sub(
            r"^[\s]*(?:[-\u2022*]|\d+[.)])\s+.+\n?", "", content, flags=re.MULTILINE
        ).strip()
        if non_list:
            components.append(_text_component(
                _strip_md_inline(non_list),
                color=COLORS["ink_muted"],
                margin="sm",
                line_spacing="4px",
            ))

        for item in list_items:
            components.append({
                "type": "box",
                "layout": "baseline",
                "margin": "sm",
                "spacing": "sm",
                "contents": [
                    _text_component(
                        "\u2022", size="sm", color=COLORS["signal"],
                        wrap=False, flex=0,
                    ),
                    _text_component(
                        item, size="sm", color=COLORS["ink_muted"],
                        flex=1, line_spacing="4px",
                    ),
                ],
            })
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
