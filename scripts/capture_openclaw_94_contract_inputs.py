"""Generate synthetic signed CLI-contract inputs; no database/network calls.

Run from the repository root with PYTHONPATH=. and the project's Python env.
Only query results and token lookup are stubbed: selector, row rendering and
HMAC signer are unchanged production code. Companion .mjs runs in the isolated
runtime container, never against a tenant.
"""

import copy
import os

os.environ.update(
    DJANGO_SETTINGS_MODULE="config.settings.development",
    SECRET_KEY="local-contract-test",
    DATABASE_URL="postgres://test_user:test_password@127.0.0.1:55496/oc94_r6",
    AZURE_MOCK="true",
    NBHD_DISABLE_BACKGROUND_THREADS="True",
)
import django

django.setup()
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from apps.cron.share_cron_sync import build_signed_crons_doc
from apps.orchestrator.cron_reconcile import _row_to_cron_dict

cases = {
    "cron-tz": {"schedule": {"kind": "cron", "expr": "17 9 * * *", "tz": "Asia/Tokyo"}},
    "cron-top-hour": {"schedule": {"kind": "cron", "expr": "0 * * * *", "tz": "UTC"}},
    "every": {"schedule": {"kind": "every", "everyMs": 86400000}},
    "every-anchor": {"schedule": {"kind": "every", "everyMs": 86400000, "anchorMs": 1798761600000}},
    "at-offset": {"schedule": {"kind": "at", "at": "2027-01-01T09:00:00+09:00", "tz": "Asia/Tokyo"}},
    "at-utc": {"schedule": {"kind": "at", "at": "2027-01-01T00:00:00Z"}},
    "delivery-default": {},
    "delivery-empty": {"delivery": {"mode": "none", "channel": "", "to": "", "accountId": "", "threadId": ""}},
    "delivery-explicit": {
        "delivery": {
            "mode": "announce",
            "channel": "telegram",
            "to": "synthetic-destination",
            "accountId": "synthetic-account",
            "threadId": "42",
        }
    },
    "system-main": {"payload": {"kind": "systemEvent", "text": "Synthetic contract event"}, "sessionTarget": "main"},
    "system-isolated": {
        "payload": {"kind": "systemEvent", "text": "Synthetic contract event"},
        "sessionTarget": "isolated",
    },
    "disabled": {"enabled": False},
    "at-offset-stable": {"schedule": {"kind": "at", "at": "2027-01-01T09:00:00+09:00", "tz": "Asia/Tokyo"}},
    "at-utc-stable": {"schedule": {"kind": "at", "at": "2027-01-01T00:00:00Z"}},
}
# Enumerate the real model registry, including the plugin enforcement description.
from apps.cron.models import CronCreationPath, CronJob, CronPattern
from apps.cron.signals import cronjob_derive_data_from_typed_payload
from apps.tenants.models import Tenant

payloads = {
    "pure_reminder": {"text": "Synthetic reminder"},
    "quote_user_intent": {"text": "Synthetic intent"},
    "domain_summary": {"query_tool": "nbhd_task_list", "render_block": "task_summary"},
    "daily_briefing": {},
    "workout_congrats": {"activity": "Synthetic walk"},
    "task_hygiene": {},
}
assert set(payloads) == set(CronPattern.values)
for pattern, typed_payload in payloads.items():
    for kind, schedule in {
        "cron": {"kind": "cron", "expr": "17 9 * * *", "tz": "UTC"},
        "every": {"kind": "every", "everyMs": 86400000},
        "at": {"kind": "at", "at": "2027-01-01T00:00:00.000Z"},
    }.items():
        row = CronJob(
            tenant=Tenant(),
            name=f"typed-{pattern}-{kind}",
            pattern=pattern,
            creation_path=CronCreationPath.TYPED,
            typed_payload=typed_payload,
            data={"schedule": schedule},
        )
        with patch.object(CronJob.objects, "get", side_effect=CronJob.DoesNotExist):
            cronjob_derive_data_from_typed_payload(CronJob, row)
        cases[row.name] = row.data
        cases[row.name + "-fallbacks"] = copy.deepcopy(row.data)
        cases[row.name + "-fallbacks"]["payload"]["fallbacks"] = ["v4-pro"]

base_payload = {"kind": "agentTurn", "message": "Synthetic contract reminder"}
for key, value in {
    "model": "v4-flash",
    "toolsAllow": ["nbhd_send_to_user"],
    "lightContext": True,
    "timeoutSeconds": 120,
    "fallbacks": ["v4-pro"],
}.items():
    cases["control-" + key] = {"payload": {**base_payload, key: value}}
    cases["null-" + key] = {"payload": {**base_payload, key: None}}
cases.update(
    {
        "text-alias": {"payload": {"kind": "agentTurn", "text": "Synthetic contract reminder"}},
        "agent-main": {"agentId": "main"},
        "wake-heartbeat": {"wakeMode": "next-heartbeat"},
        "description": {"description": "Synthetic enforcement description"},
        "announce": {"delivery": {"mode": "announce"}},
        "announce-telegram": {"delivery": {"mode": "announce", "channel": "telegram"}},
        "at-numeric-stable": {"schedule": {"kind": "at", "at": 1798761600000}},
        "at-ms-stable": {"schedule": {"kind": "at", "atMs": 1798761600000}},
        "at-expr-stable": {"schedule": {"kind": "at", "expr": "2027-01-01T00:00:00Z"}},
        "null-optionals": {
            "payload": {
                **base_payload,
                **dict.fromkeys(["model", "toolsAllow", "lightContext", "timeoutSeconds", "fallbacks"]),
            },
            "description": None,
            "agentId": None,
            "wakeMode": None,
            "sessionTarget": None,
            "delivery": None,
        },
    }
)
cases.update(
    {
        "null-all-optionals": {
            "payload": {
                **base_payload,
                **dict.fromkeys(
                    [
                        "model",
                        "fallbacks",
                        "thinking",
                        "timeoutSeconds",
                        "toolsAllow",
                        "lightContext",
                        "toolsAllowIsDefault",
                    ]
                ),
            },
            "schedule": {"kind": "cron", "expr": "17 9 * * *", "tz": None, "staggerMs": None},
            "delivery": dict.fromkeys(["mode", "channel", "to", "accountId", "threadId", "bestEffort"]),
            **dict.fromkeys(
                [
                    "description",
                    "agentId",
                    "wakeMode",
                    "sessionTarget",
                    "sessionKey",
                    "deleteAfterRun",
                    "displayName",
                    "pacing",
                ]
            ),
        },
        "null-anchor": {"schedule": {"kind": "every", "everyMs": 86400000, "anchorMs": None}},
        "light-false": {"payload": {**base_payload, "lightContext": False}},
        "tools-wildcard": {"payload": {**base_payload, "toolsAllow": ["*"]}},
        "tools-string": {"payload": {**base_payload, "toolsAllow": "nbhd_send_to_user"}},
    }
)
# Real system-cron rows (config_generator): tier/user model pinned at the top
# level, explicit delivery none, isolated. Fleet census 2026-09-25.
for suffix, model in {
    "flash-0731": "openrouter/deepseek/deepseek-v4-flash-0731",
    "flash": "openrouter/deepseek/deepseek-v4-flash",
}.items():
    cases["system-model-" + suffix] = {
        "model": model,
        "delivery": {"mode": "none"},
        "sessionTarget": "isolated",
    }
import argparse

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("output", type=Path)
parser.add_argument(
    "--cases", help="Capture only these comma-separated case names; stable IDs retain full matrix indexes"
)
options = parser.parse_args()
folder = options.output
selected_cases = set(options.cases.split(",")) if options.cases else set(cases)
if selected_cases - set(cases):
    parser.error("unknown_case")
folder.mkdir(parents=True, exist_ok=True)
inputs = []
for index, (name, overrides) in enumerate(cases.items(), 1):
    if name not in selected_cases:
        continue
    job = {
        "name": name,
        "enabled": True,
        "schedule": {"kind": "cron", "expr": "17 9 * * *", "tz": "UTC"},
        "payload": {"kind": "agentTurn", "message": "Synthetic contract reminder"},
        **overrides,
    }
    authored = copy.deepcopy(job)
    if name.endswith("-stable"):
        from apps.orchestrator.migration_preservation import writer_stable_declaration

        job = writer_stable_declaration(job)
    row = SimpleNamespace(id=index, name=name, enabled=job["enabled"], managed=True, data=job)
    with (
        patch("apps.cron.models.CronJob.objects.filter") as rows,
        patch("apps.cron.gateway_client.get_gateway_token_for_tenant", return_value="local-contract-token"),
    ):
        rows.return_value.order_by.return_value = [row] if row.enabled else []
        doc, count = build_signed_crons_doc(SimpleNamespace(id="local-contract"))
    (folder / (name + ".signed.json")).write_bytes(doc)
    declaration = _row_to_cron_dict(row)
    declaration["declarationKey"] = f"nbhd:{index}"
    inputs.append({"case": name, "declaration": declaration, "selected": count == 1, "authored": authored})
(folder / "inputs.json").write_text(json.dumps(inputs, indent=2) + "\n")
(folder / "openclaw.json").write_text(
    json.dumps(
        {
            "gateway": {
                "mode": "local",
                "auth": {"mode": "token", "token": "local-contract-token"},
                "bind": "loopback",
            },
            "cron": {"enabled": False},
            "plugins": {"enabled": False},
        }
    )
)

from hashlib import sha256

(folder / "input-metadata.json").write_text(
    json.dumps(
        {
            "sources": {
                name: sha256(Path(name).read_bytes()).hexdigest()
                for name in (
                    "runtime/openclaw/nbhd-cron-sync.mjs",
                    "apps/cron/share_cron_sync.py",
                    "apps/orchestrator/cron_reconcile.py",
                    "apps/cron/models.py",
                    *[str(p) for p in sorted(Path("apps/cron/patterns").glob("*.py"))],
                    "apps/cron/signals.py",
                    "runtime/openclaw/plugins/nbhd-automation-tools/index.js",
                    "runtime/openclaw/plugins/nbhd-cron-enforcement/index.js",
                )
            },
            "synthetic": True,
            "database": "Mock query results only; real selector, row renderer and HMAC signer",
        },
        indent=2,
    )
    + "\n"
)
