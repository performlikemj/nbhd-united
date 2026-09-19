#!/usr/bin/env python3
"""Generate local-only configuration and unloaded launchd definitions."""

import json
import os
import plistlib
import re
import secrets
import shutil
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "deploy/local-test/.state"
TEST_HOME = Path("/Users/mjjones/openclaw-yuki-test")


def main():
    os.umask(0o077)
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / "tmp").mkdir(exist_ok=True)
    if TEST_HOME.is_symlink():
        raise RuntimeError("Test home must not be a symlink")
    TEST_HOME.mkdir(mode=0o700, exist_ok=True)
    TEST_HOME.chmod(0o700)
    env_path = ROOT / ".env.local-test"
    if not env_path.exists():
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=5) as response:
            tags = json.load(response)["models"]
        names = [item["name"] for item in tags]
        preferred = "qwen3.8:27b-obliterated-q8"
        if preferred not in names:
            raise RuntimeError("Preferred pre-pulled model absent; choose explicitly before install")
        values = {}
        # Preserve the example's names/comments but blank every example value.
        for line in (ROOT / ".env.example").read_text().splitlines():
            if line and not line.startswith("#") and "=" in line:
                values[line.split("=", 1)[0]] = ""
        source = (ROOT / "config/settings/base.py").read_text()
        keys = re.findall(r'env(?:\.\w+)?\(\s*["\']([A-Z0-9_]+)', source)
        for key in keys:
            if any(
                word in key
                for word in (
                    "SECRET",
                    "TOKEN",
                    "API_KEY",
                    "AZURE_",
                    "STRIPE_",
                    "APNS_",
                    "COMPOSIO_",
                    "OAUTH_",
                    "SENTRY_",
                    "ADMIN_OPENCLAW_",
                    "STEWARD_",
                )
            ):
                values[key] = ""
        for key in (
            "SECRET_KEY",
            "JWT_SECRET",
            "NBHD_INTERNAL_API_KEY",
            "LOCAL_TEST_DB_PASSWORD",
            "LOCAL_TEST_KEK_SEED",
        ):
            values[key] = secrets.token_urlsafe(48)
        values.update(
            {
                "DJANGO_SETTINGS_MODULE": "config.settings.local_test",
                "DEBUG": "true",
                "ALLOWED_HOSTS": "127.0.0.1,localhost",
                "AZURE_MOCK": "true",
                "DATABASE_URL": "postgres://nbhd_yuki_test:"
                + values["LOCAL_TEST_DB_PASSWORD"]
                + "@127.0.0.1:55441/nbhd_yuki_test",
                "REDIS_URL": "redis://127.0.0.1:56381/0",
                "CELERY_BROKER_URL": "",
                "API_BASE_URL": "http://127.0.0.1:18080",
                "FRONTEND_URL": "http://127.0.0.1:18080",
                "NBHD_API_BASE_URL": "http://127.0.0.1:18080",
                "LOCAL_TEST_ROOT": str(TEST_HOME),
                "LOCAL_TEST_MODEL": preferred,
                "NBHD_TENANT_ID": str(uuid.uuid4()),
                "OPENCLAW_HOME": str(TEST_HOME),
                "OPENCLAW_STATE_DIR": str(TEST_HOME / "state"),
                "OPENCLAW_CONFIG_PATH": str(TEST_HOME / "openclaw.json"),
                "OPENCLAW_WORKSPACE_DIR": str(TEST_HOME / "workspace"),
                "OPENCLAW_CONTAINER_SECRET_BACKEND": "env",
                "STRIPE_LIVE_MODE": "false",
                "OPENROUTER_PER_TENANT_KEYS_ENABLED": "false",
                "COPILOT_LLM_ENABLED": "false",
                "PII_MODEL_PATH": str(TEST_HOME / "pii-model"),
                "QSTASH_TOKEN": "",
                "PII_DETECTOR_ENGINE": "deberta",
                "PII_DETECTOR_TRANSPORT": "local",
                "PII_SHARED_DEADLINE_S": "5.0",
                "PII_SHARED_QUEUE_MAX": "64",
                "PII_SHARED_WARM_WAIT_S": "90",
                "SENTRY_ENABLE_LOGS": "false",
                "SENTRY_SEND_DEFAULT_PII": "false",
                "SENTRY_TRACES_SAMPLE_RATE": "0",
                "SENTRY_PROFILE_SESSION_SAMPLE_RATE": "0",
                "SAUTAI_M2M_BASE_URL": "http://127.0.0.1:8000",
                "SAUTAI_PLATFORM_SECRET": "",
                "USAGE_DASHBOARD_SUBSCRIPTION_PRICE": "12.0",
            }
        )
        values["ADMIN_DATABASE_URL"] = values["DATABASE_URL"]
        for key, plugin in re.findall(
            r'(OPENCLAW_\w+_PLUGIN_PATH)\s*=\s*env\(\s*["\'][^"\']+["\'],\s*["\']/opt/nbhd/plugins/([^"\']+)', source
        ):
            values[key] = str(ROOT / "runtime/openclaw/plugins" / plugin)
        generator = (ROOT / "apps/orchestrator/config_generator.py").read_text()
        for key, plugin in re.findall(
            r"getattr\(settings, [\"\'](OPENCLAW_\w+_PLUGIN_PATH)[\"\'], [\"\']/opt/nbhd/plugins/([^\"\']+)", generator
        ):
            values[key] = str(ROOT / "runtime/openclaw/plugins" / plugin)
        # Enable the runtime surface used by the container for these features.
        values["OPENCLAW_USAGE_PLUGIN_ID"] = "nbhd-usage-reporter"
        values["OPENCLAW_JOURNAL_PLUGIN_ID"] = "nbhd-journal-tools"
        values["OPENCLAW_SAUTAI_PLUGIN_ID"] = "nbhd-sautai-tools"
        lines = []
        seen = set()
        for line in (ROOT / ".env.example").read_text().splitlines():
            if line and not line.startswith("#") and "=" in line:
                key = line.split("=", 1)[0]
                lines.append(f"{key}={values[key]}")
                seen.add(key)
            else:
                lines.append(line)
        lines.extend(f"{key}={value}" for key, value in sorted(values.items()) if key not in seen)
        env_path.write_text("\n".join(lines) + "\n")
        env_path.chmod(0o600)
    # No secret values in plists. No launchctl calls here.
    for suffix, command in (("united", "django"), ("united-gateway", "gateway")):
        label = f"com.mj.yuki-{suffix}"
        plist = {
            "Label": label,
            "ProgramArguments": [shutil.which("python3"), str(ROOT / "deploy/local-test/run.py"), command],
            "WorkingDirectory": str(ROOT),
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(STATE / f"{label}.log"),
            "StandardErrorPath": str(STATE / f"{label}.err"),
        }
        (STATE / f"{label}.plist").write_bytes(plistlib.dumps(plist))
    print("Generated .env.local-test (0600) and two unloaded plists in deploy/local-test/.state/.")
    print("Next: Compose postgres+redis, migrate, orchestrator loads Django plist, MJ signs up.")


if __name__ == "__main__":
    main()
