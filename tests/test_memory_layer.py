"""Tests for the agent memory integration layer."""
import os
import subprocess
import tempfile

from django.test import TestCase

from apps.orchestrator.tool_policy import generate_tool_config, get_allowed_tools


class AgentsMemoryInstructionsTest(TestCase):
    """Verify AGENTS.md template includes memory system instructions."""

    AGENTS_PATH = os.path.join(
        os.path.dirname(__file__), "..", "templates", "openclaw", "AGENTS.md"
    )

    def _read_agents(self):
        with open(self.AGENTS_PATH) as f:
            return f.read()

    def test_agents_md_exists(self):
        self.assertTrue(os.path.exists(self.AGENTS_PATH))

    def test_agents_md_references_memory_file(self):
        content = self._read_agents()
        self.assertIn("MEMORY.md", content)

    def test_agents_md_references_daily_notes(self):
        content = self._read_agents()
        self.assertIn("memory/YYYY-MM-DD.md", content)

    def test_agents_md_references_soul_and_user(self):
        content = self._read_agents()
        self.assertIn("SOUL.md", content)
        self.assertIn("USER.md", content)

    def test_agents_md_has_session_startup(self):
        content = self._read_agents()
        self.assertIn("Every Session", content)

    def test_agents_md_has_security_section(self):
        content = self._read_agents()
        self.assertIn("Never store", content.lower().replace("never store", "Never store"))
        # Check for password/secret warnings
        self.assertIn("password", content.lower())

    def test_agents_md_under_300_lines(self):
        content = self._read_agents()
        lines = content.strip().split("\n")
        self.assertLessEqual(len(lines), 300, f"AGENTS.md is {len(lines)} lines (max 300)")


class MemoryTemplateTest(TestCase):
    """Verify MEMORY.md template exists and has expected structure."""

    MEMORY_PATH = os.path.join(
        os.path.dirname(__file__), "..", "templates", "openclaw", "MEMORY.md"
    )

    def test_memory_template_exists(self):
        self.assertTrue(os.path.exists(self.MEMORY_PATH))

    def test_memory_template_has_sections(self):
        with open(self.MEMORY_PATH) as f:
            content = f.read()
        self.assertIn("About You", content)
        self.assertIn("Preferences", content)
        self.assertIn("Things to Remember", content)
        self.assertIn("Patterns", content)


class HeartbeatTemplateTest(TestCase):
    """Verify HEARTBEAT.md template exists."""

    HEARTBEAT_PATH = os.path.join(
        os.path.dirname(__file__), "..", "templates", "openclaw", "HEARTBEAT.md"
    )

    def test_heartbeat_template_exists(self):
        self.assertTrue(os.path.exists(self.HEARTBEAT_PATH))

    def test_heartbeat_references_memory_maintenance(self):
        with open(self.HEARTBEAT_PATH) as f:
            content = f.read()
        self.assertIn("MEMORY.md", content)


class EntrypointMemorySeedingTest(TestCase):
    """Verify entrypoint.sh seeds memory files correctly."""

    ENTRYPOINT_PATH = os.path.join(
        os.path.dirname(__file__), "..", "runtime", "openclaw", "entrypoint.sh"
    )

    def _read_entrypoint(self):
        with open(self.ENTRYPOINT_PATH) as f:
            return f.read()

    def test_entrypoint_creates_memory_directory(self):
        content = self._read_entrypoint()
        self.assertIn("NBHD_MEMORY_DIR", content)
        # Should be in a mkdir -p call
        self.assertIn("mkdir -p", content)

    def test_entrypoint_seeds_memory_md(self):
        content = self._read_entrypoint()
        self.assertIn("MEMORY.md", content)

    def test_entrypoint_seeds_heartbeat_md(self):
        content = self._read_entrypoint()
        self.assertIn("HEARTBEAT.md", content)

    def test_entrypoint_seed_once_pattern(self):
        """MEMORY.md and HEARTBEAT.md should use seed-once (not overwrite)."""
        content = self._read_entrypoint()
        # The seed-once loop checks [ ! -f "$dst" ] before copying
        self.assertIn('[ ! -f "$dst" ]', content)
        # MEMORY.md and HEARTBEAT.md should be in the seed-once loop
        self.assertIn("MEMORY.md", content)
        self.assertIn("HEARTBEAT.md", content)


class ToolPolicyMemoryTest(TestCase):
    """Verify tool policy allows memory and file tools."""

    def test_basic_tier_allows_memory_group(self):
        allowed = get_allowed_tools("basic")
        self.assertIn("group:memory", allowed)

    def test_basic_tier_allows_files_group(self):
        allowed = get_allowed_tools("basic")
        self.assertIn("group:files", allowed)

    def test_plus_tier_allows_memory_group(self):
        allowed = get_allowed_tools("plus")
        self.assertIn("group:memory", allowed)

    def test_config_includes_memory_tools(self):
        config = generate_tool_config("basic")
        self.assertIn("group:memory", config["allow"])
        self.assertIn("group:files", config["allow"])
