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
import register, { extractUsage } from "./index.js";

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

test("tags helper usage without changing ordinary usage", () => {
  const event = { runId: "child-run-123", model: "google/gemini-flash", usage: { input: 12, output: 4 } };
  const ordinary = extractUsage(event, { sessionKey: "agent:main:openai-user:thread:abc" });
  assert.equal(ordinary.event_type, "message");
  assert.equal(ordinary.metadata, undefined);

  const helper = extractUsage(event, {
    sessionKey: "agent:main:subagent:8cf81ea8-34ac-4fcc-8ada-d35df405cd18",
    runId: "child-run-123",
  });
  assert.equal(helper.event_type, "subagent_message");
  assert.deepEqual(helper.metadata, { kind: "subagent", run: "c9bbca7c59e7" });
});

test("helperOnly skips normal llm_output and reports helper llm_output", async () => {
  const handlers = {};
  const api = {
    pluginConfig: {
      apiBaseUrl: "https://nbhd.test",
      tenantId: "tenant-helper-only",
      internalApiKey: "test-key",
      helperOnly: true,
    },
    on: (event, handler) => { handlers[event] = handler; },
    logger: noopLogger,
  };
  register(api);

  const calls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), payload: JSON.parse(options.body) });
    return { ok: true, status: 200, async text() { return ""; } };
  };
  try {
    const event = { runId: "helper-run", model: "google/gemini-flash", usage: { input: 12, output: 4 } };
    handlers.llm_output(event, { sessionKey: "agent:main:openai-user:thread:normal", runId: "normal-run" });
    assert.equal(calls.length, 0);

    handlers.llm_output(event, { sessionKey: "agent:main:subagent:child", runId: "helper-run" });
    await new Promise((resolve) => setImmediate(resolve));
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(calls.length, 1);
  assert.equal(calls[0].payload.event_type, "subagent_message");
  assert.deepEqual(calls[0].payload.metadata, { kind: "subagent", run: "7c284f8559a9" });
});
