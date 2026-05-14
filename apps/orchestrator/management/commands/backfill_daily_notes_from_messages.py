"""Replay LINE/Telegram messages into workspace daily notes.

OpenClaw's memory_search and dreaming both read from
``workspace/memory/YYYY-MM-DD.md`` files on the per-tenant file share.
For a tenant whose conversation lives in ``pending_messages.payload``
on the Django side, those daily notes don't exist (we never wrote them
during the period when memory-core was disabled fleet-wide — PR #525).

This command lifts the gap: it walks ``pending_messages`` for a tenant
over a date range, groups messages by local date, and writes / appends
each day's user-side messages into the corresponding daily note. After
that, ``openclaw memory rem-backfill --path /home/node/.openclaw/memory
--stage-short-term`` on the container can replay those notes through
dreaming's grounded-backfill lane and surface durable candidates for
review in ``DREAMS.md``.

Designed for the May 5–13 recovery on canary (MJ's missing weight
entries from "btw. i was 69kg today and 69.4 yesterday" — those weight
statements live in pending_messages.payload but never made it into
journal_document or workspace memory). Use it once per tenant per
window; the file write is upsert-safe (existing daily notes get
appended, not replaced).

Usage:
    python manage.py backfill_daily_notes_from_messages \\
        --tenant 148ccf1c \\
        --start 2026-05-05 \\
        --end 2026-05-13

    # Dry-run (count + preview, no writes):
    python manage.py backfill_daily_notes_from_messages \\
        --tenant 148ccf1c --start 2026-05-05 --end 2026-05-13 --dry-run
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date as date_cls
from datetime import datetime
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand, CommandError

from apps.router.models import PendingMessage
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)


def _resolve_tenant(uuid_prefix: str) -> Tenant:
    """Resolve a tenant by exact UUID or unambiguous prefix."""
    matches = list(Tenant.objects.filter(id__istartswith=uuid_prefix)[:2])
    if not matches:
        raise CommandError(f"No tenant matched {uuid_prefix!r}")
    if len(matches) > 1:
        raise CommandError(f"{uuid_prefix!r} matches more than one tenant — pass a longer prefix")
    return matches[0]


def _user_local_date(created_at: datetime, user_tz: str) -> date_cls:
    """Convert a UTC timestamp to the user's local calendar date."""
    try:
        tz = ZoneInfo(user_tz)
    except Exception:
        tz = ZoneInfo("UTC")
    return created_at.astimezone(tz).date()


def _format_day_block(day: date_cls, messages: list[tuple[datetime, str]], user_tz: str) -> str:
    """Render one day's user messages as a markdown block to append.

    Format::

        ## Replayed conversation — backfilled YYYY-MM-DD

        - HH:MM — message text
        - HH:MM — next message
        ...

    Local HH:MM (not UTC) so the diary entries match the time the user
    actually said it. Multi-line messages are flattened into single
    lines (newlines → spaces) since the daily note context wants
    digestibility, not fidelity to the original LINE bubble structure.
    """
    try:
        tz = ZoneInfo(user_tz)
    except Exception:
        tz = ZoneInfo("UTC")

    today_iso = datetime.now(tz).date().isoformat()
    lines = [
        f"## Replayed conversation — backfilled {today_iso}",
        "",
        (
            "_Conversation messages from this date that pre-dated the "
            "workspace memory engine. Surfaced into the daily note so "
            "dreaming + memory_search can index them._"
        ),
        "",
    ]
    for ts, text in messages:
        local = ts.astimezone(tz)
        flat = " ".join(text.split())  # collapse whitespace including newlines
        lines.append(f"- {local.strftime('%H:%M')} — {flat}")
    lines.append("")
    return "\n".join(lines)


class Command(BaseCommand):
    help = (
        "Replay pending_messages content into workspace daily notes so "
        "OpenClaw memory-core and dreaming can index them."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant",
            type=str,
            required=True,
            help="Tenant UUID (or UUID prefix; must be unambiguous).",
        )
        parser.add_argument(
            "--start",
            type=str,
            required=True,
            help="Start date (inclusive) in YYYY-MM-DD, interpreted in the tenant's local TZ.",
        )
        parser.add_argument(
            "--end",
            type=str,
            required=True,
            help="End date (inclusive) in YYYY-MM-DD, interpreted in the tenant's local TZ.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Count messages per day and print a preview, but don't write to the share.",
        )

    def handle(self, *args, **options):
        from apps.orchestrator.azure_client import (
            download_workspace_file,
            upload_workspace_file,
        )

        tenant = _resolve_tenant(options["tenant"])
        try:
            start = datetime.strptime(options["start"], "%Y-%m-%d").date()
            end = datetime.strptime(options["end"], "%Y-%m-%d").date()
        except ValueError as exc:
            raise CommandError(f"Invalid date: {exc}") from exc
        if end < start:
            raise CommandError("--end must be on or after --start")

        user_tz = str(getattr(tenant.user, "timezone", "") or "UTC")
        try:
            tz = ZoneInfo(user_tz)
        except Exception:
            tz = ZoneInfo("UTC")

        # Convert local-date bounds to UTC moments for the DB query. Use
        # the start of the start day and the end of the end day (local).
        start_local = datetime.combine(start, datetime.min.time(), tzinfo=tz)
        end_local = datetime.combine(end, datetime.max.time(), tzinfo=tz)

        qs = PendingMessage.objects.filter(
            tenant=tenant,
            created_at__gte=start_local,
            created_at__lte=end_local,
        ).order_by("created_at")

        # Group by user-local date. Pull payload.message_text rather
        # than user_text — the latter is truncated to 200 chars at the
        # queue boundary and would lose the tail of any long aside.
        by_day: dict[date_cls, list[tuple[datetime, str]]] = defaultdict(list)
        total = 0
        for msg in qs.iterator():
            text = (msg.payload or {}).get("message_text") or msg.user_text or ""
            text = text.strip()
            if not text:
                continue
            day = _user_local_date(msg.created_at, user_tz)
            by_day[day].append((msg.created_at, text))
            total += 1

        if not by_day:
            self.stdout.write(
                f"No pending_messages in {start.isoformat()}..{end.isoformat()} "
                f"(local TZ {user_tz}) for tenant {str(tenant.id)[:8]}."
            )
            return

        self.stdout.write(
            f"Found {total} messages across {len(by_day)} day(s) for tenant "
            f"{str(tenant.id)[:8]} ({tenant.user.display_name})"
        )

        for day in sorted(by_day.keys()):
            messages = by_day[day]
            block = _format_day_block(day, messages, user_tz)
            note_path = f"workspace/memory/{day.isoformat()}.md"

            if options["dry_run"]:
                self.stdout.write(f"  {day.isoformat()}: {len(messages)} msg")
                self.stdout.write(f"    -> {note_path}")
                preview = block.split("\n", 6)
                for line in preview:
                    self.stdout.write(f"    | {line}")
                continue

            try:
                existing = download_workspace_file(str(tenant.id), note_path) or ""
            except Exception:
                existing = ""

            if existing.strip():
                merged = existing.rstrip() + "\n\n" + block
            else:
                merged = block

            try:
                upload_workspace_file(str(tenant.id), note_path, merged)
                self.stdout.write(
                    f"  {day.isoformat()}: wrote {len(messages)} msg ({len(merged)} chars total) -> {note_path}"
                )
            except Exception:
                logger.exception(
                    "Failed to upload daily note %s for tenant %s",
                    note_path,
                    str(tenant.id)[:8],
                )
                raise CommandError(
                    f"Upload failed for {note_path}. Partial write may have happened "
                    f"for earlier days; check Azure file-share state before re-running."
                )

        if options["dry_run"]:
            self.stdout.write("(dry-run — no writes performed)")
        else:
            self.stdout.write(
                "Done. On canary, run `openclaw memory rem-backfill "
                "--path /home/node/.openclaw/memory --stage-short-term` to "
                "feed these notes into dreaming's grounded-backfill lane."
            )
