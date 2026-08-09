"""Additional tenant service coverage."""

from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.journal.models import Document
from apps.journal.services import seed_default_documents_for_tenant
from apps.orchestrator.services import _stale_provisioning_tenants_queryset

from .models import Tenant, User
from .services import create_tenant, kickoff_tenant_provisioning, prepare_tenant_provisioning


class TenantServiceTest(TestCase):
    def test_duplicate_chat_id_raises_value_error(self):
        create_tenant(display_name="First", telegram_chat_id=1001)

        with self.assertRaises(ValueError):
            create_tenant(display_name="Second", telegram_chat_id=1001)

    def test_create_tenant_seeds_starter_documents(self):
        tenant = create_tenant(display_name="First", telegram_chat_id=2001)

        seeded = Document.objects.filter(
            tenant=tenant,
            kind__in=["tasks", "goal", "ideas", "memory"],
            slug__in=["tasks", "goals", "ideas", "memory"],
        )
        self.assertEqual(seeded.count(), 4)

        tasks = Document.objects.get(tenant=tenant, kind="tasks", slug="tasks")
        goals = Document.objects.get(tenant=tenant, kind="goal", slug="goals")
        ideas = Document.objects.get(tenant=tenant, kind="ideas", slug="ideas")
        memory = Document.objects.get(tenant=tenant, kind="memory", slug="memory")

        self.assertIn("# Tasks", tasks.markdown)
        self.assertIn("## What to work on", tasks.markdown)
        self.assertIn("# Goals", goals.markdown)
        self.assertIn("## Active", goals.markdown)
        self.assertIn("# Ideas", ideas.markdown)
        self.assertIn("# Memory", memory.markdown)
        self.assertIn("long-term memory", memory.markdown.lower())

    def test_seed_default_documents_is_idempotent(self):
        tenant = create_tenant(display_name="First", telegram_chat_id=3001)

        original = Document.objects.get(tenant=tenant, kind="tasks", slug="tasks")
        original.markdown = "CUSTOM TASKS CONTENT"
        original.save(update_fields=["markdown"])

        result = seed_default_documents_for_tenant(tenant=tenant)
        reseeded = Document.objects.get(tenant=tenant, kind="tasks", slug="tasks")

        self.assertFalse(result["created"]["tasks"])
        self.assertEqual(reseeded.markdown, "CUSTOM TASKS CONTENT")


class DeferredProvisioningRepairTest(TestCase):
    def test_never_published_provisioning_tenant_is_in_repair_sweep(self):
        user = User.objects.create_user(
            username="never-published@example.com",
            email="never-published@example.com",
        )
        tenant, created = prepare_tenant_provisioning(user)

        self.assertTrue(created)
        self.assertEqual(tenant.status, Tenant.Status.PROVISIONING)
        self.assertEqual(tenant.container_id, "")
        self.assertEqual(tenant.container_fqdn, "")
        self.assertTrue(_stale_provisioning_tenants_queryset().filter(id=tenant.id).exists())

    @override_settings(QSTASH_TOKEN="configured", NBHD_DISABLE_BACKGROUND_THREADS=False)
    @patch("apps.tenants.services.threading.Thread", side_effect=RuntimeError("thread unavailable"))
    def test_kickoff_spawn_failure_is_contained_and_marks_pending(self, _thread):
        user = User.objects.create_user(
            username="kickoff-failure@example.com",
            email="kickoff-failure@example.com",
        )
        tenant, _ = prepare_tenant_provisioning(user)

        with self.assertLogs("apps.tenants.services", level="ERROR"):
            kicked_off = kickoff_tenant_provisioning(str(tenant.id), str(user.id))

        self.assertFalse(kicked_off)
        tenant.refresh_from_db()
        self.assertEqual(tenant.status, Tenant.Status.PENDING)
