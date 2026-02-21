"""Additional tenant service coverage."""
from django.test import TestCase

from apps.journal.models import Document
from .services import create_tenant
from apps.journal.services import seed_default_documents_for_tenant


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
        self.assertIn("Short-term goals", goals.markdown)
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
