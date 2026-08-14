#!/usr/bin/env bash
# Install the exact Dockerfile OpenClaw pin and exercise the authenticated
# /v1/chat/completions -> real loaded datebook plugin -> runtime POST chain.

set -euo pipefail

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
  node --test runtime/openclaw/origin-propagation.e2e.test.mjs
