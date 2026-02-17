import test from "node:test";
import assert from "node:assert/strict";

import register from "./index.js";

function buildApi() {
  const tools = new Map();
  const api = {
    pluginConfig: { apiBaseUrl: "https://nbhd.test" },
    registerTool(tool) {
      tools.set(tool.name, tool);
    },
  };
  return { api, tools };
}

function mockResponse({ status = 200, payload = {} } = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async text() {
      return JSON.stringify(payload);
    },
  };
}

test("nbhd_gmail_list_messages uses GET with tenant-scoped headers", async () => {
  process.env.NBHD_TENANT_ID = "tenant-1";
  process.env.NBHD_INTERNAL_API_KEY = "internal-key";

  const { api, tools } = buildApi();
  const calls = [];
  global.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return mockResponse({ payload: { messages: [], result_size_estimate: 0 } });
  };

  register(api);
  const tool = tools.get("nbhd_gmail_list_messages");
  assert.ok(tool, "tool should be registered");

  await tool.execute("1", { q: "in:inbox", max_results: 3 });

  assert.equal(calls.length, 1);
  assert.equal(calls[0].options.method, "GET");
  assert.equal(calls[0].options.headers["X-NBHD-Internal-Key"], "internal-key");
  assert.equal(calls[0].options.headers["X-NBHD-Tenant-Id"], "tenant-1");
  assert.match(calls[0].url, /\/api\/v1\/integrations\/runtime\/tenant-1\/gmail\/messages\/\?q=in%3Ainbox&max_results=3$/);
});

test("registers exactly 4 Google tools (no journal duplicates)", () => {
  process.env.NBHD_TENANT_ID = "tenant-check";
  process.env.NBHD_INTERNAL_API_KEY = "shared-key";

  const { api, tools } = buildApi();
  register(api);

  const expected = [
    "nbhd_gmail_list_messages",
    "nbhd_gmail_get_message_detail",
    "nbhd_calendar_list_events",
    "nbhd_calendar_get_freebusy",
  ];

  assert.equal(tools.size, expected.length, `expected ${expected.length} tools, got ${tools.size}`);
  for (const name of expected) {
    assert.ok(tools.has(name), `missing tool: ${name}`);
  }
});

test("runtime error payloads are surfaced with error code/detail", async () => {
  process.env.NBHD_TENANT_ID = "tenant-err";
  process.env.NBHD_INTERNAL_API_KEY = "shared-key";

  const { api, tools } = buildApi();
  global.fetch = async () =>
    mockResponse({
      status: 400,
      payload: { error: "invalid_request", detail: "bad query" },
    });

  register(api);
  const tool = tools.get("nbhd_gmail_list_messages");
  assert.ok(tool, "gmail tool should be registered");

  await assert.rejects(
    () => tool.execute("3", { q: "invalid" }),
    /invalid_request \(bad query\)/,
  );
});
