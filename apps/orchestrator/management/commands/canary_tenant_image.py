"""Deploy an OpenClaw image to a single tenant for canary testing.

Wraps `apps.orchestrator.azure_client.update_container_image` so a single
tenant can be flipped to a custom image tag (typically `canary-<shortsha>`)
without touching `Tenant.container_image_tag` in the DB.

Why we do NOT update the DB tag:

  When the canary PR is merged, the regular CI/CD path bumps every tenant's
  pending config to point at the new full-SHA / `latest` tag, and the
  `apply-pending-configs` cron rolls each tenant onto it. Leaving the DB
  pointing at the previous known-good tag means the canary container will
  cleanly revert to the canonical image on the next normal apply — no
  manual cleanup, no lingering `canary-*` tag in the DB.

Usage:

    python manage.py canary_tenant_image \\
        --container oc-148ccf1c-ef13-47f8-a \\
        --tag canary-abc1234

See `docs/runbooks/canary.md` for the full procedure.
"""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.orchestrator.azure_client import get_container_client, is_mock, update_container_image


class Command(BaseCommand):
    help = "Deploy a custom OpenClaw image tag to a single tenant (canary; does not update DB tag)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--container",
            required=True,
            help="Container App name (e.g. oc-148ccf1c-ef13-47f8-a)",
        )
        parser.add_argument(
            "--tag",
            required=True,
            help="Image tag to deploy (e.g. canary-abc1234)",
        )
        parser.add_argument(
            "--repository",
            default="nbhd-openclaw",
            help="ACR repository name (default: nbhd-openclaw)",
        )

    def handle(self, *args, **options):
        if is_mock():
            raise CommandError(
                "Azure client is mocked (AZURE_MOCK=true) — running this command here "
                'would print "Canary image deployed" without touching Azure at all. '
                "This almost shipped a phantom canary. Run it from a shell/environment "
                "where AZURE_MOCK is unset or false (production has real Azure creds; "
                "local dev .env sets AZURE_MOCK=true on purpose) to actually deploy."
            )

        container = options["container"]
        tag = options["tag"]
        repository = options["repository"]

        registry = getattr(settings, "AZURE_ACR_SERVER", None)
        if not registry:
            raise CommandError("AZURE_ACR_SERVER is not configured")

        image = f"{registry}/{repository}:{tag}"

        self.stdout.write(f"Deploying canary image to {container}")
        self.stdout.write(f"  image:  {image}")
        self.stdout.write("  db tag: (unchanged — canary)")

        try:
            update_container_image(container, image)
        except Exception as exc:
            raise CommandError(f"update_container_image failed: {exc}") from exc

        actual_image = self._read_deployed_image(container)
        if actual_image != image:
            raise CommandError(
                f"Deploy did not take: requested {image!r}, but {container}'s openclaw "
                f"container reads back as {actual_image!r} on Azure. Do not assume this "
                "canary is live — check the Container App revision directly."
            )

        self.stdout.write(self.style.SUCCESS(f"Canary image deployed to {container}"))
        self.stdout.write(
            "Tenant.container_image_tag NOT updated. The next normal "
            "apply-pending-configs run will reconcile this container back "
            "to the canonical fleet tag."
        )

    def _read_deployed_image(self, container: str) -> str | None:
        """Read back the live openclaw container image from Azure.

        `update_container_image` blocks on the create-or-update poller
        before returning, so the template `container_apps.get` returns
        here already reflects the new image — no polling loop needed.
        """
        client = get_container_client()
        app = client.container_apps.get(settings.AZURE_RESOURCE_GROUP, container)
        for tpl_container in app.template.containers:
            if tpl_container.name == "openclaw":
                return tpl_container.image
        return None
