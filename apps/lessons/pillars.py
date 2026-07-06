"""Pure lesson→pillar inference.

Shared by three callers so the classification never drifts between them:
  * runtime lesson creation (``Lesson.save`` auto-fills a blank pillar),
  * the friends share-block (``apps.friends.services``), and
  * the one-time backfill migration (``lessons.0005``).

Deliberately dependency-light — no model instances, no app-registry access — so
a data migration can import it safely. The tag markers stay broad on purpose:
over-classifying a lesson as ``gravity`` (finance) or ``core`` (mindfulness) is
the SAFE direction, because those two pillars refuse to share to the
Neighborhood (design §4.7).
"""

from __future__ import annotations

from apps.insights.pillars import Pillar

GRAVITY_MARKERS = frozenset({"gravity", "finance", "money", "debt", "budget", "savings", "loan", "salary", "invoice"})
CORE_MARKERS = frozenset({"core", "mindfulness", "meditation", "mental-health", "mental health", "therapy"})


def infer_pillar_from_tags(tags) -> str:
    """Best-effort pillar from a lesson's tags: ``gravity`` / ``core`` /
    ``lessons`` (the neutral default when nothing marks it)."""
    tagset = {str(t).strip().lower() for t in (tags or [])}
    if tagset & GRAVITY_MARKERS:
        return Pillar.GRAVITY.value
    if tagset & CORE_MARKERS:
        return Pillar.CORE.value
    return Pillar.LESSONS.value
