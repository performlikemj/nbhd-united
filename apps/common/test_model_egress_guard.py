"""Static guards for model-provider egress and embedding caller scope."""

from __future__ import annotations

import ast
import re
import subprocess
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

_ROOT = Path(__file__).resolve().parents[2]
_DEPLOYABLE_ROOTS = ("apps", "config", "runtime", "scripts")
_SOURCE_SUFFIXES = {
    ".py",
    ".js",
    ".ts",
    ".mjs",
    ".cjs",
    ".sh",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
}

_MODEL_HOSTNAME = re.compile(
    r"api\.openai\.com|openai\.com/v1|api\.anthropic\.com|"
    r"generativelanguage\.googleapis\.com|aiplatform\.googleapis\.com|"
    r"api\.mistral\.ai|api\.deepseek\.com|api\.groq\.com|deepgram|elevenlabs",
    re.IGNORECASE,
)
_SDK_USAGE = re.compile(
    r"from\s+openai\s+import|\bOpenAI\s*\(|\bAnthropic\s*\(|@anthropic-ai/sdk|"
    r"(?:from|require\s*\()\s*['\"]openai['\"]|['\"]openai['\"]\s*:",
)
_PROVIDER_KEY_READ = re.compile(
    r"settings\.(?:OPENAI|ANTHROPIC)_API_KEY|"
    r"getattr\(settings,\s*['\"](?:OPENAI|ANTHROPIC)_API_KEY['\"]|"
    r"os\.(?:getenv|environ\.get)\(\s*['\"](?:OPENAI|ANTHROPIC)_API_KEY['\"]|"
    r"process\.env\.(?:OPENAI|ANTHROPIC)_API_KEY|"
    r"env\(\s*['\"](?:OPENAI|ANTHROPIC)_API_KEY['\"]",
)

# PR-B2 removes the azure_client/image-gen runtime seams; integration must
# shrink those allowlist entries once that lands. On PR-A alone they remain
# real, so keeping exact path+rule pairs makes any new use fail. Django's
# settings declaration is configuration, not a provider call.
_ALLOWED_MATCHES = {
    ("config/settings/base.py", "provider_key_read"),
    ("apps/orchestrator/azure_client.py", "provider_key_read"),
    ("runtime/openclaw/plugins/nbhd-image-gen/index.js", "model_hostname"),
    ("runtime/openclaw/plugins/nbhd-image-gen/index.js", "provider_key_read"),
}

_EGRESS_PATTERNS = (
    ("model_hostname", _MODEL_HOSTNAME),
    ("sdk_usage", _SDK_USAGE),
    ("provider_key_read", _PROVIDER_KEY_READ),
)


def _deployable_files():
    tracked = subprocess.run(
        ["git", "-C", str(_ROOT), "ls-files", "-z"],
        check=True,
        capture_output=True,
    ).stdout
    for encoded_relative in tracked.split(b"\0"):
        if not encoded_relative:
            continue
        relative = Path(encoded_relative.decode("utf-8"))
        path = _ROOT / relative
        if not path.is_file():
            continue

        root_special = len(relative.parts) == 1 and (
            relative.name.startswith("Dockerfile") or "entrypoint" in relative.name
        )
        deployable_source = (
            bool(relative.parts)
            and relative.parts[0] in _DEPLOYABLE_ROOTS
            and path.suffix in _SOURCE_SUFFIXES
            and "tests" not in relative.parts
            and not path.name.startswith("test")
            and ".test." not in path.name
        )
        if root_special or deployable_source:
            yield path


def _find_offenders():
    offenders = []
    for path in _deployable_files():
        relative = path.relative_to(_ROOT).as_posix()
        for line_number, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            if line.lstrip().startswith(("#", "//")):
                continue
            for rule, pattern in _EGRESS_PATTERNS:
                if pattern.search(line) and (relative, rule) not in _ALLOWED_MATCHES:
                    offenders.append((relative, line_number, rule))
    return offenders


class ModelEgressStaticGuard(SimpleTestCase):
    def test_no_unapproved_provider_egress(self):
        offenders = _find_offenders()
        self.assertEqual(
            offenders,
            [],
            "Direct model-provider egress, SDK usage, or provider-key reads must "
            f"use an explicitly reviewed seam. Offenders: {offenders}",
        )

    def test_tracked_offender_is_scanned_and_untracked_artifact_is_ignored(self):
        tracked_offender = _ROOT / "runtime/openclaw/plugins/nbhd-image-gen/index.js"
        self.assertIn(tracked_offender, set(_deployable_files()))
        self.assertRegex(tracked_offender.read_text(), _MODEL_HOSTNAME)

        runtime_dir = _ROOT / "runtime/openclaw"
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=runtime_dir,
            prefix="egress-guard-untracked-",
            suffix=".js",
            delete=False,
        ) as artifact:
            artifact.write('const OpenAI = require("openai");\n')
            artifact_path = Path(artifact.name)

        try:
            self.assertRegex(artifact_path.read_text(), _SDK_USAGE)
            self.assertNotIn(artifact_path, set(_deployable_files()))
            relative = artifact_path.relative_to(_ROOT).as_posix()
            self.assertFalse(any(path == relative for path, _line, _rule in _find_offenders()))
        finally:
            artifact_path.unlink(missing_ok=True)

    def test_six_embedding_callers_all_pass_tenant(self):
        expected = {
            "apps/lessons/services.py": 2,
            "apps/journal/extraction.py": 1,
            "apps/journal/embedding.py": 1,
            "apps/journal/workspace_services.py": 1,
            "apps/router/poller.py": 1,
        }
        found: dict[str, int] = {}
        missing_tenant = []
        for relative, count in expected.items():
            tree = ast.parse((_ROOT / relative).read_text())
            calls = [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "generate_embedding"
            ]
            found[relative] = len(calls)
            missing_tenant.extend(
                (relative, node.lineno)
                for node in calls
                if not any(keyword.arg == "tenant" for keyword in node.keywords)
            )

        self.assertEqual(found, expected)
        self.assertEqual(missing_tenant, [])

    def test_all_openrouter_http_callers_use_mandatory_builder(self):
        request_modules = {
            "apps/common/openrouter.py",
            "apps/lessons/services.py",
            "apps/router/transcription.py",
        }
        for relative in request_modules:
            source = (_ROOT / relative).read_text()
            self.assertIn("build_openrouter_body(", source, relative)

        migrated_chat_clients = {
            "apps/lessons/cluster_naming.py",
            "apps/lessons/copilot.py",
            "apps/lessons/tutoring.py",
            "apps/lessons/management/commands/rewrite_lessons_actionable.py",
        }
        for relative in migrated_chat_clients:
            source = (_ROOT / relative).read_text()
            self.assertIn("chat_completion(", source, relative)
            self.assertNotIn("requests.post(", source, relative)
