"""Markdown → Telegram-HTML renderer.

The assistant emits CommonMark/GFM markdown. Telegram's legacy ``Markdown``
parse-mode renders ``[links]`` but passes ``##`` headings, ``---`` rules,
``**bold**`` and tables through *literally* — so the user sees raw markdown
syntax in their chat (see the "study kit" leak). Telegram's ``HTML`` parse-mode
supports a small, well-defined tag subset that we *can* target precisely.

This module converts markdown into that subset:

  * ``# … ######`` headings        → ``<b>…</b>`` (bold standout line)
  * ``**x** / __x__``              → ``<b>x</b>``
  * ``*x* / _x_``                  → ``<i>x</i>``
  * ``~~x~~``                      → ``<s>x</s>``
  * `` `x` ``                      → ``<code>x</code>``
  * ```` ```lang…``` ````          → ``<pre><code class="language-lang">…</code></pre>``
  * ``[t](u)``                     → ``<a href="u">t</a>``
  * ``> quote``                    → ``<blockquote>…</blockquote>``
  * ``- / * / 1.`` lists           → ``•`` / ``◦`` / numbered, with nesting
  * ``- [ ] / - [x]`` task items   → ``☐`` / ``☑``
  * ``---`` / ``***`` rules        → a clean ``──────────`` separator line
  * GFM tables                     → an *aligned monospace* ``<pre>`` grid

Safety contract (this is the whole point — a malformed tag makes Telegram 400
and fall back to ugly raw-markdown plaintext):

  1. All literal text is HTML-escaped *before* tags are inserted, so user text
     can never inject or break a tag.
  2. Every rendered block is validated for balanced, properly-nested tags.
     A block that fails validation degrades to its *markdown-stripped plaintext*
     form — never raw markdown.
  3. ``markdown_to_plaintext`` is the channel-wide clean fallback: readable text
     with **no** visible markdown for the rare case Telegram still rejects HTML.

Stdlib only — no new dependency.
"""

from __future__ import annotations

import html
import re

__all__ = [
    "render_telegram_html",
    "markdown_to_plaintext",
    "strip_telegram_html",
    "TG_MAX_LEN",
]

TG_MAX_LEN = 4096  # Telegram per-message hard limit.

# Tags we emit and that Telegram's HTML parse-mode accepts. Anything outside
# this set in the rendered output is a bug we refuse to send.
_ALLOWED_TAGS = frozenset(
    {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre", "a", "blockquote"}
)

_PLACEHOLDER = "\x00{}\x00"
_PLACEHOLDER_RE = re.compile(r"\x00(\d+)\x00")


# ───────────────────────── inline ─────────────────────────


def _escape(text: str) -> str:
    """Escape the three characters Telegram HTML treats specially."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _emphasis(escaped: str) -> str:
    """Apply bold / italic / strikethrough on already-escaped text.

    Order matters: bold (``**`` / ``__``) is consumed before italic so a
    leftover single ``*`` / ``_`` is unambiguously italic. The italic guards
    require a non-space immediately inside the delimiters and a word/marker
    boundary outside, so ``2 * 3`` and lone ``*`` never produce a tag.
    """
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped, flags=re.DOTALL)
    escaped = re.sub(r"__(.+?)__", r"<b>\1</b>", escaped, flags=re.DOTALL)
    escaped = re.sub(r"~~(.+?)~~", r"<s>\1</s>", escaped, flags=re.DOTALL)
    # Italic: single * — not part of ** (already gone), not touching a word char
    # outside, not wrapping whitespace inside.
    escaped = re.sub(
        r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])",
        r"<i>\1</i>",
        escaped,
    )
    escaped = re.sub(
        r"(?<![\w_])_(?!\s)([^_\n]+?)(?<!\s)_(?![\w_])",
        r"<i>\1</i>",
        escaped,
    )
    return escaped


def _strip_leftover(text: str) -> str:
    """Clean up markdown that emphasis/link passes left behind.

    Runs on text whose code spans and well-formed links are already stashed as
    placeholders, so it only ever touches *unresolved* markup:

      * malformed/sloppy links ``[label](…`` (spaces in URL, missing paren) →
        degrade to just ``label`` so no ``[..](..)`` is ever shown,
      * unmatched ``**`` / ``~~`` runs (e.g. an unclosed ``**bold``) → removed,
        since a stray ``**`` is never meant literally outside code.

    A lone single ``*`` / ``_`` is left alone (``2 * 3``, ``snake_case``).
    """
    # Degrade leftover/malformed [label](url) to its label — innermost first,
    # repeated so nested labels like [a [b](u) c](u) fully collapse.
    for _ in range(4):
        reduced = re.sub(r"\[([^\[\]\n]*)\]\([^)\n]*\)?", r"\1", text)
        if reduced == text:
            break
        text = reduced
    # Strip any orphan ](url) left by unbalanced / nested link syntax.
    text = re.sub(r"\]\([^)\n]*\)?", "", text)
    # Strip orphaned table-separator runs ('|---|---|', '|:--|--:|') that aren't
    # part of a real GFM table — they appear in prose or quotes otherwise.
    text = re.sub(r"\|(?:\s*:?-{2,}:?\s*\|)+", " ", text)
    # Remove unmatched emphasis runs that survived (e.g. an unclosed **bold).
    text = re.sub(r"\*\*+", "", text)
    text = re.sub(r"~~+", "", text)
    return text


def _inline(text: str) -> str:
    """Convert one run of inline markdown to Telegram HTML.

    Code spans and links are stashed *before* emphasis runs so their contents
    are not re-processed (an URL or code body must stay literal).
    """
    stash: list[str] = []

    def _stash(htm: str) -> str:
        stash.append(htm)
        return _PLACEHOLDER.format(len(stash) - 1)

    escaped = _escape(text)

    # Inline code first — its body is literal (already escaped).
    escaped = re.sub(
        r"`([^`\n]+)`",
        lambda m: _stash(f"<code>{m.group(1)}</code>"),
        escaped,
    )

    # Links: [label](url). Label may carry emphasis; url stays literal.
    def _link(m: re.Match[str]) -> str:
        label = _strip_leftover(_emphasis(m.group(1)))
        url = m.group(2).replace('"', "&quot;")
        return _stash(f'<a href="{url}">{label}</a>')

    escaped = re.sub(r"\[([^\]\n]+)\]\(([^)\s]+)\)", _link, escaped)

    escaped = _emphasis(escaped)
    escaped = _strip_leftover(escaped)

    # Restore stashed code/links (bounds-safe: a forged placeholder index that
    # somehow slipped through input sanitisation resolves to empty, not a crash).
    def _restore(m: re.Match[str]) -> str:
        idx = int(m.group(1))
        return stash[idx] if 0 <= idx < len(stash) else ""

    return _PLACEHOLDER_RE.sub(_restore, escaped)


def _strip_inline(text: str) -> str:
    """Reduce inline markdown to plain text (no tags, no markers)."""
    # A leading heading marker makes no sense inline (e.g. a '## A' table cell).
    text = re.sub(r"^\s*#{1,6}\s+", "", text)
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    text = re.sub(r"\[([^\]\n]+)\]\(([^)\s]+)\)", r"\1", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"__(.+?)__", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"~~(.+?)~~", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])", r"\1", text)
    text = re.sub(r"(?<![\w_])_(?!\s)([^_\n]+?)(?<!\s)_(?![\w_])", r"\1", text)
    return _strip_leftover(text)


# ───────────────────────── HTML validation ─────────────────────────


_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)(\s[^>]*)?>")


def _is_valid_html(fragment: str) -> bool:
    """True iff every tag is allowed and tags are balanced & properly nested.

    A defensive gate: anything that would make Telegram 400 is rejected here so
    the caller can degrade the *block* (not the whole message) to plaintext.
    """
    stack: list[str] = []
    for m in _TAG_RE.finditer(fragment):
        closing, name, _attrs = m.group(1), m.group(2).lower(), m.group(3)
        if name not in _ALLOWED_TAGS:
            return False
        if closing:
            if not stack or stack[-1] != name:
                return False
            stack.pop()
        else:
            stack.append(name)
    return not stack


# ───────────────────────── tables ─────────────────────────


def _split_row(line: str) -> list[str]:
    """Split a ``| a | b |`` markdown row into trimmed cells."""
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    # Honour escaped pipes (\|) inside cells.
    parts = re.split(r"(?<!\\)\|", line)
    return [p.replace("\\|", "|").strip() for p in parts]


def _is_separator_row(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{1,}:?", c.strip() or "-") for c in cells)


def _render_table_grid(header: list[str], rows: list[list[str]]) -> str:
    """Render header + rows as an aligned monospace grid (plain text body)."""
    cols = max([len(header)] + [len(r) for r in rows])

    def cell(cells: list[str], idx: int) -> str:
        return _strip_inline(cells[idx]) if idx < len(cells) else ""

    widths = [0] * cols
    for c in range(cols):
        widths[c] = max(
            len(cell(header, c)),
            *(len(cell(r, c)) for r in rows),
            1,
        )

    def fmt(cells: list[str]) -> str:
        return " │ ".join(cell(cells, c).ljust(widths[c]) for c in range(cols))

    out = [fmt(header), "─┼─".join("─" * widths[c] for c in range(cols))]
    out.extend(fmt(r) for r in rows)
    return "\n".join(line.rstrip() for line in out)


# ───────────────────────── block parser ─────────────────────────


# Heading: any run of leading '#' + text. We don't enforce the CommonMark
# 1–6 cap — 7+ hashes from the agent should still bold, never leak as '#'.
_HEADING_RE = re.compile(r"^\s{0,3}#{1,}\s+(.*?)\s*#*\s*$")
# A line that is only hashes ('######') carries no text — drop it, don't leak.
_HASHES_ONLY_RE = re.compile(r"^\s{0,3}#{1,}\s*$")
_RULE_RE = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*([\w+-]*)\s*$")
_BULLET_RE = re.compile(r"^(\s*)([-*+•])\s+(.*)$")
_ORDERED_RE = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
_QUOTE_RE = re.compile(r"^\s{0,3}>\s?(.*)$")
_TASK_RE = re.compile(r"^\[([ xX])\]\s+(.*)$")

_RULE_LINE = "──────────"


class _Block:
    __slots__ = ("html", "plain")

    def __init__(self, html_str: str, plain: str):
        self.html = html_str
        self.plain = plain


def _list_item(indent: int, marker: str, body: str) -> tuple[str, str]:
    """Render one list item to (html, plain). ``marker`` is ``•`` or ``1.``."""
    pad = "   " * indent
    task = _TASK_RE.match(body)
    if task:
        box = "☑" if task.group(1).lower() == "x" else "☐"
        body = task.group(2)
        bullet = box
    else:
        bullet = marker
    return (
        f"{pad}{bullet} {_inline(body)}",
        f"{pad}{bullet} {_strip_inline(body)}",
    )


def _parse_blocks(text: str) -> list[_Block]:
    """Parse markdown into a list of rendered blocks (html + plain views)."""
    # NUL is our inline-stash sentinel — strip any from input so a user typing
    # a literal \x00…\x00 can't collide with (or forge) a placeholder.
    text = text.replace("\x00", "")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[_Block] = []
    i = 0
    n = len(lines)

    while i < n:
        raw = lines[i]
        stripped = raw.strip()

        # Blank line.
        if not stripped:
            i += 1
            continue

        # Fenced code block.
        fence = _FENCE_RE.match(raw)
        if fence:
            token, lang = fence.group(1), fence.group(2)
            body: list[str] = []
            i += 1
            while i < n and not re.match(rf"^\s{{0,3}}{re.escape(token[0])}{{{len(token)},}}\s*$", lines[i]):
                body.append(lines[i])
                i += 1
            i += 1  # consume closing fence
            code = _escape("\n".join(body))
            cls = f' class="language-{lang}"' if lang else ""
            blocks.append(
                _Block(
                    f"<pre><code{cls}>{code}</code></pre>",
                    "\n".join(body),
                )
            )
            continue

        # A line of only hashes ('######') — no heading text, drop it.
        if _HASHES_ONLY_RE.match(raw):
            i += 1
            continue

        # Horizontal rule.
        if _RULE_RE.match(raw):
            blocks.append(_Block(_RULE_LINE, _RULE_LINE))
            i += 1
            continue

        # GFM table: header row, separator row, then data rows.
        if "|" in raw and i + 1 < n and "|" in lines[i + 1] and _is_separator_row(_split_row(lines[i + 1])):
            header = _split_row(raw)
            i += 2
            rows: list[list[str]] = []
            while i < n and "|" in lines[i] and lines[i].strip():
                cells = _split_row(lines[i])
                if _is_separator_row(cells):
                    i += 1
                    continue
                rows.append(cells)
                i += 1
            grid = _render_table_grid(header, rows)
            blocks.append(_Block(f"<pre>{_escape(grid)}</pre>", grid))
            continue

        # A standalone separator row ('|---|---|') with no header above it is
        # orphaned table syntax — drop it so the dashes never show as text.
        if "|" in raw and _is_separator_row(_split_row(raw)):
            i += 1
            continue

        # Heading.
        heading = _HEADING_RE.match(raw)
        if heading:
            content = heading.group(1)
            if content:
                blocks.append(_Block(f"<b>{_inline(content)}</b>", _strip_inline(content)))
            i += 1
            continue

        # Blockquote (consecutive ``>`` lines).
        if _QUOTE_RE.match(raw):
            quote: list[str] = []
            while i < n and _QUOTE_RE.match(lines[i]):
                quote.append(_QUOTE_RE.match(lines[i]).group(1))
                i += 1
            html_body = "\n".join(_inline(q) for q in quote)
            plain_body = "\n".join(f"┃ {_strip_inline(q)}" for q in quote)
            blocks.append(_Block(f"<blockquote>{html_body}</blockquote>", plain_body))
            continue

        # List (bullet or ordered), possibly nested & multi-item.
        if _BULLET_RE.match(raw) or _ORDERED_RE.match(raw):
            html_items: list[str] = []
            plain_items: list[str] = []
            while i < n:
                bm = _BULLET_RE.match(lines[i])
                om = _ORDERED_RE.match(lines[i])
                if bm:
                    indent = len(bm.group(1)) // 2
                    marker = "•" if indent == 0 else "◦"
                    h, p = _list_item(indent, marker, bm.group(3))
                elif om:
                    indent = len(om.group(1)) // 2
                    marker = f"{om.group(2)}."
                    h, p = _list_item(indent, marker, om.group(3))
                elif lines[i].strip() and lines[i].startswith((" ", "\t")) and html_items:
                    # Continuation line of the previous item.
                    cont = lines[i].strip()
                    html_items[-1] += " " + _inline(cont)
                    plain_items[-1] += " " + _strip_inline(cont)
                    i += 1
                    continue
                else:
                    break
                html_items.append(h)
                plain_items.append(p)
                i += 1
            blocks.append(_Block("\n".join(html_items), "\n".join(plain_items)))
            continue

        # Paragraph: gather until blank line or a structural line.
        para: list[str] = []
        while i < n and lines[i].strip():
            ln = lines[i]
            if (
                _FENCE_RE.match(ln)
                or _RULE_RE.match(ln)
                or _HEADING_RE.match(ln)
                or _HASHES_ONLY_RE.match(ln)
                or _QUOTE_RE.match(ln)
                or _BULLET_RE.match(ln)
                or _ORDERED_RE.match(ln)
            ):
                break
            # Table start inside a paragraph.
            if "|" in ln and i + 1 < n and "|" in lines[i + 1] and _is_separator_row(_split_row(lines[i + 1])):
                break
            para.append(ln.strip())
            i += 1
        joined = "\n".join(para)
        html_para = "\n".join(_inline(p) for p in para)
        plain_para = "\n".join(_strip_inline(p) for p in para)
        # Degrade a block to plaintext if our inline somehow produced bad HTML.
        if not _is_valid_html(html_para):
            html_para = _escape(plain_para)
        blocks.append(_Block(html_para, plain_para))
        # ``joined`` kept for readability; not otherwise used.
        del joined

    # Final safety sweep: any block whose HTML doesn't validate degrades to
    # escaped plaintext. Guarantees we never hand Telegram a broken tag.
    safe: list[_Block] = []
    for b in blocks:
        if _is_valid_html(b.html):
            safe.append(b)
        else:
            safe.append(_Block(_escape(b.plain), b.plain))
    return safe


# ───────────────────────── chunking ─────────────────────────


_PRE_WRAP_RE = re.compile(r"^<pre>(<code(?: [^>]*)?>)?(.*?)(</code>)?</pre>$", re.DOTALL)


def _pack_lines(text: str, budget: int) -> list[str]:
    """Pack ``text`` into ≤budget pieces, hard-splitting any over-long line."""
    if budget < 1:
        budget = 1
    lines: list[str] = []
    for line in text.split("\n"):
        while len(line) > budget:
            lines.append(line[:budget])
            line = line[budget:]
        lines.append(line)
    out: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for line in lines:
        add = len(line) + (1 if cur else 0)
        if cur and cur_len + add > budget:
            out.append("\n".join(cur))
            cur, cur_len = [], 0
            add = len(line)
        cur.append(line)
        cur_len += add
    if cur:
        out.append("\n".join(cur))
    return out or [""]


def _hard_split(fragment: str, max_len: int) -> list[str]:
    """Split a single over-long block into ≤max_len pieces, preserving HTML.

    A ``<pre>`` (optionally ``<pre><code …>``) block is split on its inner
    lines and *each* piece is re-wrapped with the same open/close tags — so a
    huge table or code listing stays a formatted, balanced block across
    messages. Over-long single lines are hard-cut. Plain fragments split on
    line boundaries (tags never span a newline in our output).
    """
    if len(fragment) <= max_len:
        return [fragment]

    pre = _PRE_WRAP_RE.match(fragment)
    if pre:
        open_code, inner, close_code = pre.group(1) or "", pre.group(2), pre.group(3) or ""
        open_w = f"<pre>{open_code}"
        close_w = f"{close_code}</pre>"
        budget = max_len - len(open_w) - len(close_w)
        return [f"{open_w}{piece}{close_w}" for piece in _pack_lines(inner, budget)]

    # Plain fragment — split on line boundaries (our tags never cross '\n').
    return _pack_lines(fragment, max_len)


def _pack(blocks: list[str], max_len: int) -> list[str]:
    """Pack rendered block strings into ≤max_len messages on block boundaries."""
    chunks: list[str] = []
    cur = ""
    for block in blocks:
        for piece in _hard_split(block, max_len):
            if not cur:
                cur = piece
            elif len(cur) + 2 + len(piece) <= max_len:
                cur += "\n\n" + piece
            else:
                chunks.append(cur)
                cur = piece
    if cur:
        chunks.append(cur)
    return chunks


# ───────────────────────── public API ─────────────────────────


def render_telegram_html(text: str, *, max_len: int = TG_MAX_LEN) -> list[str]:
    """Convert markdown ``text`` into a list of Telegram-HTML message chunks.

    Each chunk is ≤ ``max_len`` chars, contains only Telegram-supported tags,
    and is split on block boundaries (never inside a tag or ``<pre>`` block).
    Send each with ``parse_mode="HTML"``. Returns ``[]`` for empty input.
    """
    if not text or not text.strip():
        return []
    blocks = _parse_blocks(text)
    rendered = [b.html for b in blocks if b.html.strip()]
    chunks = _pack(rendered, max_len)

    # Backstop: guarantee every returned chunk is valid Telegram HTML and within
    # the length limit, no matter what splitting did. A chunk that somehow isn't
    # degrades to escaped, tag-free text (re-split if needed) — still no markdown.
    safe: list[str] = []
    for c in chunks:
        if len(c) <= max_len and _is_valid_html(c):
            safe.append(c)
        else:
            plain = _escape(strip_telegram_html(c))
            safe.extend(_pack_lines(plain, max_len))
    return [c for c in safe if c.strip()]


def strip_telegram_html(fragment: str) -> str:
    """Strip tags from a rendered Telegram-HTML chunk back to clean text.

    Used as a per-chunk fallback: if Telegram still rejects a chunk's HTML
    (it shouldn't — every block is validated), send the tag-free, entity-
    decoded text instead. Tables keep their monospace grid; nothing leaks as
    raw markdown because the chunk was already rendered, not raw markdown.
    """
    return html.unescape(re.sub(r"<[^>]+>", "", fragment))


def markdown_to_plaintext(text: str, *, max_len: int | None = None) -> str:
    """Reduce markdown to clean, readable plain text — **no** visible markdown.

    The channel-wide safe fallback: headings lose ``#``, emphasis markers are
    stripped, tables become aligned grids, rules become a separator line,
    bullets keep a ``•``. Used when a channel can't take HTML (or rejects it).
    """
    if not text:
        return ""
    blocks = _parse_blocks(text)
    out = "\n\n".join(b.plain for b in blocks if b.plain.strip())
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    if max_len is not None and len(out) > max_len:
        out = out[:max_len].rstrip()
    return out
