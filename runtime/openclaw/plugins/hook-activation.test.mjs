import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

import {
  assertHookOnlyManifestsDeclareActivation,
  assertHookPluginDiagnosticsClean,
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
  const missing = discoverHookOnlyPlugins().filter(({ indexPath, registersTypedHooks }) => {
    if (!registersTypedHooks) return false;
    const source = readFileSync(indexPath, "utf8");
    return !/(?:logger\.warn\s*\(|safeLog\s*\(\s*api\s*,\s*"warn"\s*,)[\s\S]{0,240}\bregistered\b/u.test(source);
  });
  assert.deepEqual(missing.map(({ id }) => id), []);
});

test("declarative manifest hook owners are part of the hook-plugin inventory", (t) => {
  const root = mkdtempSync(path.join(os.tmpdir(), "nbhd-manifest-hook-owner-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const pluginDirectory = path.join(root, "declarative-hook-owner");
  mkdirSync(pluginDirectory);
  writeFileSync(path.join(pluginDirectory, "openclaw.plugin.json"), JSON.stringify({
    id: "declarative-hook-owner",
    hooks: ["./before-agent.js"],
  }));

  const discovered = discoverHookOnlyPlugins(root);
  assert.deepEqual(discovered.map(({ id }) => id), ["declarative-hook-owner"]);
  assert.equal(discovered[0].ownsManifestHooks, true);
  assert.deepEqual(
    assertHookOnlyManifestsDeclareActivation(root).map(({ id }) => id),
    ["declarative-hook-owner"],
  );
});

test("hook-plugin diagnostics reject blocked and unknown typed hooks", () => {
  const expected = [{ id: "expected-hook-plugin" }];
  assert.doesNotThrow(() => assertHookPluginDiagnosticsClean("clean boot", expected));
  assert.throws(
    () => assertHookPluginDiagnosticsClean(
      "[plugins] expected-hook-plugin: typed hook \"agent_end\" blocked because non-bundled plugins must set policy\n",
      expected,
    ),
    /blocked because non-bundled plugins must set/u,
  );
  assert.throws(
    () => assertHookPluginDiagnosticsClean(
      "[plugins] expected-hook-plugin: unknown typed hook \"future_hook\" ignored\n",
      expected,
    ),
    /unknown typed hook/u,
  );
});
