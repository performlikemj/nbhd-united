// Registration-contract test for nbhd-usage-reporter.
//   node --test runtime/openclaw/plugins/nbhd-usage-reporter/register.test.mjs
//
// model_call_started / llm_output / agent_end are TYPED conversation hooks
// (OpenClaw PLUGIN_HOOK_NAMES). They MUST be registered via api.on
// (→ registerTypedHook → the typedHooks registry the dispatchers read), NOT via
// api.registerHook (an internal-hook API that requires opts.name and never fires
// for these events). PR #746 used registerHook → "hook registration missing name"
// + silently-dead hooks on 5.28. This test pins the correct API so that mistake
// can't recur.
import { test } from "node:test";
import assert from "node:assert/strict";
import register from "./index.js";

const noopLogger = { info() {}, warn() {}, error() {}, debug() {} };
const EXPECTED = ["model_call_started", "llm_output", "agent_end"];

test("registers its typed hooks via api.on", () => {
  const events = [];
  const api = { on: (event, handler) => events.push({ event, handlerType: typeof handler }), logger: noopLogger };
  register(api);
  for (const e of EXPECTED) assert.ok(events.some((c) => c.event === e), `registers ${e} via api.on`);
  for (const c of events) assert.equal(c.handlerType, "function");
});

test("uses api.on even when api.registerHook ALSO exists (5.28 reality)", () => {
  // The whole bug: on 5.28 BOTH exist; registerHook is the wrong (internal)
  // registry for these typed events. The plugin must still pick api.on.
  const onEvents = [];
  const api = {
    on: (event) => onEvents.push(event),
    registerHook: () => { throw new Error("must NOT use registerHook for typed conversation hooks"); },
    logger: noopLogger,
  };
  register(api);
  for (const e of EXPECTED) assert.ok(onEvents.includes(e), `${e} went through api.on, not registerHook`);
});

test("registers nothing (no throw) when api.on is absent", () => {
  assert.doesNotThrow(() => register({ registerHook: () => {}, logger: noopLogger }));
});
