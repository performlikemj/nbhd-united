"""Opt-in local filesystem adapter for the isolated synthetic test stack."""

import hashlib
import hmac
import os
from pathlib import Path

from django.conf import settings


def local_root(tenant_id, *, require_tenant=True):
    root = getattr(settings, "LOCAL_TEST_ROOT", "")
    if not root:
        return None
    if not settings.DEBUG or os.environ.get("AZURE_MOCK") != "true":
        raise RuntimeError("Local test storage requires DEBUG and AZURE_MOCK=true")
    if str(tenant_id) != os.environ.get("NBHD_TENANT_ID"):
        raise ValueError("Not the designated local test tenant")
    from apps.tenants.models import Tenant

    if require_tenant and not Tenant.objects.filter(id=tenant_id, is_synthetic=True, is_eval_sink=False).exists():
        raise ValueError("Local storage requires a synthetic non-sink tenant")
    return Path(root).resolve()


def share_path(tenant_id, file_path):
    root = local_root(tenant_id)
    if root is None:
        return None
    path = (root / file_path).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Local share path escapes test home")
    return path


def mock_kek(tenant_id):
    if local_root(tenant_id) is None:
        return None
    seed = os.environ["LOCAL_TEST_KEK_SEED"]
    return hmac.digest(seed.encode(), str(tenant_id).encode(), hashlib.sha256)


def configure_gateway(config, tenant):
    root = local_root(tenant.id)
    if root is None:
        return
    model = os.environ["LOCAL_TEST_MODEL"]
    name = f"ollama/{model}"
    config["auth"] = {"profiles": {}}
    config.pop("env", None)
    config["channels"] = {}
    config["models"] = {
        "providers": {
            "ollama": {
                "baseUrl": "http://127.0.0.1:11434/v1",
                "api": "openai-completions",
                "injectNumCtxForOpenAICompat": False,
                "apiKey": "local-ollama-no-auth",
                "models": [
                    {
                        "id": model,
                        "name": model,
                        "reasoning": False,
                        "input": ["text"],
                        "contextWindow": 262144,
                        "maxTokens": 8192,
                        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                    }
                ],
            }
        }
    }
    defaults = config["agents"]["defaults"]
    defaults["workspace"] = str(root / "workspace")
    defaults["model"] = {"primary": name, "fallbacks": []}
    defaults["models"] = {name: {}}
    defaults["pdfModel"] = {"primary": name, "fallbacks": []}
    defaults["subagents"]["model"] = name
    defaults["heartbeat"] = {"every": "0m"}
    defaults["memorySearch"] = {"enabled": False}
    defaults["compaction"]["memoryFlush"] = {"enabled": False}
    # Never pass num_ctx: Ollama retains its server default.
    defaults.pop("params", None)
    defaults.pop("contextPruning", None)
    config["gateway"]["port"] = 19443
    config["gateway"]["bind"] = "loopback"
    config["gateway"]["auth"]["token"] = "${NBHD_INTERNAL_API_KEY}"
    config.setdefault("logging", {})["file"] = str(root / "gateway.log")
    denied = config["tools"].setdefault("deny", [])
    for tool in ("exec", "process", "browser", "web_search", "web_fetch", "image", "pdf"):
        if tool not in denied:
            denied.append(tool)
    # Local model performs tools through the repo runtime plugins only.

    paths = config.get("plugins", {}).get("load", {}).get("paths", [])
    config["plugins"]["load"]["paths"] = [
        str(settings.BASE_DIR / "runtime/openclaw/plugins" / path.removeprefix("/opt/nbhd/plugins/"))
        if path.startswith("/opt/nbhd/plugins/")
        else path
        for path in paths
    ]
    # The installed 2026.9.1 already uses these schema moves (verified with
    # `openclaw config validate`); fleet's 9.4 transform also adds cloud Brave,
    # which this offline stack must not load.
    defaults["pdfMaxMb"] = defaults.pop("pdfMaxBytesMb", 10)
    defaults.pop("envelopeTimezone", None)
    defaults.pop("memorySearch", None)
    config["memory"] = {"search": {"enabled": False}}
    config["logging"].pop("redactSensitive", None)
    config["plugins"].pop("bundledDiscovery", None)
    config.pop("commitments", None)
    config["tools"]["media"] = {"audio": {"enabled": False}, "image": {"enabled": False}, "video": {"enabled": False}}
    config["tools"]["web"] = {"search": {"enabled": False}, "fetch": {"enabled": False}}


def dispatch_task(task_path, args, kwargs, delay_seconds=None):
    """Keep local runtime HTTP acks fast and preserve delayed poll semantics."""
    import threading

    from django.db import close_old_connections, transaction

    if not settings.DEBUG or os.environ.get("AZURE_MOCK") != "true":
        raise RuntimeError("Local task executor requires DEBUG/AZURE_MOCK")

    def run():
        from apps.cron.views import execute_task_sync

        close_old_connections()
        try:
            execute_task_sync(task_path, *args, **kwargs)
        finally:
            close_old_connections()

    def start():
        timer = threading.Timer(max(0, delay_seconds or 0), run)
        timer.daemon = True
        timer.start()

    transaction.on_commit(start)
