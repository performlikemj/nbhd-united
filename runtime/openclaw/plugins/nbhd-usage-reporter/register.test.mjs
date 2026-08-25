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

test("extractUsage tags scoped usage without changing ordinary legacy usage", () => {
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

  const cron = extractUsage(event, { trigger: "cron", runId: "cron-run-123" }, null, "cron");
  assert.equal(cron.event_type, "cron_message");
  assert.deepEqual(cron.metadata, { kind: "cron", run: "8dae770edd52" });
});

async function captureReports(pluginConfig, invoke) {
  const handlers = {};
  const api = {
    pluginConfig: {
      apiBaseUrl: "https://nbhd.test",
      tenantId: "tenant-test",
      internalApiKey: "test-key",
      ...pluginConfig,
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
    invoke(handlers);
    await new Promise((resolve) => setImmediate(resolve));
  } finally {
    globalThis.fetch = originalFetch;
  }
  return calls;
}

test("cron scope meters cron llm_output as cron_message", async () => {
  const calls = await captureReports({ meterScopes: ["cron"] }, (handlers) => {
    handlers.llm_output(
      { model: "google/gemini-flash", usage: { input: 12, output: 4 } },
      { trigger: "cron", sessionKey: "agent:main:cron:job", runId: "cron-run" },
    );
  });

  assert.equal(calls.length, 1);
  assert.equal(calls[0].payload.event_type, "cron_message");
  assert.deepEqual(calls[0].payload.metadata, { kind: "cron", run: "532fe297fa1f" });
});

test("helper scope meters helper llm_output as subagent_message", async () => {
  const calls = await captureReports({ meterScopes: ["helper"] }, (handlers) => {
    handlers.llm_output(
      { model: "google/gemini-flash", usage: { input: 12, output: 4 } },
      { sessionKey: "agent:main:subagent:child", runId: "helper-run" },
    );
  });

  assert.equal(calls.length, 1);
  assert.equal(calls[0].payload.event_type, "subagent_message");
  assert.deepEqual(calls[0].payload.metadata, { kind: "subagent", run: "7c284f8559a9" });
});

test("meterScopes skips user-originated HTTP llm_output", async () => {
  const calls = await captureReports({ meterScopes: ["helper", "cron"] }, (handlers) => {
    handlers.llm_output(
      { model: "google/gemini-flash", usage: { input: 12, output: 4 } },
      { trigger: "user", sessionKey: "agent:main:openai-user:thread:normal", runId: "normal-run" },
    );
  });

  assert.equal(calls.length, 0);
});

test("absent meterScopes preserves legacy all-turn reporting", async () => {
  const calls = await captureReports({}, (handlers) => {
    handlers.llm_output(
      { model: "google/gemini-flash", usage: { input: 12, output: 4 } },
      { trigger: "user", sessionKey: "agent:main:openai-user:thread:normal", runId: "normal-run" },
    );
  });

  assert.equal(calls.length, 1);
  assert.equal(calls[0].payload.event_type, "message");
  assert.equal(calls[0].payload.metadata, undefined);
});

test("meterScopes still reports ordinary-session BYO failures", async () => {
  const calls = await captureReports({ meterScopes: ["helper", "cron"] }, (handlers) => {
    const ctx = { trigger: "user", sessionKey: "agent:main:openai-user:thread:normal", runId: "normal-run" };
    handlers.model_call_started({ provider: "anthropic", model: "anthropic/claude-sonnet" }, ctx);
    handlers.agent_end({ success: false, error: "401 Unauthorized: invalid API key" }, ctx);
  });

  assert.equal(calls.length, 1);
  assert.ok(calls[0].url.endsWith("/byo/error/"));
  assert.equal(calls[0].payload.provider, "anthropic");
  assert.equal(calls[0].payload.reason, "auth_permanent");
});
