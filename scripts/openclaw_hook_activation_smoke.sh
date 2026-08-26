#!/usr/bin/env bash
# Boot the exact Dockerfile OpenClaw pin with a real generated maximal config
# and require every repo-derived hook plugin in the activation line with no
# dropped or unknown typed-hook diagnostics.

set -euo pipefail

MAXIMAL_CONFIG="${1:-openclaw-maximal.json}"
if [ ! -f "$MAXIMAL_CONFIG" ]; then
  echo "Maximal OpenClaw config not found: $MAXIMAL_CONFIG" >&2
  exit 1
fi

TEMPORARY_ROOT="$(mktemp -d)"
cleanup() {
  rm -rf "$TEMPORARY_ROOT"
}
trap cleanup EXIT

./scripts/install_pinned_openclaw.sh "$TEMPORARY_ROOT/install"

OPENCLAW_REPRO_BIN="$TEMPORARY_ROOT/install/node_modules/openclaw/openclaw.mjs" \
OPENCLAW_MAXIMAL_CONFIG="$MAXIMAL_CONFIG" \
  node --test runtime/openclaw/hook-activation.e2e.test.mjs
