#!/usr/bin/env bash
# Install the exact Dockerfile OpenClaw pin and exercise the authenticated
# /v1/chat/completions -> real loaded datebook plugin -> runtime POST chain.

set -euo pipefail

TEMPORARY_ROOT="$(mktemp -d)"
cleanup() {
  rm -rf "$TEMPORARY_ROOT"
}
trap cleanup EXIT

./scripts/install_pinned_openclaw.sh "$TEMPORARY_ROOT/install"

OPENCLAW_REPRO_BIN="$TEMPORARY_ROOT/install/node_modules/openclaw/openclaw.mjs" \
  node --test runtime/openclaw/origin-propagation.e2e.test.mjs
