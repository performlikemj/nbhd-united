"""Inbound-event idempotency gate shared by every message channel.

LINE webhook, the Telegram webhook, and the Telegram poller all deliver
*at least once*. ``claim_inbound_event`` is the single chokepoint each
one calls — as early as possible, before any user-visible side effect —
to decide whether this is the first sighting of a provider event
(process it) or a redelivery / poller-restart replay (skip it).

Design notes:

* **Race-safe.** The claim is a ``get_or_create`` on a UNIQUE column,
  so the LINE webhook's per-event daemon threads and overlapping
  redeliveries can't both win. ``get_or_create`` swallows the
  ``IntegrityError`` from the loser and re-reads, so ``created`` is
  authoritative; we additionally catch ``IntegrityError`` defensively.

* **Fail-open.** A dedupe-store outage must never drop a real user
  message. Any unexpected error (or a missing/blank event id) returns
  ``True`` — process the message. The worst case degrades to today's
  behaviour (a possible duplicate), never to a lost message.

* **Self-pruning.** A small fraction of successful claims also delete a
  bounded batch of rows older than the retention window. This keeps the
  ledger from growing forever without standing up a new QStash cron
  (the project has no Celery; adding cron infra for a janitor is more
  surface than the bug warrants).
"""

from __future__ import annotations

import logging
import random
from datetime import timedelta

from django.db import IntegrityError
from django.utils import timezone

logger = logging.getLogger(__name__)

# How long a claimed event id is remembered. Comfortably longer than any
# provider's redelivery window (LINE retries for minutes; a Telegram
# poller restart replays only the unacked tail) while keeping the table
# tiny at this fleet's message volume.
_RETENTION = timedelta(days=3)

# Probability that a given successful claim also runs a prune pass, and
# the row cap per pass. Tuned so pruning amortises to roughly "once per
# ~100 inbound events" with a hard ceiling on the DELETE cost.
_PRUNE_PROBABILITY = 0.01
_PRUNE_BATCH = 500


def _maybe_prune() -> None:
    """Opportunistically delete a bounded batch of expired ledger rows."""
    if random.random() >= _PRUNE_PROBABILITY:
        return
    try:
        from apps.router.models import ProcessedInboundEvent

        cutoff = timezone.now() - _RETENTION
        stale_ids = list(
            ProcessedInboundEvent.objects.filter(created_at__lt=cutoff).values_list("id", flat=True)[:_PRUNE_BATCH]
        )
        if stale_ids:
            ProcessedInboundEvent.objects.filter(id__in=stale_ids).delete()
            logger.info("inbound_dedup: pruned %d expired ledger rows", len(stale_ids))
    except Exception:
        # Pruning is housekeeping — never let it affect message handling.
        logger.exception("inbound_dedup: prune pass failed (non-fatal)")


def claim_inbound_event(event_key: str | None) -> bool:
    """Claim a provider event id for first-time processing.

    Returns ``True`` if the caller should process the event (first
    sighting, blank/unknown id, or dedupe-store error — fail open), and
    ``False`` only when this exact event id has already been claimed and
    the caller must skip it to avoid a duplicate reply.
    """
    if not event_key:
        # No stable id to dedupe on — process rather than risk dropping.
        return True

    try:
        from apps.router.models import ProcessedInboundEvent

        _, created = ProcessedInboundEvent.objects.get_or_create(event_key=event_key)
    except IntegrityError:
        # Lost a concurrent race for the same id → someone else owns it.
        logger.info("inbound_dedup: duplicate event %s (race)", event_key)
        return False
    except Exception:
        # Dedupe store is unreachable/broken — fail open so we never
        # silently swallow a real user message.
        logger.exception(
            "inbound_dedup: claim failed for %s — processing (fail-open)",
            event_key,
        )
        return True

    if not created:
        logger.info("inbound_dedup: duplicate event %s — skipping", event_key)
        return False

    _maybe_prune()
    return True
