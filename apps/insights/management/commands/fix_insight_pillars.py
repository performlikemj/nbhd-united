"""Inspect and reassign the pillar of existing AssistantInsight rows.

Before reply markers carried an optional ``<pillar>/`` prefix, every insight
recorded through the generic chat paths (Telegram poller / webhook, LINE) was
filed under a hard-coded default pillar — so a handful of production rows are
mislabeled. This command is the manual repair tool for those ~6 rows.

List mode (no args) prints every insight with its current pillar + topic so an
operator can eyeball which are wrong::

    manage.py fix_insight_pillars
    manage.py fix_insight_pillars --pillar gravity --limit 50

Reassign mode takes one or more ``<insight_id>=<pillar>`` pairs. Each row's
pillar is changed and its topic is re-resolved under the new pillar (the same
slug is looked up / auto-proposed there), so pillar and topic stay consistent::

    manage.py fix_insight_pillars 3f2b...=journal 9a1c...=fuel

Idempotent: reassigning a row to the pillar it already has is a no-op.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.insights.models import AssistantInsight
from apps.insights.pillars import Pillar
from apps.insights.topic_resolver import resolve_topic

_VALID_PILLARS = set(Pillar.values)


class Command(BaseCommand):
    help = "List insights or reassign their pillar (id=pillar pairs). Manual ops tool."

    def add_arguments(self, parser):
        parser.add_argument(
            "assignments",
            nargs="*",
            help="Zero or more <insight_id>=<pillar> reassignments. Omit to list.",
        )
        parser.add_argument(
            "--pillar",
            default="",
            help="List mode: only show insights currently in this pillar.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=200,
            help="List mode: max rows to print (default 200).",
        )

    def handle(self, *args, **options):
        assignments = options["assignments"]
        if not assignments:
            self._list(pillar=options["pillar"].strip().lower(), limit=options["limit"])
            return
        self._reassign(assignments)

    # ── list ────────────────────────────────────────────────────────────
    def _list(self, *, pillar: str, limit: int) -> None:
        qs = AssistantInsight.objects.select_related("topic", "tenant").order_by("pillar", "-created_at")
        if pillar:
            if pillar not in _VALID_PILLARS:
                raise CommandError(f"unknown pillar {pillar!r}; valid: {sorted(_VALID_PILLARS)}")
            qs = qs.filter(pillar=pillar)

        rows = list(qs[:limit])
        if not rows:
            self.stdout.write("(no insights)")
            return

        self.stdout.write(f"{len(rows)} insight(s):")
        for ins in rows:
            slug = ins.topic.slug if ins.topic_id else "?"
            statement = (ins.statement or "").replace("\n", " ")
            if len(statement) > 70:
                statement = statement[:67] + "..."
            self.stdout.write(
                f"  {ins.id}  [{ins.pillar}/{slug}]  ({ins.status})  tenant={str(ins.tenant_id)[:8]}  {statement}"
            )
        self.stdout.write("\nReassign with: manage.py fix_insight_pillars <id>=<pillar> ...")

    # ── reassign ────────────────────────────────────────────────────────
    def _reassign(self, assignments: list[str]) -> None:
        parsed: list[tuple[str, str]] = []
        for raw in assignments:
            if "=" not in raw:
                raise CommandError(f"bad assignment {raw!r}; expected <insight_id>=<pillar>")
            ins_id, _, new_pillar = raw.partition("=")
            ins_id = ins_id.strip()
            new_pillar = new_pillar.strip().lower()
            if not ins_id or new_pillar not in _VALID_PILLARS:
                raise CommandError(f"bad assignment {raw!r}; pillar must be one of {sorted(_VALID_PILLARS)}")
            parsed.append((ins_id, new_pillar))

        changed = 0
        skipped = 0
        for ins_id, new_pillar in parsed:
            try:
                ins = AssistantInsight.objects.select_related("topic").get(id=ins_id)
            except AssistantInsight.DoesNotExist:
                raise CommandError(f"insight {ins_id} not found")

            if ins.pillar == new_pillar:
                self.stdout.write(f"  {ins_id}: already {new_pillar} — skipped")
                skipped += 1
                continue

            old_slug = ins.topic.slug if ins.topic_id else ""
            with transaction.atomic():
                # Re-point the topic to the same slug under the new pillar so the
                # (pillar, slug) pairing stays coherent. resolve_topic creates a
                # proposed row there if the slug doesn't exist yet.
                new_topic = resolve_topic(new_pillar, old_slug or "untitled")
                old_pillar = ins.pillar
                ins.pillar = new_pillar
                ins.topic = new_topic
                ins.save(update_fields=["pillar", "topic"])
            self.stdout.write(f"  {ins_id}: {old_pillar}/{old_slug} -> {new_pillar}/{new_topic.slug}")
            changed += 1

        self.stdout.write(self.style.SUCCESS(f"Reassigned {changed}, skipped {skipped}."))
