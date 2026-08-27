"""Current workspace-memory template and seed-path contracts."""

from pathlib import Path

from django.test import SimpleTestCase

_ROOT = Path(__file__).resolve().parents[2]


class WorkspaceMemoryTemplateTests(SimpleTestCase):
    def test_agents_names_current_session_context_files(self):
        agents = (_ROOT / "templates/openclaw/AGENTS.md").read_text()
        for phrase in ("## Session Start", "SOUL.md", "USER.md", "IDENTITY.md", "TOOLS.md"):
            self.assertIn(phrase, agents)
        self.assertNotIn("MEMORY.md", agents)
        self.assertNotIn("memory/YYYY-MM-DD.md", agents)

    def test_memory_template_keeps_its_four_user_memory_sections(self):
        memory = (_ROOT / "templates/openclaw/MEMORY.md").read_text()
        for heading in ("## About You", "## Your Preferences", "## Things to Remember", "## Patterns I've Noticed"):
            self.assertIn(heading, memory)

    def test_heartbeat_template_keeps_memory_maintenance(self):
        heartbeat = (_ROOT / "templates/openclaw/HEARTBEAT.md").read_text()
        self.assertIn("memory/YYYY-MM-DD.md", heartbeat)
        self.assertIn("promote it to `MEMORY.md`", heartbeat)

    def test_entrypoint_creates_memory_dir_and_seed_once_templates(self):
        entrypoint = (_ROOT / "runtime/openclaw/entrypoint.sh").read_text()
        self.assertIn('mkdir -p "$OPENCLAW_HOME" "$OPENCLAW_WORKSPACE_PATH" "$NBHD_MEMORY_DIR"', entrypoint)
        self.assertIn("for file in USER.md TOOLS.md MEMORY.md HEARTBEAT.md; do", entrypoint)
        self.assertIn('if [ -f "$src" ] && [ ! -f "$dst" ]; then', entrypoint)
