"""Migrate ``Document(kind=goal|tasks)`` rows into Goal and Task tables.

Idempotent: re-running skips rows already migrated (Goal/Task carry a
``migrated_from_document`` FK). Best-effort: anything unparseable on the
tasks side is left as a Document for the agent to re-curate later.

Usage:
    python manage.py migrate_documents_to_typed_models           # all tenants
    python manage.py migrate_documents_to_typed_models --tenant <uuid>
    python manage.py migrate_documents_to_typed_models --dry-run

The source Document rows are NOT deleted by this command — only marked-as-
migrated via FK. Run ``prune_migrated_goal_task_documents`` (separate
command, not yet written) to physically remove them after observation.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

TASK_LINE_RE = re.compile(r"^\s*-\s*\[(?P<mark>[ xX])\]\s+(?P<text>.+?)\s*$")
DUE_DATE_RE = re.compile(r"\b(?:by|due)\s+(\d{4}-\d{2}-\d{2})\b", re.IGNORECASE)


class Command(BaseCommand):
    help = "Migrate Document(kind=goal|tasks) into Goal and Task tables."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", default=None, help="Tenant UUID; defaults to all tenants.")
        parser.add_argument("--dry-run", action="store_true", help="Print intended actions without writing.")

    def handle(self, *args, tenant=None, dry_run=False, **kwargs):
        from apps.tenants.models import Tenant

        tenants: Iterable[Tenant] = Tenant.objects.filter(id=tenant) if tenant else Tenant.objects.all()
        for t in tenants:
            self._migrate_tenant(t, dry_run=dry_run)

    def _migrate_tenant(self, tenant, *, dry_run: bool) -> None:
        from apps.journal.models import Document

        goal_docs = Document.objects.filter(tenant=tenant, kind=Document.Kind.GOAL)
        task_docs = Document.objects.filter(tenant=tenant, kind=Document.Kind.TASKS)

        goals_migrated = self._migrate_goals(tenant, goal_docs, dry_run=dry_run)
        tasks_migrated = self._migrate_tasks(tenant, task_docs, dry_run=dry_run)

        self.stdout.write(f"[{str(tenant.id)[:8]}] goals={goals_migrated} tasks={tasks_migrated} dry_run={dry_run}")

    def _migrate_goals(self, tenant, docs, *, dry_run: bool) -> int:
        from apps.journal.models import Goal

        count = 0
        # Dedup by (title-key, pillar). The canary has goal/goal.md +
        # goal/goals.md with overlapping content; keep the most-recently-
        # updated and fold the other into a "Earlier draft" section.
        by_key: dict[tuple, list] = {}
        for doc in docs.order_by("-updated_at"):
            key = (self._goal_key(doc), doc.pillar or "")
            by_key.setdefault(key, []).append(doc)

        for _key, group in by_key.items():
            primary = group[0]
            if Goal.objects.filter(migrated_from_document=primary).exists():
                continue

            description = primary.markdown or ""
            for older in group[1:]:
                older_md = (older.markdown or "").strip()
                if older_md and older_md != (primary.markdown or "").strip():
                    description += f"\n\n## Earlier draft ({older.slug})\n\n{older.markdown or ''}"

            status = self._map_intent_status(primary.intent_status)
            target_date = self._extract_target_date(primary.target)

            if dry_run:
                self.stdout.write(f"  goal: {primary.title or primary.slug} [{status}]")
                count += 1
                continue

            with transaction.atomic():
                Goal.objects.create(
                    tenant=tenant,
                    title=primary.title or primary.slug,
                    description=description,
                    pillar=primary.pillar or "",
                    topic_id=primary.topic_id,
                    target=primary.target,
                    status=status,
                    target_date=target_date,
                    achieved_at=timezone.now() if status == Goal.Status.ACHIEVED else None,
                    migrated_from_document=primary,
                )
            count += 1
        return count

    def _migrate_tasks(self, tenant, docs, *, dry_run: bool) -> int:
        from apps.journal.models import Task

        count = 0
        for doc in docs:
            for line in (doc.markdown or "").splitlines():
                m = TASK_LINE_RE.match(line)
                if not m:
                    continue
                text = m.group("text").strip()
                done = m.group("mark").lower() == "x"

                due = None
                due_match = DUE_DATE_RE.search(text)
                if due_match:
                    try:
                        due = timezone.datetime.strptime(due_match.group(1), "%Y-%m-%d").date()
                    except ValueError:
                        pass

                # Idempotency — skip if a task with same title was already
                # migrated from this Document.
                if Task.objects.filter(tenant=tenant, title=text, migrated_from_document=doc).exists():
                    continue

                if dry_run:
                    self.stdout.write(f"  task: [{'x' if done else ' '}] {text}")
                    count += 1
                    continue

                Task.objects.create(
                    tenant=tenant,
                    title=text,
                    pillar=doc.pillar or "",
                    status=Task.Status.DONE if done else Task.Status.OPEN,
                    due_date=due,
                    completed_at=timezone.now() if done else None,
                    migrated_from_document=doc,
                )
                count += 1
        return count

    @staticmethod
    def _goal_key(doc) -> str:
        base = (doc.title or doc.slug or "").strip().lower()
        # Singularize a trailing 's' to fold "goal" vs "goals" collisions.
        return base.rstrip("s") or "_blank"

    @staticmethod
    def _map_intent_status(intent_status: str | None):
        from apps.journal.models import Goal

        mapping = {
            "active": Goal.Status.ACTIVE,
            "achieved": Goal.Status.ACHIEVED,
            "abandoned": Goal.Status.ABANDONED,
            "expired": Goal.Status.EXPIRED,
        }
        return mapping.get((intent_status or "").lower(), Goal.Status.ACTIVE)

    @staticmethod
    def _extract_target_date(target):
        if isinstance(target, dict) and target.get("target_date"):
            try:
                return timezone.datetime.strptime(str(target["target_date"]), "%Y-%m-%d").date()
            except (ValueError, TypeError):
                return None
        return None
