"""LINE Flex Message builder.

Converts structured agent responses (markdown-like) into LINE Flex Messages.
Falls back to plain text for short/simple responses.
"""
from __future__ import annotations

import re
from typing import Any


# ── Detection ────────────────────────────────────────────────────────────────

def should_use_flex(text: str) -> bool:
    """Determine if a response warrants Flex formatting.

    Returns True for structured content (headers, lists, multiple sections).
    Returns False for short, simple messages.
    """
    if len(text) < 200 and "\n" not in text:
        return False

    # Has markdown headers
    if re.search(r"^#{1,3}\s+.+", text, re.MULTILINE):
        return True

    # Has bullet lists (3+ items)
    bullets = re.findall(r"^[\s]*[-•*]\s+.+", text, re.MULTILINE)
    if len(bullets) >= 3:
        return True

    # Has numbered lists (3+ items)
    numbered = re.findall(r"^[\s]*\d+[.)]\s+.+", text, re.MULTILINE)
    if len(numbered) >= 3:
        return True

    # Multiple sections separated by double newlines with substantial content
    sections = [s.strip() for s in text.split("\n\n") if s.strip()]
    if len(sections) >= 4 and len(text) > 500:
        return True

    return False


# ── Parsing ──────────────────────────────────────────────────────────────────

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
        if header_match:
            # Save previous section
            if current_lines or current_title:
                sections.append({
                    "title": current_title,
                    "content": "\n".join(current_lines).strip(),
                })
            current_title = header_match.group(1).strip()
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
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1: \2", text)
    return text


def _parse_list_items(content: str) -> list[str]:
    """Extract bullet or numbered list items from content."""
    items = re.findall(r"^[\s]*(?:[-•*]|\d+[.)])\s+(.+)", content, re.MULTILINE)
    return [_strip_md_inline(item.strip()) for item in items]


# ── Flex Builders ────────────────────────────────────────────────────────────

def _text_component(
    text: str,
    *,
    size: str = "sm",
    color: str = "#555555",
    weight: str = "regular",
    wrap: bool = True,
    margin: str | None = None,
) -> dict:
    """Create a Flex text component."""
    comp: dict[str, Any] = {
        "type": "text",
        "text": text[:2000],  # LINE text component limit
        "size": size,
        "color": color,
        "weight": weight,
        "wrap": wrap,
    }
    if margin:
        comp["margin"] = margin
    return comp


def _separator(margin: str = "lg") -> dict:
    return {"type": "separator", "margin": margin}


def _section_box(title: str | None, content: str) -> list[dict]:
    """Build Flex components for a single section."""
    components: list[dict] = []

    if title:
        components.append(_text_component(
            _strip_md_inline(title),
            size="md",
            color="#1a1a1a",
            weight="bold",
        ))

    if not content:
        return components

    # Check for list items in content
    list_items = _parse_list_items(content)
    if list_items:
        # Non-list content before the list
        non_list = re.sub(
            r"^[\s]*(?:[-•*]|\d+[.)])\s+.+\n?", "", content, flags=re.MULTILINE
        ).strip()
        if non_list:
            components.append(_text_component(
                _strip_md_inline(non_list),
                margin="sm",
            ))

        for item in list_items:
            components.append({
                "type": "box",
                "layout": "horizontal",
                "margin": "sm",
                "contents": [
                    _text_component("•", size="sm", color="#888888", wrap=False),
                    _text_component(item, size="sm", color="#555555", margin="sm"),
                ],
            })
    else:
        components.append(_text_component(
            _strip_md_inline(content),
            margin="sm" if title else None,
        ))

    return components


def build_flex_bubble(text: str, alt_text: str = "Message from your assistant") -> dict:
    """Build a single Flex bubble from structured text.

    Returns a LINE Flex message object ready for the Push/Reply API.
    """
    sections = _parse_sections(text)

    if not sections:
        # Fallback: treat entire text as one section
        sections = [{"title": None, "content": _strip_md_inline(text)}]

    body_contents: list[dict] = []

    for i, section in enumerate(sections):
        if i > 0:
            body_contents.append(_separator())

        components = _section_box(section.get("title"), section.get("content", ""))
        body_contents.extend(components)

    # Trim to avoid oversized Flex (LINE has ~50KB limit per message)
    # Keep max 20 components in body
    if len(body_contents) > 30:
        body_contents = body_contents[:30]

    bubble: dict = {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "md",
            "contents": body_contents,
        },
    }

    return {
        "type": "flex",
        "altText": alt_text[:400],  # LINE altText limit
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
                color="#1a1a1a",
                weight="bold",
            ))
        if item.get("content"):
            body_contents.append(_text_component(
                item["content"],
                size="sm",
                color="#555555",
                margin="sm",
            ))

        bubble: dict[str, Any] = {
            "type": "bubble",
            "size": "kilo",
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
                    "color": "#06C755",
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


# ── Quick Reply ──────────────────────────────────────────────────────────────

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
