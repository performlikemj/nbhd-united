#!/bin/sh
set -eu

ASSET_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$ASSET_DIR/../../.." && pwd)

for relative_path in \
    apps/fuel/runtime_views.py \
    apps/fuel/tests.py \
    apps/fuel/test_update_plan_merge.py \
    runtime/openclaw/plugins/nbhd-fuel-tools/index.js \
    runtime/openclaw/plugins/nbhd-fuel-tools/index.test.mjs
do
    cp "$ASSET_DIR/$relative_path" "$REPO_ROOT/$relative_path"
    printf 'placed %s\n' "$relative_path"
done
