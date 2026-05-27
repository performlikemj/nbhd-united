#!/usr/bin/env bash
# OpenClaw chmod call-site audit.
#
# Extracts the pinned openclaw npm package, finds every chmod-family call
# expression in dist/, and diffs the set of callee shapes against the
# baseline at runtime/openclaw/chmod-audit-baseline.txt.
#
# A diff fails CI — the next OpenClaw bump risks introducing a chmod variant
# that runtime/openclaw/suppress-chmod-eperm.js Layer 1 doesn't cover. The
# 2026-05-10 incident (PR #504) is what this gate would have caught:
# OpenClaw 2026.5.7 moved task-registry chmod from async-rejection to
# sync-throw, our suppression silently missed it, and cron firing stopped
# fleet-wide for ~48h before anyone noticed.
#
# Context: memory/project_openclaw_chmod_eperm_saga.md
#
# Usage:
#   scripts/openclaw_chmod_audit.sh           # check vs baseline (CI mode)
#   scripts/openclaw_chmod_audit.sh --update  # regenerate baseline

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DOCKERFILE="$REPO_ROOT/Dockerfile.openclaw"
BASELINE="$REPO_ROOT/runtime/openclaw/chmod-audit-baseline.txt"
UPDATE_MODE=0

for arg in "$@"; do
  case "$arg" in
    --update) UPDATE_MODE=1 ;;
    -h|--help)
      sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "Unknown arg: $arg" >&2
      echo "Usage: $0 [--update]" >&2
      exit 2
      ;;
  esac
done

OPENCLAW_VERSION="$(awk -F= '/^ARG OPENCLAW_VERSION=/{print $2}' "$DOCKERFILE" | tr -d ' "')"
if [ -z "$OPENCLAW_VERSION" ]; then
  echo "Could not read OPENCLAW_VERSION from $DOCKERFILE" >&2
  exit 1
fi

if ! command -v npm >/dev/null 2>&1; then
  echo "npm is required (install Node 22+) — not found on PATH" >&2
  exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "Packing openclaw@$OPENCLAW_VERSION..."
(cd "$TMP" && npm pack "openclaw@$OPENCLAW_VERSION" >/dev/null 2>&1)
TGZ="$(ls "$TMP"/openclaw-*.tgz 2>/dev/null | head -1)"
if [ -z "$TGZ" ] || [ ! -f "$TGZ" ]; then
  echo "npm pack produced no tarball for openclaw@$OPENCLAW_VERSION" >&2
  exit 1
fi
(cd "$TMP" && tar -xf "$TGZ")
DIST="$TMP/package/dist"
if [ ! -d "$DIST" ]; then
  echo "openclaw@$OPENCLAW_VERSION tarball missing dist/ — unexpected layout" >&2
  exit 1
fi

# Find every chmod-family call expression. The pattern matches:
#   fs.chmodSync(    fs.promises.chmod(    fs$N.chmod(    handle.chmod(
#   chmodSync(       chmod(                fs.fchmodSync(  ...etc
# Excludes Python source embedded in JS string literals — currently the
# only such case is `os.fchmod(...)` inside browser-bridges-*.js. Add to
# the filter regex if a new Python false positive appears.
SHAPES="$(
  grep -rnE \
    '(\bfs(\$[0-9]+)?\.(promises\.)?|\bhandle\.)?\b(chmod|lchmod|fchmod)(Sync)?\(' \
    "$DIST" 2>/dev/null \
    | grep -vE 'os\.(f|l)?chmod' \
    | grep -oE '(\bfs(\$[0-9]+)?\.(promises\.)?|\bhandle\.)?\b(chmod|lchmod|fchmod)(Sync)?\(' \
    | sort -u
)"

if [ -z "$SHAPES" ]; then
  echo "ERROR: no chmod call sites found in openclaw@$OPENCLAW_VERSION dist/." >&2
  echo "If OpenClaw has stopped chmodding entirely, suppress-chmod-eperm.js" >&2
  echo "may be vestigial — but this is a meaningful surface change. Investigate" >&2
  echo "before relaxing the gate." >&2
  exit 1
fi

# Build the new baseline file body.
build_baseline() {
  cat <<HEADER
# OpenClaw chmod call-site audit baseline.
#
# runtime/openclaw/suppress-chmod-eperm.js Layer 1 monkey-patches fs.chmodSync
# and fs.promises.chmod to suppress EPERM/EACCES/ENOTSUP on Azure Container
# Apps' root-owned mounts. Coverage is fragile to OpenClaw refactors — a new
# chmod variant slipping into OC dist/ silently breaks production
# (see PR #504 / 2026-05-10 incident).
#
# CI runs scripts/openclaw_chmod_audit.sh on every push. The script extracts
# the pinned OpenClaw npm package, scans dist/ for chmod-family call
# expressions, and diffs the observed callee-shape set against this file.
# Any diff fails the build. To accept a change:
#
#   1. Run \`./scripts/openclaw_chmod_audit.sh --update\` locally
#   2. Inspect the diff and decide for each new shape:
#        - Does Layer 1 cover it? (Patch on fs.chmodSync covers bare
#          chmodSync destructured from fs; patch on fs.promises.chmod
#          covers fs\$N.chmod aliases that point at node:fs/promises.)
#        - If not, does it run in a path that throws to caller? (Check
#          OC source for surrounding try/catch.)
#   3. Update Layer 1 if needed, then commit the refreshed baseline with
#      a rationale in the commit message.
#
# Coverage notes (most recent review):
#   COVERED — Layer 1 monkey-patch catches:
#     fs.chmodSync(           direct sync
#     chmodSync(              destructured \`import { chmodSync } from "node:fs"\`
#                             (covered because suppress-chmod-eperm.js loads
#                             via NODE_OPTIONS=--require BEFORE OpenClaw
#                             modules, so the destructured binding reads our
#                             patched fs.chmodSync)
#     fs.promises.chmod(      direct promise
#     fs\$1.chmod(             rollup alias for \`import fs\$1 from "node:fs/promises"\`
#     chmod(                  bare; either destructured from fs.promises or
#                             aliased via \`options.chmodSync ?? fs.chmodSync\`
#
#   BENIGN — uncovered but harmless on our containers as of this review:
#     fs.chmod(               callback form; concentrated in launchd-*.js
#                             (macOS install path) — never runs on Linux.
#                             One uncatchable site at launchd:434 is inside
#                             a try/catch{} block, so even on macOS it would
#                             swallow.
#     fs.fchmodSync(          wrapped in try/catch by OpenClaw at the call
#                             site (runtime-*.js trajectory pointer write)
#     handle.chmod(           FileHandle prototype; trajectory writer calls
#                             this only on files OpenClaw just created
#                             (node-owned), so no EPERM in practice
#
# If a NEW shape appears (e.g. lchmod, lchmodSync, fchmod via fs.promises,
# fchmodSync from a non-try/catch site, *Sync on a FileHandle), re-evaluate
# whether Layer 1 needs extending.

# Pinned OpenClaw version (must match Dockerfile.openclaw \`ARG OPENCLAW_VERSION\`):
openclaw-version: $OPENCLAW_VERSION

# Observed callee shapes (one per line, sorted):
HEADER
  echo "$SHAPES"
}

if [ "$UPDATE_MODE" -eq 1 ]; then
  build_baseline > "$BASELINE"
  echo "Baseline updated: $BASELINE"
  echo ""
  echo "Review the file (especially the 'Coverage notes' section) and commit"
  echo "with a rationale describing which shape(s) changed and why."
  exit 0
fi

if [ ! -f "$BASELINE" ]; then
  echo "Baseline file missing: $BASELINE" >&2
  echo "Run \`$0 --update\` to create it." >&2
  exit 1
fi

RECORDED_VERSION="$(awk -F': ' '/^openclaw-version:/{print $2; exit}' "$BASELINE" | tr -d ' ')"
if [ "$RECORDED_VERSION" != "$OPENCLAW_VERSION" ]; then
  echo "ERROR: Dockerfile.openclaw is pinned to openclaw@$OPENCLAW_VERSION" >&2
  echo "       but baseline records openclaw-version: $RECORDED_VERSION" >&2
  echo "       Run \`$0 --update\` to refresh after verifying coverage." >&2
  exit 1
fi

EXPECTED_SHAPES="$(grep -vE '^\s*(#|openclaw-version:|$)' "$BASELINE" | sort -u)"

DIFF="$(diff <(echo "$EXPECTED_SHAPES") <(echo "$SHAPES") || true)"
if [ -n "$DIFF" ]; then
  echo "ERROR: openclaw@$OPENCLAW_VERSION chmod call-site inventory has drifted from baseline." >&2
  echo "" >&2
  echo "Diff (- baseline, + current):" >&2
  echo "$DIFF" >&2
  echo "" >&2
  echo "For each new shape, decide:" >&2
  echo "  - Is the new variant covered by Layer 1 of suppress-chmod-eperm.js?" >&2
  echo "  - Could it throw to caller? (Check OC source for surrounding try/catch.)" >&2
  echo "Update Layer 1 if needed, then run \`$0 --update\` and commit the new baseline." >&2
  echo "" >&2
  echo "Context: runtime/openclaw/suppress-chmod-eperm.js + memory/project_openclaw_chmod_eperm_saga.md" >&2
  exit 1
fi

COUNT="$(echo "$SHAPES" | wc -l | tr -d ' ')"
echo "Audit OK: $COUNT distinct chmod call shapes in openclaw@$OPENCLAW_VERSION match baseline."
