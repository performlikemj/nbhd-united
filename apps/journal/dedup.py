"""Server-side duplicate detection for typed Task/Goal creation.

The agent re-derives items from journal prose on background/cron turns. Its
*in-turn* dedup only ever inspects the user's **open** items, so a task the
user already completed earlier the same day is invisible to it — and when the
day's narration (an errand nudge, a reminder line) still mentions that item,
the agent recreates it as a brand-new open task with a tidied-up title. The
morning briefing then reports the fresh copy as overdue, the nudge fires
again, and the loop never ends.

Canary 2026-06-07 reproduction: "Fill out customs clearance paperwork for
Jamaica shipments" was completed at 12:51; the 02:00-JST "Background Tasks"
maintenance turn recreated it as open "Customs clearance paperwork" at 17:04.
Same night: "Book hotel for cousin's wedding in March (Jamaica)" (done) →
"Book hotel for Jamaica wedding" (open).

This module is the deterministic backstop that prompt rules could not provide:
it normalizes a proposed title and matches it against existing rows —
including ones **recently completed/skipped** — so ``nbhd_task_create`` /
``nbhd_goal_create`` are *idempotent* regardless of which surface calls them
(cron maintenance, chat, nightly extraction). It is applied only on the
agent/runtime path; a user creating a task in the UI is never blocked.

The matcher is intentionally conservative — it fires on exact-normalized
equality, full content-token containment (the canary case: the shorter title's
content words are all present in the longer one), or high token overlap — so a
genuinely new, differently-worded task still gets through. Fuzzy matching is
gated on the *shorter* title having >=3 content tokens; below that only an exact
normalized match counts, so a generic two-word stub ("Call mom", "Pay rent",
"Buy gift") can never swallow a longer, more-specific task ("Call mom's lawyer",
"Pay rent for March", "Buy gift card") — the symmetric failure of the bug above.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

    from apps.tenants.models import Tenant

    from .models import Goal, Task

# How far back a *closed* item (done/skipped/deferred task; achieved/abandoned
# goal) still suppresses a re-create. Open items match at any age. 14 days is
# long enough to cover the resurrection loop (which recreates within hours)
# without permanently blocking a legitimately recurring task.
DEFAULT_CLOSED_WINDOW_DAYS = 14

# Minimum content tokens in the *shorter* title for the fuzzy rules
# (containment / Jaccard) to fire. Below this we demand exact-normalized
# equality, so a generic two-word stub ("Call mom", "Pay rent", "Buy gift")
# can't be swallowed by — or swallow — a longer, more-specific task. 3 keeps
# both real canary pairs (smaller side = 3 and 4 tokens) while killing the
# 2-vs-3-token over-merge class the review surfaced.
_MIN_TOKENS_FOR_FUZZY = 3

# Token-overlap ratio at which two titles are considered the same intent even
# when neither is a strict subset of the other (reworded duplicates).
_JACCARD_THRESHOLD = 0.6

# Generic words stripped before comparison. Deliberately small — particles and
# articles, not domain nouns — so we never strip the words that carry meaning.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "to",
        "for",
        "of",
        "in",
        "on",
        "at",
        "and",
        "or",
        "with",
        "my",
        "your",
        "his",
        "her",
        "their",
        "this",
        "that",
        "from",
        "by",
        "is",
        "are",
        "be",
        "will",
        "out",
        "up",
        "off",
        "re",
        "s",
        "today",
        "tomorrow",
        "please",
        "need",
        "want",
    }
)


def _content_tokens(title: str) -> tuple[frozenset[str], str]:
    """Return ``(content_token_set, normalized_string)`` for a title.

    Normalization: lowercase, non-alphanumerics → spaces, drop stopwords and
    sub-2-char tokens. The normalized string is the sorted content tokens
    joined by spaces — a stable key for exact-match comparison that ignores
    word order and punctuation.
    """
    lowered = title.casefold()
    cleaned = "".join(ch if ch.isalnum() else " " for ch in lowered)
    tokens = frozenset(tok for tok in cleaned.split() if len(tok) >= 2 and tok not in _STOPWORDS)
    normalized = " ".join(sorted(tokens))
    return tokens, normalized


def titles_match(a: str, b: str) -> bool:
    """True if two titles describe the same item (order/punctuation-insensitive).

    Pure and side-effect-free so the matching rules are unit-testable without
    the ORM. Fires on:
      - exact normalized equality (any length), or
      - when the shorter title has ≥3 content tokens:
          - full content-token containment (smaller set ⊆ larger), or
          - token Jaccard ≥ threshold.
    """
    a_tokens, a_norm = _content_tokens(a)
    b_tokens, b_norm = _content_tokens(b)

    # Empty-after-normalization (e.g. all-stopword titles) → compare raw norms.
    if not a_tokens or not b_tokens:
        return a_norm == b_norm and a_norm != ""

    if a_norm == b_norm:
        return True

    smaller, larger = sorted((a_tokens, b_tokens), key=len)
    # Two-word stubs ("Call mom", "Pay rent") only match verbatim — never via the
    # fuzzy rules below, or they'd swallow a longer, more-specific task.
    if len(smaller) < _MIN_TOKENS_FOR_FUZZY:
        return False

    if smaller <= larger:
        return True

    union = a_tokens | b_tokens
    if union and (len(a_tokens & b_tokens) / len(union)) >= _JACCARD_THRESHOLD:
        return True

    return False


def find_duplicate_task(
    tenant: Tenant,
    title: str,
    *,
    now: datetime,
    closed_window_days: int = DEFAULT_CLOSED_WINDOW_DAYS,
) -> Task | None:
    """Return an existing Task that ``title`` duplicates, or ``None``.

    Candidates: every open/in-progress task (any age) plus tasks
    closed (done/skipped/deferred) within ``closed_window_days``. Open
    matches win over closed; within a group the most recently updated wins.
    """
    from django.db.models import Q

    from .models import Task

    if not (title or "").strip():
        return None

    cutoff = now - timedelta(days=closed_window_days)
    open_states = (Task.Status.OPEN, Task.Status.IN_PROGRESS)

    # Full rows (no .only()): the matched row is serialized by the caller, so
    # deferring columns here just trades one scan query for a per-hit reload.
    candidates = list(
        Task.objects.filter(tenant=tenant)
        .filter(Q(status__in=open_states) | Q(updated_at__gte=cutoff))
        .order_by("-updated_at")
    )

    closed_match = None
    for task in candidates:
        if not titles_match(title, task.title):
            continue
        if task.status in open_states:
            return task  # ordered by -updated_at, so this is the freshest open dup
        if closed_match is None:
            closed_match = task
    return closed_match


def find_duplicate_goal(
    tenant: Tenant,
    title: str,
    *,
    now: datetime,
    closed_window_days: int = DEFAULT_CLOSED_WINDOW_DAYS,
) -> Goal | None:
    """Return an existing Goal that ``title`` duplicates, or ``None``.

    Candidates: every active goal (any age) plus goals closed
    (achieved/abandoned/expired) within ``closed_window_days``.
    """
    from django.db.models import Q

    from .models import Goal

    if not (title or "").strip():
        return None

    cutoff = now - timedelta(days=closed_window_days)

    candidates = list(
        Goal.objects.filter(tenant=tenant)
        .filter(Q(status=Goal.Status.ACTIVE) | Q(updated_at__gte=cutoff))
        .order_by("-updated_at")
    )

    closed_match = None
    for goal in candidates:
        if not titles_match(title, goal.title):
            continue
        if goal.status == Goal.Status.ACTIVE:
            return goal
        if closed_match is None:
            closed_match = goal
    return closed_match
