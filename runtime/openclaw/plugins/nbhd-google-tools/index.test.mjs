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
  process.env.NBHD_PREVIEW_KEY = "preview-key";

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
  assert.equal(calls[0].options.headers["X-Preview-Key"], "preview-key");
  assert.match(calls[0].url, /\/api\/v1\/integrations\/runtime\/tenant-1\/gmail\/messages\/\?q=in%3Ainbox&max_results=3$/);
});

test("nbhd_journal_create_entry uses POST with JSON payload", async () => {
  process.env.NBHD_TENANT_ID = "tenant-abc";
  process.env.NBHD_INTERNAL_API_KEY = "shared-key";
  process.env.NBHD_PREVIEW_KEY = "";

  const { api, tools } = buildApi();
  const calls = [];
  global.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return mockResponse({ payload: { tenant_id: "tenant-abc", entry: { id: "entry-1" } } });
  };

  register(api);
  const tool = tools.get("nbhd_journal_create_entry");
  assert.ok(tool, "journal create tool should be registered");

  const result = await tool.execute("2", {
    date: "2026-02-12",
    mood: "focused",
    energy: "medium",
    wins: ["Ship feature"],
    challenges: ["Meetings"],
    reflection: "Protect deep work",
    raw_text: "Session summary",
  });

  assert.equal(calls.length, 1);
  assert.equal(calls[0].options.method, "POST");
  assert.equal(calls[0].options.headers["Content-Type"], "application/json");
  const parsed = JSON.parse(calls[0].options.body);
  assert.equal(parsed.date, "2026-02-12");
  assert.deepEqual(parsed.wins, ["Ship feature"]);
  assert.equal(result.details.json.entry.id, "entry-1");
});

test("runtime error payloads are surfaced with error code/detail", async () => {
  process.env.NBHD_TENANT_ID = "tenant-err";
  process.env.NBHD_INTERNAL_API_KEY = "shared-key";
  process.env.NBHD_PREVIEW_KEY = "";

  const { api, tools } = buildApi();
  global.fetch = async () =>
    mockResponse({
      status: 400,
      payload: { error: "invalid_request", detail: "bad date range" },
    });

  register(api);
  const tool = tools.get("nbhd_journal_list_entries");
  assert.ok(tool, "journal list tool should be registered");

  await assert.rejects(
    () => tool.execute("3", { date_from: "2026-02-12", date_to: "2026-02-01" }),
    /invalid_request \(bad date range\)/,
  );
});
