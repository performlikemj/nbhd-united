"""Generate the Core AI on-device model manifest from an exported LanguageBundle dir.

Walks the bundle directory, computes a streaming sha256 + byte size for every file,
and writes the manifest JSON that ``CoreAIModelManifestView`` serves. Upload the same
files to ``COREAI_MODEL_BASE_URL/<name>/<version>/<relative-path>`` (Azure Blob / CDN).

    python manage.py generate_coreai_manifest \\
        --bundle ~/Projects/nbhd-ios/Tools/coreai-verify/exported-qwen3-0_6b/qwen3_0_6b_dynamic \\
        --name qwen3_0_6b_dynamic --version 2026-06-10 \\
        --out apps/router/coreai_manifest.json
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Command(BaseCommand):
    help = "Generate the Core AI on-device model manifest from an exported bundle dir."

    def add_arguments(self, parser):
        parser.add_argument("--bundle", required=True, help="Path to the exported LanguageBundle dir")
        parser.add_argument("--name", required=True, help="Bundle name (matches the dir + hosting path)")
        # NB: `--version` is reserved by Django's BaseCommand, so use `--model-version`.
        parser.add_argument("--model-version", required=True, help="Model version (e.g. a date or semver)")
        parser.add_argument("--out", required=True, help="Where to write the manifest JSON")

    def handle(self, *args, **opts):
        bundle = Path(opts["bundle"]).expanduser()
        if not bundle.is_dir():
            raise CommandError(f"bundle dir not found: {bundle}")

        files = []
        total = 0
        for path in sorted(bundle.rglob("*")):
            if not path.is_file() or path.name == ".DS_Store":
                continue
            rel = path.relative_to(bundle).as_posix()
            size = path.stat().st_size
            files.append({"path": rel, "sha256": _sha256(path), "bytes": size})
            total += size

        if not files:
            raise CommandError(f"no files found under {bundle}")

        manifest = {
            "name": opts["name"],
            "version": opts["model_version"],
            "total_bytes": total,
            "files": files,
        }
        out = Path(opts["out"]).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(manifest, indent=2) + "\n")

        self.stdout.write(
            self.style.SUCCESS(
                f"Wrote {out} — {len(files)} files, {total / 1e9:.2f} GB.\n"
                f"Now upload the bundle to "
                f"COREAI_MODEL_BASE_URL/{opts['name']}/{opts['model_version']}/<path>."
            )
        )
