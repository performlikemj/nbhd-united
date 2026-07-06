// Registration-contract test for nbhd-friends-tools.
//   node --test runtime/openclaw/plugins/nbhd-friends-tools/index.test.mjs
//
// The config generator emits `plugins.entries["nbhd-friends-tools"].config =
// { proposeEnabled: <bool> }` for EVERY friends-enabled tenant. The manifest
// configSchema must DECLARE proposeEnabled (additionalProperties:false → the
// OpenClaw binary hard-rejects the whole config at boot otherwise; that was the
// 2026-07-06 image-boot-smoke failure) AND register() must GATE the two PROPOSE
// tools on it. This test pins both halves of that contract:
//   proposeEnabled:true  → all 4 tools registered
//   proposeEnabled:false → only the 2 context tools (propose tools absent)
//   key absent/undefined → fail-closed, same as false
import test from "node:test";
import assert from "node:assert/strict";

import register from "./index.js";

const CONTEXT_TOOLS = ["nbhd_neighborhood_context", "nbhd_mission_context"];
const PROPOSE_TOOLS = ["nbhd_propose_lesson_share", "nbhd_propose_mission_task"];

function registeredNames(pluginConfig) {
  const names = [];
  const api = {
    pluginConfig,
    registerTool(tool) {
      names.push(tool.name);
    },
  };
  register(api);
  return names;
}

test("proposeEnabled:true registers all 4 tools", () => {
  const names = registeredNames({ proposeEnabled: true });
  for (const n of [...CONTEXT_TOOLS, ...PROPOSE_TOOLS]) {
    assert.ok(names.includes(n), `expected ${n} to be registered`);
  }
  assert.equal(names.length, 4);
});

test("proposeEnabled:false registers only the 2 context tools", () => {
  const names = registeredNames({ proposeEnabled: false });
  for (const n of CONTEXT_TOOLS) assert.ok(names.includes(n), `expected ${n} registered`);
  for (const n of PROPOSE_TOOLS) assert.ok(!names.includes(n), `expected ${n} NOT registered`);
  assert.equal(names.length, 2);
});

test("proposeEnabled absent (undefined) fails closed → only 2 context tools", () => {
  const names = registeredNames({});
  for (const n of CONTEXT_TOOLS) assert.ok(names.includes(n), `expected ${n} registered`);
  for (const n of PROPOSE_TOOLS) assert.ok(!names.includes(n), `expected ${n} NOT registered`);
  assert.equal(names.length, 2);
});

test("non-boolean truthy proposeEnabled does NOT register propose tools (strict === true)", () => {
  const names = registeredNames({ proposeEnabled: "true" });
  for (const n of PROPOSE_TOOLS) assert.ok(!names.includes(n), `expected ${n} NOT registered`);
  assert.equal(names.length, 2);
});
