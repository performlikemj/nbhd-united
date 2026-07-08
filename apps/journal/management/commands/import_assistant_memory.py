"""One-off importer: load an OpenClaw assistant's exported memory files into a
tenant's journal ``Document`` rows (the Postgres system-of-record), so the
web/iOS journal UI and ``nbhd_journal_search`` surface the migrated history.

Reads decrypted workspace files from ``--src`` and writes:

    MEMORY.md              -> Document(kind=memory, slug="long-term")
    memory/YYYY-MM-DD.md   -> Document(kind=daily,  slug="YYYY-MM-DD")
    memory/<other>.md      -> Document(kind=ideas,  slug=<sanitized stem>)

(The daily read path filters non-ISO daily slugs — see the NaN-slug fix — so
date-with-suffix notes go to ``ideas`` to stay visible.)

Idempotent: ``update_or_create`` on (tenant, kind, slug). Dry-run by default;
pass ``--apply`` to write. Prints only metadata (kind / slug / char-count),
never file contents, so the import can be driven without exposing the data.

Targets whatever ``DATABASE_URL`` resolves to. It prints the connected DB host
and the resolved tenant first, so you can confirm you are pointed at prod
*before* running with ``--apply``.
"""

import re
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from apps.journal.models import Document
from apps.tenants.models import Tenant

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
NON_SLUG = re.compile(r"[^a-z0-9-]+")


class Command(BaseCommand):
    help = "Import exported OpenClaw memory files into a tenant's journal Documents (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True, help="Owner email of the destination tenant")
        parser.add_argument("--src", required=True, help="Directory holding MEMORY.md + memory/*.md")
        parser.add_argument("--apply", action="store_true", help="Write to the DB (default: dry-run)")

    def handle(self, *args, **opts):
        src = Path(opts["src"]).expanduser()
        if not src.is_dir():
            raise CommandError(f"--src is not a directory: {src}")

        try:
            tenant = Tenant.objects.select_related("user").get(user__email__iexact=opts["email"])
        except Tenant.DoesNotExist as exc:
            raise CommandError(f"No tenant found for email {opts['email']}") from exc

        host = connection.settings_dict.get("HOST", "?")
        self.stdout.write(f"DB host : {host}")
        self.stdout.write(f"Tenant  : {tenant.id} (status={tenant.status}, email={tenant.user.email})")
        self.stdout.write(f"Mode    : {'APPLY' if opts['apply'] else 'DRY-RUN'}")
        self.stdout.write("")

        # Build the plan (path kept so contents are read only at write time).
        plan: list[tuple[str, str, str, int, Path]] = []

        mem = src / "MEMORY.md"
        if mem.is_file():
            n = len(mem.read_text(encoding="utf-8", errors="replace"))
            plan.append(("memory", "long-term", "Long-term memory", n, mem))

        mdir = src / "memory"
        if mdir.is_dir():
            for f in sorted(mdir.glob("*.md")):
                stem = f.stem
                n = len(f.read_text(encoding="utf-8", errors="replace"))
                if ISO_DATE.match(stem):
                    plan.append(("daily", stem, stem, n, f))
                else:
                    slug = (NON_SLUG.sub("-", stem.lower()).strip("-") or "note")[:128]
                    plan.append(("ideas", slug, stem, n, f))

        if not plan:
            raise CommandError(f"Found no MEMORY.md or memory/*.md under {src}")

        written = 0
        for kind, slug, title, n, path in plan:
            self.stdout.write(f"  {kind:7} {slug:26} {n:6d} chars")
            if opts["apply"]:
                Document.objects.update_or_create(
                    tenant=tenant,
                    kind=kind,
                    slug=slug,
                    defaults={"markdown": path.read_text(encoding="utf-8", errors="replace"), "title": title},
                )
                written += 1

        self.stdout.write("")
        if opts["apply"]:
            self.stdout.write(self.style.SUCCESS(f"Wrote {written} documents for tenant {tenant.id}."))
            self.stdout.write(
                "Text search (nbhd_journal_search) + the journal UI work immediately. "
                "Semantic (pgvector) recall fills in once embeddings are backfilled."
            )
        else:
            self.stdout.write(self.style.WARNING(f"DRY-RUN — would write {len(plan)} documents. Re-run with --apply."))
