#!/usr/bin/env bash
# Install the exact OpenClaw dependency graph recorded in the committed lockfile.

set -euo pipefail

INSTALL_ROOT="${1:?usage: install_pinned_openclaw.sh <install-root>}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PIN_ROOT="$REPO_ROOT/runtime/openclaw/pinned-runtime"
OPENCLAW_VERSION="$(sed -nE 's/^ARG OPENCLAW_VERSION=([^[:space:]]+).*$/\1/p' "$REPO_ROOT/Dockerfile.openclaw" | head -n 1)"
LOCKED_VERSION="$(node -p "require('$PIN_ROOT/package.json').dependencies.openclaw")"

if [ -z "$OPENCLAW_VERSION" ]; then
  echo "Could not resolve OPENCLAW_VERSION from Dockerfile.openclaw" >&2
  exit 1
fi
if [ "$LOCKED_VERSION" != "$OPENCLAW_VERSION" ]; then
  echo "Pinned-runtime lock version $LOCKED_VERSION does not match Dockerfile OpenClaw $OPENCLAW_VERSION" >&2
  exit 1
fi

mkdir -p "$INSTALL_ROOT"
cp "$PIN_ROOT/package.json" "$PIN_ROOT/package-lock.json" "$INSTALL_ROOT/"
echo "Installing integrity-locked OpenClaw pin: $OPENCLAW_VERSION"
npm ci \
  --prefix "$INSTALL_ROOT" \
  --no-audit \
  --no-fund

INSTALLED_VERSION="$(node -p "require('$INSTALL_ROOT/node_modules/openclaw/package.json').version")"
if [ "$INSTALLED_VERSION" != "$OPENCLAW_VERSION" ]; then
  echo "Installed OpenClaw $INSTALLED_VERSION does not match Dockerfile pin $OPENCLAW_VERSION" >&2
  exit 1
fi
