"""Shared daily mood persistence for owner check-ins and daily-note sections."""

from __future__ import annotations

import logging
import re
from datetime import date

from django.db import connection, transaction

from apps.common.cache import bump_tag
from apps.tenants.models import Tenant

from .md_utils import _ENERGY_RE, _MOOD_RE
from .models import JournalEntry
from .store_authoring import author_store_fields

logger = logging.getLogger(__name__)


def upsert_mood(
    *,
    tenant: Tenant,
    check_in_date: date,
    score: int,
    feeling: str | None = None,
    writer: str = "owner",
    seam: str = "journal.mood.upsert.owner",
) -> tuple[JournalEntry, bool]:
    """Part 1 store path; omitted feeling preserves the existing mood and receipt."""
    fields = {"energy_score": score, "raw_text": f"Energy {score}/10"}
    if feeling is not None:
        fields["mood"] = feeling
        if feeling:
            fields["raw_text"] += f"; {feeling}"

    # Detection may call out of process: author before taking DB locks.
    authored, receipts = author_store_fields(
        tenant,
        fields,
        model_label="journal.JournalEntry",
        seam=seam,
        writer=writer,
        defer_detection=writer == "runtime",
    )
    with transaction.atomic():
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('app.tenant_id', %s, true), set_config('app.user_id', %s, true)",
                    [str(tenant.pk), str(tenant.user_id)],
                )
        # There is no unique tenant/date constraint on legacy entries.
        # Lock the parent even when today's entry does not exist yet.
        Tenant.objects.select_for_update().get(pk=tenant.pk)
        entry = (
            JournalEntry.objects.select_for_update()
            .filter(tenant=tenant, date=check_in_date)
            .order_by("-created_at", "-pk")
            .first()
        )
        created = entry is None
        if created:
            entry = JournalEntry(tenant=tenant, date=check_in_date)
        elif entry.raw_text:
            # A mood check-in must preserve the owner's existing journal.
            authored.pop("raw_text")
            receipts.pop("raw_text", None)
        for field, value in authored.items():
            setattr(entry, field, value)
        entry.pii_receipts = {**(entry.pii_receipts or {}), **receipts}
        entry.save()

    bump_tag(tenant.pk, "journal")
    bump_tag(tenant.pk, "dashboard")
    return entry, created


def bridge_daily_note_mood(*, tenant: Tenant, note_date: date, content: str, writer: str) -> None:
    """Mirror a parseable energy-mood section without jeopardizing its markdown write."""
    score = None
    feeling = None
    for line in content.splitlines():
        energy_match = _ENERGY_RE.search(line)
        if energy_match and re.fullmatch(r"\s*(?:/\s*10\s*)?(?:\|.*)?", line[energy_match.end() :]):
            # Reject decimals, malformed scales, and out-of-range values rather
            # than treating a numeric prefix (e.g. 7.5) as a score.
            digits = energy_match.group(1)
            if digits in {str(n) for n in range(1, 11)}:
                score = int(digits)
        mood_match = _MOOD_RE.search(line)
        if mood_match:
            feeling = mood_match.group(1).strip()[:255]
    if score is None:
        return
    try:
        upsert_mood(
            tenant=tenant,
            check_in_date=note_date,
            score=score,
            feeling=feeling,
            writer=writer,
            seam=f"journal.daily_note.mood_bridge.{writer}",
        )
    except Exception:
        # The existing daily-note save has already succeeded. An additive
        # mirror failure must not turn that successful write into a tool error.
        logger.warning("Daily-note mood bridge failed for tenant %s on %s", tenant.pk, note_date, exc_info=True)
