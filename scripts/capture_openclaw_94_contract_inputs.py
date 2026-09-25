"""Generate synthetic signed CLI-contract inputs; no database/network calls.

Run from the repository root with PYTHONPATH=. and the project's Python env.
Only query results and token lookup are stubbed: selector, row rendering and
HMAC signer are unchanged production code. Companion .mjs runs in the isolated
runtime container, never against a tenant.
"""

import os

os.environ.update(
    DJANGO_SETTINGS_MODULE="config.settings.development",
    SECRET_KEY="local-contract-test",
    DATABASE_URL="postgres://test_user:test_password@127.0.0.1:55495/oc94_r5",
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
import argparse

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("output", type=Path)
folder = parser.parse_args().output
folder.mkdir(parents=True, exist_ok=True)
inputs = []
for index, (name, overrides) in enumerate(cases.items(), 1):
    job = {
        "name": name,
        "enabled": True,
        "schedule": {"kind": "cron", "expr": "17 9 * * *", "tz": "UTC"},
        "payload": {"kind": "agentTurn", "message": "Synthetic contract reminder"},
        **overrides,
    }
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
    inputs.append({"case": name, "declaration": declaration, "selected": count == 1})
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
                )
            },
            "synthetic": True,
            "database": "Mock query results only; real selector, row renderer and HMAC signer",
        },
        indent=2,
    )
    + "\n"
)
