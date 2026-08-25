from __future__ import annotations

import ast
import threading
from pathlib import Path
from time import monotonic
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from apps.journal.models import Document, NoteTemplate
from apps.lessons.models import Lesson
from apps.tenants.models import Tenant, User
from apps.tenants.test_utils import seed_internal_key

_AUTHORING_CALLS = frozenset({"author_json_paths", "author_store_fields", "author_text"})
_AUTHORING_PLUMBING = frozenset(
    {
        Path("apps/pii/authoring.py"),
        Path("apps/pii/store_authoring.py"),
    }
)


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((keyword.value for keyword in call.keywords if keyword.arg == name), None)


def _is_runtime_capable_writer(node: ast.expr) -> bool:
    """Treat every non-literal provenance as capable of resolving to runtime.

    Only a literal ``owner`` or ``background`` is provably synchronous-only at
    the call site. Names, conditionals, context lookups, and other expressions
    must therefore opt into detector deferral explicitly.
    """
    return not (isinstance(node, ast.Constant) and node.value in {"owner", "background"})


def _runtime_capable_authoring_calls():
    root = Path(__file__).resolve().parents[2]
    for path in sorted((root / "apps").rglob("*.py")):
        relative = path.relative_to(root)
        if relative in _AUTHORING_PLUMBING:
            continue
        if path.name.startswith("test") or "tests" in relative.parts or "migrations" in relative.parts:
            continue
        tree = ast.parse(path.read_text(), filename=str(relative))
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            if _call_name(call) == "get_or_create_authored_document" and path.name == "runtime_views.py":
                yield "helper", relative, call, ast.Constant(value="runtime")
                continue
            if _call_name(call) not in _AUTHORING_CALLS:
                continue
            writer = _keyword(call, "writer")
            if writer is None:
                continue
            if _is_runtime_capable_writer(writer):
                yield "direct", relative, call, writer


class RuntimeWriterDeferralInventoryTests(SimpleTestCase):
    def test_every_runtime_capable_authoring_call_explicitly_defers_detection(self):
        sites = list(_runtime_capable_authoring_calls())
        direct_sites = [site for site in sites if site[0] == "direct"]
        helper_sites = [site for site in sites if site[0] == "helper"]
        self.assertEqual(len(direct_sites), 35, "runtime authoring direct-call inventory changed")
        self.assertEqual(len(helper_sites), 5, "runtime Document helper inventory changed")

        for _kind, path, call, writer in sites:
            with self.subTest(path=str(path), line=call.lineno, call=_call_name(call)):
                deferred = _keyword(call, "defer_detection")
                self.assertIsNotNone(
                    deferred,
                    f"{path}:{call.lineno} can author as writer='runtime' without explicit detector deferral",
                )
                if isinstance(writer, ast.Constant) and writer.value == "runtime":
                    self.assertIsInstance(deferred, ast.Constant)
                    self.assertIs(deferred.value, True)
                elif isinstance(deferred, ast.Constant):
                    self.assertIsNot(
                        deferred.value,
                        False,
                        f"{path}:{call.lineno} disables deferral for a runtime-capable writer",
                    )


@override_settings(NBHD_INTERNAL_API_KEY="shared-key")
class RuntimeWriterBlockingDetectorTests(TestCase):
    def setUp(self):
        user = User.objects.create_user(username=f"runtime-deferral-{id(self)}", password="x")
        self.tenant = Tenant.objects.create(
            user=user,
            status=Tenant.Status.ACTIVE,
            layer1_placeholder_writes=True,
            pii_entity_map={"[PERSON_1]": {"name": "Alice"}},
        )
        seed_internal_key(self.tenant, key="shared-key")
        self.client = APIClient()
        self.headers = {
            "HTTP_X_NBHD_INTERNAL_KEY": "shared-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }

    def _post_while_detector_hangs(self, post):
        detector_started = threading.Event()
        detector_release = threading.Event()

        def hanging_detector(*_args, **_kwargs):
            if not detector_started.is_set():
                detector_started.set()
                detector_release.wait(timeout=0.5)
            return []

        try:
            with (
                patch("apps.pii.redactor._detect_pii", side_effect=hanging_detector) as redactor_detect,
                patch("apps.pii.authoring._detect_pii", side_effect=hanging_detector) as authoring_detect,
            ):
                started_at = monotonic()
                response = post()
                elapsed = monotonic() - started_at
        finally:
            detector_release.set()

        self.assertLess(elapsed, 0.2, f"runtime write waited {elapsed:.3f}s for deferred detection")
        self.assertFalse(detector_started.is_set())
        redactor_detect.assert_not_called()
        authoring_detect.assert_not_called()
        return response

    @patch("apps.cron.publish.publish_task")
    def test_daily_note_set_section_masks_known_values_and_returns_before_detector(self, _publish):
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.DAILY,
            slug="2026-08-15",
            title="2026-08-15",
            markdown="# 2026-08-15\n\n## Evening Check-In\nOld entry\n",
            pii_receipts={
                "markdown": {
                    "state": "placeholder",
                    "redactions": [],
                    "writer": "background",
                }
            },
        )

        response = self._post_while_detector_hangs(
            lambda: self.client.post(
                f"/api/v1/integrations/runtime/{self.tenant.id}/daily-note/append/",
                {
                    "content": "Alice completed the evening review",
                    "date": "2026-08-15",
                    "section_slug": "evening-check-in",
                },
                format="json",
                **self.headers,
            )
        )

        self.assertEqual(response.status_code, 201, response.data)
        note = Document.objects.get(tenant=self.tenant, kind=Document.Kind.DAILY, slug="2026-08-15")
        self.assertIn("[PERSON_1] completed the evening review", note.markdown)
        self.assertNotIn("Alice", note.markdown)
        self.assertEqual(
            note.pii_receipts["markdown"],
            {
                "state": "unconfirmed",
                "reason": "detector-deferred",
                "redactions": [{"placeholder": "[PERSON_1]"}],
                "writer": "runtime",
            },
        )

    @patch("apps.cron.publish.publish_task")
    def test_fresh_daily_note_default_and_section_return_before_detector(self, _publish):
        NoteTemplate.objects.create(
            tenant=self.tenant,
            slug="alice-evening",
            name="Evening",
            is_default=True,
            sections=[
                {
                    "slug": "background-context",
                    "title": "Background Context",
                    "content": "Alice's starting point",
                    "source": "agent",
                },
                {
                    "slug": "evening-check-in",
                    "title": "Evening Check-In",
                    "content": "",
                    "source": "agent",
                },
            ],
        )

        response = self._post_while_detector_hangs(
            lambda: self.client.post(
                f"/api/v1/integrations/runtime/{self.tenant.id}/daily-note/append/",
                {
                    "content": "Alice completed the fresh-row review",
                    "date": "2026-08-16",
                    "section_slug": "evening-check-in",
                },
                format="json",
                **self.headers,
            )
        )

        self.assertEqual(response.status_code, 201, response.data)
        note = Document.objects.get(tenant=self.tenant, kind=Document.Kind.DAILY, slug="2026-08-16")
        self.assertIn("# 2026-08-16 (Evening)", note.markdown)
        self.assertIn("## Background Context\n[PERSON_1]'s starting point", note.markdown)
        self.assertIn("## Evening Check-In", note.markdown)
        self.assertIn("[PERSON_1] completed the fresh-row review", note.markdown)
        self.assertNotIn("Alice", note.markdown)
        self.assertEqual(note.pii_receipts["markdown"]["state"], "unconfirmed")
        self.assertEqual(note.pii_receipts["markdown"]["reason"], "detector-deferred")
        self.assertEqual(note.pii_receipts["markdown"]["writer"], "runtime")
        self.assertEqual(
            note.pii_receipts["markdown"]["redactions"],
            [{"placeholder": "[PERSON_1]"}],
        )

    @patch("apps.lessons.clustering.refresh_constellation")
    @patch("apps.lessons.services.process_approved_lesson")
    def test_lesson_suggest_masks_known_values_and_returns_before_detector(self, _process, _refresh):
        response = self._post_while_detector_hangs(
            lambda: self.client.post(
                f"/api/v1/integrations/runtime/{self.tenant.id}/lessons/",
                {
                    "text": "Ask Alice for constraints",
                    "context": "Evening review with Alice",
                    "source_type": "reflection",
                    "source_ref": "cron:evening-check-in",
                },
                format="json",
                **self.headers,
            )
        )

        self.assertEqual(response.status_code, 201, response.data)
        lesson = Lesson.objects.get(tenant=self.tenant, source_ref="cron:evening-check-in")
        self.assertEqual(lesson.text, "Ask [PERSON_1] for constraints")
        self.assertEqual(lesson.context, "Evening review with [PERSON_1]")
        for field in ("text", "context"):
            self.assertEqual(
                lesson.pii_receipts[field],
                {
                    "state": "unconfirmed",
                    "reason": "detector-deferred",
                    "redactions": [{"placeholder": "[PERSON_1]"}],
                    "writer": "runtime",
                },
            )
