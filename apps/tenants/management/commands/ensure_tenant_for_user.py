"""Create + provision a tenant for authenticated users who never got one.

Incident remediation for the web-signup -> app PKCE handoff cohort: those users
were authenticated (User row + JWTs) but bypassed the web /onboarding step, so
no Tenant was ever created and every feature tab 404'd. This command backfills
their tenant through the same idempotent ``ensure_tenant_provisioned`` helper
the live signup paths now use — safe to re-run (a user who already has a tenant
is left untouched).

Usage:
    python manage.py ensure_tenant_for_user --email a@example.com --email b@example.com
    python manage.py ensure_tenant_for_user --user-id <uuid>
"""

from django.core.management.base import BaseCommand, CommandError

from apps.tenants.models import User
from apps.tenants.services import ensure_tenant_provisioned


class Command(BaseCommand):
    help = "Create + provision a tenant for users who don't have one yet (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--email", action="append", default=[], help="User email (repeatable)")
        parser.add_argument("--user-id", action="append", default=[], help="User UUID (repeatable)")

    def handle(self, *args, **options):
        emails = options["email"]
        user_ids = options["user_id"]
        if not emails and not user_ids:
            raise CommandError("Provide at least one --email or --user-id")

        users = []
        for email in emails:
            try:
                users.append(User.objects.get(email=email))
            except User.DoesNotExist as exc:
                raise CommandError(f"No user with email {email}") from exc
        for uid in user_ids:
            try:
                users.append(User.objects.get(id=uid))
            except User.DoesNotExist as exc:
                raise CommandError(f"No user with id {uid}") from exc

        for user in users:
            tenant, created, published = ensure_tenant_provisioned(user)
            state = "created" if created else "already-existed"
            prov = "provision-published" if published else "PENDING(publish-failed)"
            self.stdout.write(
                self.style.SUCCESS(f"user={user.email} tenant={tenant.id} status={tenant.status} {state} {prov}")
            )
