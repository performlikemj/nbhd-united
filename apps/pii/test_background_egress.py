from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory
from rest_framework.views import APIView

from apps.core import compose, render
from apps.insights.synthesis import _call_synthesis_llm
from apps.journal.agenda_hints import _classify
from apps.journal.extraction import _call_extraction_llm
from apps.lessons.tutoring import _tutor_request
from apps.pii.egress import ENTITY_LEGEND_HEADER, KnownValueResponseGuardMixin
from apps.tenants.models import Tenant, User


def _tenant_stub():
    return SimpleNamespace(
        id="tenant-a",
        pk="tenant-a",
        pii_entity_map={
            "[PERSON_1]": {
                "name": "Theo Smith",
                "relationship": "recruiter at Optiver",
                "notes": "from work",
            },
            "[ORG_1]": "Optiver",
        },
    )


def _completion(content="{}"):
    return {"choices": [{"message": {"content": content}}], "usage": {}}, "test/model"


class BackgroundPromptGuardTests(SimpleTestCase):
    def assert_has_entity_legend(self, prompt):
        self.assertIn(ENTITY_LEGEND_HEADER, prompt)
        self.assertIn("[PERSON_1]: recruiter at [ORG_1]; from work", prompt)
        self.assertNotIn("Theo Smith", prompt)
        self.assertNotIn("Optiver", prompt)

    @patch("apps.journal.extraction.chat_completion", return_value=_completion())
    def test_extraction_prompt_is_guarded(self, completion):
        _call_extraction_llm("Theo Smith joined Optiver", tenant=_tenant_stub())
        prompt = completion.call_args.args[1][1]["content"]
        self.assertIn("[PERSON_1]", prompt)
        self.assertIn("[ORG_1]", prompt)
        self.assert_has_entity_legend(prompt)

    @patch("apps.journal.agenda_hints.chat_completion", return_value=_completion('{"matches": []}'))
    def test_agenda_prompt_is_guarded(self, completion):
        thread = SimpleNamespace(kind="task", item_id="1", label="Theo Smith", context="Optiver")
        _classify("Theo Smith journal", [thread], tenant=_tenant_stub())
        prompt = completion.call_args.args[1][1]["content"]
        self.assert_has_entity_legend(prompt)

    @patch("apps.insights.synthesis._format_context_for_prompt", return_value="Theo Smith at Optiver")
    @patch("apps.insights.synthesis.chat_completion", return_value=_completion("reflection"))
    def test_synthesis_prompt_is_guarded(self, completion, _format):
        _call_synthesis_llm({}, tenant=_tenant_stub())
        prompt = completion.call_args.args[1][1]["content"]
        self.assertTrue(prompt.startswith("[PERSON_1] at [ORG_1]"))
        self.assert_has_entity_legend(prompt)

    @override_settings(OPENROUTER_API_KEY="test-key")
    @patch("apps.core.compose.render.validate_manifest", return_value=[])
    @patch("apps.core.compose._normalize", return_value={"ok": True})
    @patch("apps.core.compose.chat_completion", return_value=_completion("{}"))
    def test_meditation_compose_prompt_is_guarded(self, completion, _normalize, _validate):
        compose.author_manifest({"additional_context": "Theo Smith at Optiver"}, tenant=_tenant_stub())
        prompt = completion.call_args.args[1][1]["content"]
        self.assert_has_entity_legend(prompt)

    @patch("apps.pii.egress.build_entity_legend", side_effect=RuntimeError("map unavailable"))
    @patch("apps.journal.extraction.chat_completion", return_value=_completion())
    def test_extraction_legend_failure_is_fail_open(self, completion, _legend):
        with self.assertLogs("apps.pii.egress", level="WARNING") as logs:
            _call_extraction_llm("Theo Smith joined Optiver", tenant=_tenant_stub())
        prompt = completion.call_args.args[1][1]["content"]
        self.assertEqual(prompt, "Extract from this daily note:\n\n[PERSON_1] joined [ORG_1]")
        self.assertIn(
            "pii_egress_guard_error tenant=tenant-a seam=legend:journal_extraction_prompt",
            logs.output[0],
        )

    @patch("apps.core.render.time.sleep")
    def test_gemini_tts_narration_is_guarded(self, _sleep):
        generate = Mock(side_effect=RuntimeError("stop"))
        client = SimpleNamespace(models=SimpleNamespace(generate_content=generate))
        with self.assertRaises(RuntimeError):
            render.render_gemini_segment(
                client,
                "Breathe with Theo Smith at Optiver",
                "Aoede",
                "gemini-test",
                "calm",
                Path("unused.wav"),
                attempts=1,
                tenant=_tenant_stub(),
            )
        prompt = generate.call_args.kwargs["contents"]
        self.assertNotIn("Theo Smith", prompt)
        self.assertNotIn("Optiver", prompt)
        self.assertNotIn(ENTITY_LEGEND_HEADER, prompt)


class _GuardedTestView(KnownValueResponseGuardMixin, APIView):
    authentication_classes = []
    permission_classes = []
    pii_egress_seam = "test_runtime_response"
    pii_egress_text_fields = frozenset({"title", "description"})

    def get(self, request, tenant_id):
        return Response(
            {
                "id": "Theo Smith-id",
                "slug": "theo-smith",
                "amount": 42.5,
                "status": "Optiver",
                "title": "Theo Smith plan",
                "items": [{"description": "Call Optiver", "kind": "Theo Smith"}],
            }
        )

    def post(self, request, tenant_id):
        return Response({"error": "conflict", "description": "Theo Smith at Optiver"}, status=409)


class ProviderAndRuntimeGuardTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="phase2-guard")
        self.tenant = Tenant.objects.create(user=self.user)
        self.tenant.pii_entity_map = {
            "[PERSON_1]": {
                "name": "Theo Smith",
                "relationship": "recruiter at Optiver",
                "notes": "from work",
            },
            "[ORG_1]": "Optiver",
        }
        self.tenant.save(update_fields=["pii_entity_map"])

    @override_settings(OPENROUTER_API_KEY="test-key")
    @patch("apps.common.openrouter.requests.post")
    def test_tutoring_prompt_is_guarded(self, post):
        response = Mock()
        response.json.return_value = {"choices": [{"message": {"content": "{}"}}], "usage": {}}
        post.return_value = response
        _tutor_request(
            [{"role": "user", "content": "Coach Theo Smith at Optiver"}],
            tenant_id=str(self.tenant.id),
        )
        prompt = post.call_args.kwargs["json"]["messages"][0]["content"]
        self.assertTrue(prompt.startswith("Coach [PERSON_1] at [ORG_1]"))
        self.assertIn(ENTITY_LEGEND_HEADER, prompt)
        self.assertIn("[PERSON_1]: recruiter at [ORG_1]; from work", prompt)
        self.assertNotIn("Theo Smith", prompt)
        self.assertNotIn("Optiver", prompt)

    def test_runtime_mixin_guards_only_human_text_fields(self):
        request = APIRequestFactory().get("/")
        response = _GuardedTestView.as_view()(request, tenant_id=self.tenant.id)
        self.assertEqual(response.data["title"], "[PERSON_1] plan")
        self.assertEqual(response.data["items"][0]["description"], "Call [ORG_1]")
        self.assertEqual(response.data["id"], "Theo Smith-id")
        self.assertEqual(response.data["slug"], "theo-smith")
        self.assertEqual(response.data["amount"], 42.5)
        self.assertEqual(response.data["status"], "Optiver")
        self.assertEqual(response.data["items"][0]["kind"], "Theo Smith")

    def test_runtime_mixin_guards_human_text_in_error_envelopes(self):
        request = APIRequestFactory().post("/", {})
        response = _GuardedTestView.as_view()(request, tenant_id=self.tenant.id)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data, {"error": "conflict", "description": "[PERSON_1] at [ORG_1]"})

    @patch("apps.tenants.models.Tenant.objects.filter", side_effect=RuntimeError("map unavailable"))
    def test_runtime_mixin_failure_returns_original_and_logs(self, _filter):
        request = APIRequestFactory().get("/")
        with self.assertLogs("apps.pii.egress", level="WARNING") as logs:
            response = _GuardedTestView.as_view()(request, tenant_id=self.tenant.id)
        self.assertEqual(response.data["title"], "Theo Smith plan")
        self.assertIn(
            f"pii_egress_guard_error tenant={self.tenant.id} seam=test_runtime_response",
            logs.output[0],
        )

    def test_required_runtime_views_attach_family_specific_guards(self):
        from apps.finance.runtime_views import (
            RuntimeFinanceAccountsView,
            RuntimeFinanceSummaryView,
            RuntimeFinanceTransactionsView,
        )
        from apps.fuel.runtime_views import (
            RuntimeFuelProfileView,
            RuntimeFuelSummaryView,
            RuntimeLogWorkoutView,
            RuntimeWorkoutPlanListCreateView,
        )
        from apps.insights.runtime_views import RuntimeInsightListView
        from apps.integrations.runtime_views import (
            RuntimeCronCreatePureReminderView,
            RuntimeDocumentView,
            RuntimeGoalListCreateView,
            RuntimeJournalContextView,
            RuntimeJournalSearchView,
            RuntimeLessonSearchView,
            RuntimeMissionsView,
            RuntimeReconcileScanView,
            RuntimeTaskListCreateView,
        )

        expectations = {
            RuntimeDocumentView: {"title", "markdown"},
            RuntimeJournalContextView: {"title", "markdown"},
            RuntimeJournalSearchView: {"title", "snippet", "query"},
            RuntimeGoalListCreateView: {"title", "description"},
            RuntimeTaskListCreateView: {"title", "description"},
            RuntimeReconcileScanView: {"claim", "statement", "excerpt", "matched_tokens"},
            RuntimeLessonSearchView: {"text", "context", "query"},
            RuntimeCronCreatePureReminderView: {"text", "render_block"},
            RuntimeMissionsView: {"title", "my_commitment", "next_step"},
            RuntimeFuelSummaryView: {"activity", "objective"},
            RuntimeFuelProfileView: {"goals", "limitations"},
            RuntimeLogWorkoutView: {"activity", "notes"},
            RuntimeWorkoutPlanListCreateView: {"name", "objective", "notes"},
            RuntimeFinanceAccountsView: {"nickname"},
            RuntimeFinanceTransactionsView: {"description"},
            RuntimeFinanceSummaryView: {"nickname"},
            RuntimeInsightListView: {"statement", "response_note"},
        }
        for view_class, fields in expectations.items():
            with self.subTest(view=view_class.__name__):
                self.assertTrue(issubclass(view_class, KnownValueResponseGuardMixin))
                self.assertTrue(fields <= view_class.pii_egress_text_fields)
