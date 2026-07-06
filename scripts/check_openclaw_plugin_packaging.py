#!/usr/bin/env python3
"""CI guard: every OpenClaw plugin the config can load must be baked into the image.

The 2026-07-05 incident: ``apps/orchestrator/config_generator.py`` emits
``plugins.load.paths: /opt/nbhd/plugins/nbhd-friends-tools`` whenever a tenant
has ``friends_enabled``, but ``Dockerfile.openclaw`` never ``COPY``'d that
plugin into the image. OpenClaw hard-fails boot on a declared-but-absent plugin
path — ``plugin path not found: /opt/nbhd/plugins/nbhd-friends-tools`` — so the
gateway (:18789) refused to start while the proxy (:8080) stayed up, bricking
every friends tenant while Azure health looked green. ``nbhd-agenda-tools`` had
the identical latent omission.

This static check (no DB, no Docker) would have caught BOTH at PR time. It
enforces two invariants; either one failing exits non-zero with the offending
plugin named:

  1. PACKAGING COMPLETENESS — every plugin source dir that carries an
     ``openclaw.plugin.json`` manifest is ``COPY``'d into ``/opt/nbhd/plugins/``.
     (Catches a shippable plugin whose COPY line was forgotten: friends, agenda.)

  2. NO DANGLING REFERENCE — every ``/opt/nbhd/plugins/<name>`` path the config
     generator can emit is ``COPY``'d. (The property whose violation is the
     literal boot crash.)

Usage: ``python scripts/check_openclaw_plugin_packaging.py`` — prints a summary
and exits 0 when clean, 1 with a named failure otherwise.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGINS_SRC = REPO_ROOT / "runtime" / "openclaw" / "plugins"
DOCKERFILE = REPO_ROOT / "Dockerfile.openclaw"
CONFIG_GENERATOR = REPO_ROOT / "apps" / "orchestrator" / "config_generator.py"

_PLUGIN_DEST_RE = re.compile(r"^\s*COPY\s+\S+\s+/opt/nbhd/plugins/([\w.-]+)", re.MULTILINE)
_PLUGIN_REF_RE = re.compile(r"/opt/nbhd/plugins/([\w.-]+)")


def shippable_plugins(plugins_src: Path = PLUGINS_SRC) -> set[str]:
    """Plugin dir names that carry an ``openclaw.plugin.json`` manifest."""
    return {manifest.parent.name for manifest in plugins_src.glob("*/openclaw.plugin.json")}


def dockerfile_copied_plugins(dockerfile: Path = DOCKERFILE) -> set[str]:
    """Plugin dir names ``COPY``'d into ``/opt/nbhd/plugins/`` by the Dockerfile."""
    return set(_PLUGIN_DEST_RE.findall(dockerfile.read_text()))


def config_emittable_plugins(config_generator: Path = CONFIG_GENERATOR) -> set[str]:
    """Plugin dir names referenced as ``/opt/nbhd/plugins/<name>`` in the generator.

    These are the paths that can land in ``plugins.load.paths`` across feature
    flags — the ones OpenClaw will try (and fail) to load if absent.
    """
    return set(_PLUGIN_REF_RE.findall(config_generator.read_text()))


def find_packaging_errors(
    shippable: set[str],
    copied: set[str],
    emittable: set[str],
) -> list[str]:
    """Pure core — returns a list of human-readable errors (empty when clean)."""
    errors: list[str] = []

    missing_package = sorted(shippable - copied)
    if missing_package:
        errors.append(
            "Shippable plugin(s) present in runtime/openclaw/plugins/ but NOT COPY'd "
            f"into Dockerfile.openclaw: {missing_package}. Add a "
            "`COPY runtime/openclaw/plugins/<name> /opt/nbhd/plugins/<name>` line — "
            "an absent plugin path makes OpenClaw hard-fail boot with "
            "'plugin path not found'."
        )

    dangling = sorted(emittable - copied)
    if dangling:
        errors.append(
            "config_generator can emit plugins.load.paths for plugin(s) the image "
            f"never COPYs: {dangling}. This is the exact 2026-07-05 boot-crash class "
            "('plugin path not found: /opt/nbhd/plugins/<name>'). COPY them in "
            "Dockerfile.openclaw."
        )

    return errors


def main() -> int:
    shippable = shippable_plugins()
    copied = dockerfile_copied_plugins()
    emittable = config_emittable_plugins()

    errors = find_packaging_errors(shippable, copied, emittable)
    if errors:
        print("OpenClaw plugin packaging check FAILED:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print(
        f"OK: all {len(shippable)} shippable plugin(s) COPY'd into the image; "
        f"all {len(emittable & shippable)} config-emittable plugin path(s) packaged."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
