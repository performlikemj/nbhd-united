import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import {
  assertHookOnlyManifestsDeclareActivation,
  discoverHookOnlyPlugins,
} from "./hook-activation-contract.mjs";

test("every hook-only plugin declares hook activation in its manifest", () => {
  const discovered = discoverHookOnlyPlugins();
  assert.ok(discovered.length > 0, "the source-derived hook-only plugin inventory must not be empty");
  assert.deepEqual(
    assertHookOnlyManifestsDeclareActivation().map(({ id }) => id),
    discovered.map(({ id }) => id),
  );
});

test("every hook-only plugin emits a warn-level registered line", () => {
  const missing = discoverHookOnlyPlugins().filter(({ indexPath }) => {
    const source = readFileSync(indexPath, "utf8");
    return !/(?:logger\.warn\s*\(|safeLog\s*\(\s*api\s*,\s*"warn"\s*,)[\s\S]{0,240}\bregistered\b/u.test(source);
  });
  assert.deepEqual(missing.map(({ id }) => id), []);
});
