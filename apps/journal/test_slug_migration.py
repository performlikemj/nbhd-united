"""Regression tests for the fleet-wide journal slug sanitizer migration."""

from __future__ import annotations

from datetime import timedelta

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

from apps.tenants.models import Tenant, User


class SanitizeDocumentSlugsMigrationTest(TransactionTestCase):
    migrate_from = ("journal", "0029_notetemplate_pii_receipts_session_pii_receipts")
    migrate_to = ("journal", "0030_sanitize_document_slugs")

    def setUp(self):
        super().setUp()
        user = User.objects.create(username="slug-migration-owner")
        tenant = Tenant.objects.create(user=user)
        self.tenant_id = tenant.pk

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old_apps = executor.loader.project_state([self.migrate_from]).apps
        Document = old_apps.get_model("journal", "Document")

        documents = {
            "collision": Document.objects.create(
                tenant_id=self.tenant_id,
                kind="weekly",
                slug="weekly-review",
                title="Collision",
            ),
            "underscore": Document.objects.create(
                tenant_id=self.tenant_id,
                kind="weekly",
                slug="weekly_review",
                title="Underscore",
            ),
            "dot": Document.objects.create(
                tenant_id=self.tenant_id,
                kind="project",
                slug="project.alpha",
                title="Dot",
            ),
            "leading_underscore": Document.objects.create(
                tenant_id=self.tenant_id,
                kind="ideas",
                slug="_leading",
                title="Leading underscore",
            ),
            "leading_star": Document.objects.create(
                tenant_id=self.tenant_id,
                kind="memory",
                slug="*leading",
                title="Leading star",
            ),
            "fallback": Document.objects.create(
                tenant_id=self.tenant_id,
                kind="monthly",
                slug="***",
                title="Fallback",
            ),
            "valid": Document.objects.create(
                tenant_id=self.tenant_id,
                kind="weekly",
                slug="2026-W32",
                title="Valid",
            ),
        }
        self.document_ids = {name: document.pk for name, document in documents.items()}
        self.fallback_slug = f"doc-{documents['fallback'].pk.hex[:8]}"

        preserved_time = timezone.now() - timedelta(days=30)
        Document.objects.filter(pk__in=self.document_ids.values()).update(updated_at=preserved_time)
        self.preserved_time = preserved_time

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_sanitizes_invalid_slugs_without_touching_recency(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        new_apps = executor.loader.project_state([self.migrate_to]).apps
        Document = new_apps.get_model("journal", "Document")

        expected_slugs = {
            "collision": "weekly-review",
            "underscore": "weekly-review-2",
            "dot": "project-alpha",
            "leading_underscore": "leading",
            "leading_star": "leading",
            "fallback": self.fallback_slug,
            "valid": "2026-W32",
        }
        for name, expected_slug in expected_slugs.items():
            document = Document.objects.get(pk=self.document_ids[name])
            self.assertEqual(document.slug, expected_slug)
            self.assertEqual(document.updated_at, self.preserved_time)
