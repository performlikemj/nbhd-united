#!/usr/bin/env bash
# Boot the exact Dockerfile OpenClaw pin with a real generated maximal config
# and require every source-derived hook-only plugin in the activation line.

set -euo pipefail

MAXIMAL_CONFIG="${1:-openclaw-maximal.json}"
if [ ! -f "$MAXIMAL_CONFIG" ]; then
  echo "Maximal OpenClaw config not found: $MAXIMAL_CONFIG" >&2
  exit 1
fi

OPENCLAW_VERSION="$(sed -nE 's/^ARG OPENCLAW_VERSION=([^[:space:]]+).*$/\1/p' Dockerfile.openclaw | head -n 1)"
if [ -z "$OPENCLAW_VERSION" ]; then
  echo "Could not resolve OPENCLAW_VERSION from Dockerfile.openclaw" >&2
  exit 1
fi

TEMPORARY_ROOT="$(mktemp -d)"
cleanup() {
  rm -rf "$TEMPORARY_ROOT"
}
trap cleanup EXIT

echo "Installing exact OpenClaw pin: $OPENCLAW_VERSION"
npm install \
  --prefix "$TEMPORARY_ROOT/install" \
  --no-audit \
  --no-fund \
  "openclaw@$OPENCLAW_VERSION"

OPENCLAW_REPRO_BIN="$TEMPORARY_ROOT/install/node_modules/openclaw/openclaw.mjs" \
OPENCLAW_MAXIMAL_CONFIG="$MAXIMAL_CONFIG" \
  node --test runtime/openclaw/hook-activation.e2e.test.mjs
